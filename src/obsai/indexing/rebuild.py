"""Build a fresh derived index beside the live one, then atomically replace it.

该模块实现了工业级的蓝绿/影子数据库原子全量重建机制（Shadow Index Rebuilding）。

核心架构与设计原则：
1. 蓝绿影子库热替换（Blue-Green Shadow Database Swap）：
   全量重建并不直接在正在运行的生产库（index.db）上执行，而是在同级目录下构建独立的影子库（index.db.building）。
   构建与校验全流程中，线上只读查询不受任何影响；最终通过 POSIX 原生原子重命名（os.replace）瞬间完成割接，实现零停机。
2. 跨进程非阻塞排他锁（Cross-Process Concurrency Protection）：
   通过底层 fcntl.flock(descriptor, LOCK_EX | LOCK_NB) 锁定独立锁文件（.building.lock），
   杜绝多个 CLI 或后台服务同时触发全量重建而引发磁盘与 CPU 竞争。
3. 严密健康审查与基数对账（Integrity & Cardinality Audit）：
   影子库上线前必须经历三重严酷质检：
   - PRAGMA integrity_check == "ok"（B-Tree 物理存储页完整无损）；
   - PRAGMA foreign_key_check is None（外键关系无任何孤儿记录）；
   - 基数对账：notes 表总数必须与物理文件数 1:1 吻合，chunks 切片数必须与 FTS5 全文索引数完全相等。
4. 活跃库写冲突防覆盖保护（Live Fingerprint Drift Detection）：
   在切库前对比活跃库的 inode、大小与纳秒级修改时间戳（st_mtime_ns）。
   若发现重建期间有其他并发写入者提交了数据，果断放弃替换并报错，坚决防止覆盖第三方最新提交。
5. 抗断电与信号防御（Crash Consistency & Graceful Cutover）：
   在原子替换与父目录 fsync 关键区使用 defer_shutdown() 推迟响应 SIGINT/SIGTERM 中断信号，
   杜绝因外部中断产生“半替换”坏死状态。
"""

import fcntl
import os
from pathlib import Path

from obsai.errors import SchemaError
from obsai.indexing.incremental import IncrementalIndexer, UpdateResult
from obsai.safe_write.service import _sync_directory
from obsai.shutdown import check_shutdown, defer_shutdown
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionService
from obsai.vault.scanner import scan_markdown_files


