"""SQLite 数据库连接策略与显式、可嵌套事务管理（SQLite connection policy and explicit, nestable transactions）。

核心设计哲学与安全架构：
1. 显式可控的事务管理（Explicit Autocommit Mode via isolation_level=None）：
   关闭 Python 原生 sqlite3 隐式开启事务的黑盒行为，所有事务生命周期完全由 transaction() 显式接管；
2. 防锁升级死锁（Deadlock Prevention via BEGIN IMMEDIATE）：
   顶级事务强制采用 BEGIN IMMEDIATE 立即获取保留锁（Reserved Lock），
   杜绝并发读写事务在尝试升级为排他写锁时引发死锁；
3. 基于 Savepoint 的完全可嵌套事务（Nestable Transactions via Savepoints）：
   多层嵌套调用通过 SAVEPOINT 机制实现，内层抛出异常时仅回滚到当前保存点（ROLLBACK TO savepoint），
   内层成功时释放合并（RELEASE savepoint），外层事务拥有最终决定权；
4. 跨进程/跨线程并发锁诊断（Concurrency Diagnostics via LockBusyError）：
   通过 PRAGMA busy_timeout 设置毫秒级底层等待重试，在超时依然冲突时将 SQLite 晦涩的
   OperationalError 转换为领域统一的 LockBusyError，便于上层发起安全重试或提示用户；
5. 向量检索扩展与安全加固（sqlite-vec Extension & Security Hardening）：
   动态加载 sqlite-vec 向量检索扩展库支持余弦相似度检索；
   加载完成后立即关闭扩展加载权限（enable_load_extension(False)），防范动态库注入攻击；
   强制校验并开启外键约束（PRAGMA foreign_keys = ON）；
6. 可插拔 Schema 扩展接缝（Pluggable Schema Seam）：
   支持注入自定义建表回调，使任务日志（Job Journal）等非主索引数据库能够完美复用统一的连接策略。
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import sqlite_vec

from obsai.application.locks import LockBusyError
from obsai.errors import SchemaError
from obsai.storage.schema import initialize_schema

SchemaInitializer = Callable[[sqlite3.Connection], None]
"""数据库表结构初始化回调函数类型。接收一个 sqlite3.Connection 并完成建表与索引创建。"""


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """判断 SQLite 操作异常是否为数据库锁冲突或忙等待超时错误。

    用于识别 "database is locked" 或 "database table is locked" 等底层并发锁错误。
    """
    msg = str(exc).lower()
    return "database is locked" in msg or "busy" in msg


class Database:
    """受固定安全策略管辖的单 SQLite 数据库连接封装。

    核心特性：
    - 集成 sqlite-vec 向量嵌入搜索扩展；
    - 强制执行外键完整性约束；
    - 统一配置忙等待超时（busy_timeout）与防死锁事务模式；
    - 支持可插拔 Schema 初始化回调（如主知识库索引或后台任务日志）；
    - 支持受控的多线程共享访问（check_same_thread 配置）；
    - 支持标准上下文管理器（with Database(...) as db:）。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        schema: SchemaInitializer = initialize_schema,
        check_same_thread: bool = True,
        timeout: float = 15.0,
    ):
        """初始化 SQLite 数据库连接并应用全局安全策略。

        Args:
            path: 数据库文件系统路径，或内存数据库标识 `":memory:"`。
            schema: 表结构初始化回调，默认为主知识库索引的 `initialize_schema`。
            check_same_thread: 是否强制同线程访问限制。
                当调用方能保证串行化访问时（如工作线程写入任务日志、请求线程读取日志），可设为 False。
            timeout: 数据库锁等待超时时间（秒），默认为 15.0 秒。

        Raises:
            SchemaError: 当动态扩展不可用、外键约束无法启用或表结构初始化失败时抛出。
        """
        self.path = Path(path) if path != ":memory:" else path
        self.timeout = timeout
        self.connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=check_same_thread, timeout=timeout
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        try:
            self.connection.enable_load_extension(True)
            sqlite_vec.load(self.connection)
            self.connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error, OSError) as exc:
            self.connection.close()
            raise SchemaError(f"sqlite-vec extension is unavailable: {exc}") from exc
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.connection.close()
            raise SchemaError("SQLite foreign key enforcement is unavailable")
        try:
            schema(self.connection)
        except Exception:
            self.connection.close()
            raise
        self._savepoint_number = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启事务上下文管理器，提供原子性保障与嵌套保存点能力。

        行为规则：
        1. 顶级事务（Top-level）：执行 `BEGIN IMMEDIATE` 立即获取写锁，防范死锁；
           正常退出时执行 `COMMIT`，异常退出时执行 `ROLLBACK`；
        2. 嵌套事务（Nested）：通过 `SAVEPOINT` 创建递增命名的子事务断点（如 `obsai_sp_1`）；
           正常退出时执行 `RELEASE` 将修改合并入外层事务；异常退出时执行 `ROLLBACK TO` 回滚局部修改；
        3. 并发锁处理：若在开启、执行或提交时遇到锁冲突超时，统一包装为 `LockBusyError` 向上抛出。

        Yields:
            当前事务上下文下的 `sqlite3.Connection` 实例。

        Raises:
            LockBusyError: 当数据库被其他并发进程或线程锁定时抛出；
            BaseException: 代码块内发生的任何其他业务异常。
        """
        nested = self.connection.in_transaction
        try:
            if nested:
                self._savepoint_number += 1
                name = f"obsai_sp_{self._savepoint_number}"
                self.connection.execute(f"SAVEPOINT {name}")
            else:
                self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                raise LockBusyError(
                    f"SQLite database is busy or locked by another process: {self.path}"
                ) from exc
            raise

        try:
            yield self.connection
        except BaseException as exc:
            if nested:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
            else:
                self.connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.OperationalError) and _is_busy_error(exc):
                raise LockBusyError(
                    f"SQLite database is busy or locked by another process: {self.path}"
                ) from exc
            raise
        else:
            try:
                self.connection.execute(f"RELEASE {name}" if nested else "COMMIT")
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc):
                    raise LockBusyError(
                        f"SQLite database is busy or locked by another process: {self.path}"
                    ) from exc
                raise

    def close(self) -> None:
        """显式关闭底层的 SQLite 物理连接，释放文件描述符与锁资源。"""
        self.connection.close()

    def __enter__(self) -> "Database":
        """进入上下文管理器，返回自身实例。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开上下文管理器，自动关闭数据库连接。"""
        self.close()