class ShadowIndexRebuilder:
    """影子索引重建管理器，负责在旁路安全重建索引并原子替换生效。"""

    def __init__(self, vault_root: Path, database_path: Path):
        """初始化影子索引重建器。

        :param vault_root: Obsidian 知识库物理根目录
        :param database_path: 目标生产索引数据库文件路径（如 .obsai/index.db）
        """
        self.vault = vault_root.expanduser().resolve(strict=True)
        self.database_path = database_path.expanduser()
        # 同级目录下构建影子库，确保跨文件重命名处于同一文件系统分区，满足 POSIX 原子替换要求
        self.shadow_path = self.database_path.with_name(self.database_path.name + ".building")
        self.lock_path = self.database_path.with_name(self.database_path.name + ".building.lock")

    @staticmethod
    def _remove_derived(path: Path) -> None:
        """清理指定数据库路径及其衍生的全部临时副产物文件（-wal, -shm, -journal）。

        :param path: 数据库基准路径
        :raises SchemaError: 若发现符号链接或非普通文件，直接报错拦截，防止恶意穿透删除
        """
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(path) + suffix)
            # 安全防线：拒绝处理符号链接或非文件实体
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise SchemaError(f"Unsafe shadow index artifact: {candidate}")
            candidate.unlink(missing_ok=True)

    def rebuild(self) -> UpdateResult:
        """在旁路构建全新索引，完成深度校验后原子替换生产数据库。

        执行流程：
        1. 事务就绪性检查：确保知识库当前无挂起的未提交文件事务；
        2. 活跃库指纹采集与 WAL 挂起连接检查；
        3. 获取跨进程排他文件锁（flock）；
        4. 在影子库（.building）中全量生成索引数据；
        5. 执行 PRAGMA 物理完整性与 FTS5/物理文件基数对账；
        6. 校验活跃库指纹无并发修改漂移；
        7. 延迟信号响应（defer_shutdown），执行 os.replace 原子割接并对目录执行 fsync；
        8. 释放锁并安全清理临时文件。

        :return: 包含重建所有变更明细的 UpdateResult
        :raises SchemaError: 锁竞争、校验失败、写冲突或文件异常时抛出
        """
        # 1. 事务就绪性检查：确保无挂起的文件事务
        TransactionService(self.vault).ensure_ready()

        # 2. 生产库前置安全检查
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.is_symlink() or (self.database_path.exists() and not self.database_path.is_file()):
            raise SchemaError(f"Unsafe live index path: {self.database_path}")

        # 采集生产库初始物理指纹四元组（设备号、inode、文件大小、纳秒级修改时间）
        original_stat = self.database_path.stat() if self.database_path.exists() else None
        original_fingerprint = (
            (original_stat.st_dev, original_stat.st_ino, original_stat.st_size,
             original_stat.st_mtime_ns) if original_stat is not None else None
        )

        # 检查是否存在活跃的 WAL 侧车文件（若存在说明其他进程尚未断开连接）
        for suffix in ("-wal", "-shm"):
            if Path(str(self.database_path) + suffix).exists():
                raise SchemaError("Live index has WAL sidecars; close other index connections before rebuild")

        # 3. 获取跨进程文件锁
        if self.lock_path.is_symlink():
            raise SchemaError(f"Unsafe shadow index lock: {self.lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            try:
                # 尝试获取非阻塞排他锁，若已有其他重建进程在运行则立即报错
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SchemaError("Another index rebuild is already running") from exc

            # 清理之前可能意外中断留下的影子库残留
            self._remove_derived(self.shadow_path)
            try:
                check_shutdown()
                # 4. 在隔离的影子库中全量构建新索引
                with Database(self.shadow_path) as shadow:
                    result = IncrementalIndexer(IndexRepository(shadow)).update(self.vault)
                    check_shutdown()

                    # 5.1 检查 SQLite 底层 B-Tree 物理页完整性
                    integrity = shadow.connection.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise SchemaError(f"Shadow index integrity check failed: {integrity}")

                    # 5.2 检查全表外键约束是否健全
                    if shadow.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise SchemaError("Shadow index foreign key check failed")

                    # 5.3 基数对账：物理文件数 == 数据库笔记数，切片数 == 全文索引数
                    note_count = shadow.connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                    chunk_count = shadow.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                    fts_count = shadow.connection.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
                    if note_count != len(scan_markdown_files(self.vault)) or chunk_count != fts_count:
                        raise SchemaError("Shadow index validation counts do not match the Vault or FTS")

                check_shutdown()
                # 6. 检查生产库在影子构建期间是否发生并发写漂移
                current_stat = self.database_path.stat() if self.database_path.exists() else None
                current_fingerprint = (
                    (current_stat.st_dev, current_stat.st_ino, current_stat.st_size,
                     current_stat.st_mtime_ns) if current_stat is not None else None
                )
                if current_fingerprint != original_fingerprint:
                    raise SchemaError("Live index changed during rebuild; retry after other writers stop")

                # 7. 关键原子割接区：推迟中断信号，执行 POSIX 原子替换与父目录 fsync 刷盘
                with defer_shutdown():
                    os.replace(self.shadow_path, self.database_path)
                    _sync_directory(self.database_path.parent)

                check_shutdown()
                return result
            finally:
                # 确保在退出前清理影子库中间文件
                self._remove_derived(self.shadow_path)
        finally:
            # 8. 释放文件锁并关闭文件描述符
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
