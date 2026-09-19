"""带预检、预写日志（WAL）、两阶段提交与灾难恢复的多文件知识库事务引擎。

核心设计哲学与技术架构：
1. 完备的多文件预检与两阶段提交协议（Preflighted Two-Phase Commit Protocol）：
   - 规划阶段（Plan）：在内存中完全推导所有文件的虚拟演变状态，校验待变更文件存在性、哈希唯一性、路径冲突；
   - 预检阶段（Preflight）：在物理写前严格核验当前磁盘哈希（OCC 防脏写）、读写执行权限、剩余磁盘空间，
     并强制检查是否存在跨物理设备分区移动（Cross-Device Move 防御）；
   - 执行阶段（Execute）：基于 WAL 日志分步原子应用，每一步精准记录 `applied_count`；
   - 提交阶段（Commit）：校验最终所有文件的终态内容，更新日志为 `committed`。
2. 物理写入与衍生索引持久性解耦（Decoupled Physical vs. Index Durability）：
   用户知识库中的真实 Markdown 物理文件拥有最高等级的真实性。
   一旦物理文件全部成功提交，即便后续 SQLite 索引或向量化更新发生任何异常（如数据库锁死、断电），
   事务引擎绝不会反向销毁物理文件，而是将状态标记为 `index_dirty`，交由后台增量补偿任务异步修复。
3. 停机信号感知与写保全（Graceful Shutdown Awareness）：
   通过 `check_shutdown()` 并在关键落盘与状态更新区使用 `with defer_shutdown():` 延迟退出信号，
   杜绝进程在中间状态被 SIGINT / SIGTERM 强行杀死而留下难以清理的半写残局。
4. 字节级与权限级无损回滚（Byte & Permission Fidelity Rollback）：
   回滚不仅能够通过预写快照将正文恢复至初始字节流，还能精准还原文件的 POSIX 权限模式（st_mode），
   并自动递归清理事务过程中新建的空父目录（`absent_directories`）。
5. 灾难推演与自愈恢复（State Matching & Disaster Recovery via _recovery_state）：
   若系统在物理写入中途遭遇断电崩溃，重启后 `recover` 能够遍历所有可能的执行步数，
   自动推导磁盘当前状态停留在哪一步，并安全反向补偿还原。
6. 结构化差异行与全端视觉还原（Byte-for-Byte Visual Determinism via PreviewLine）：
   通过 `plan_preview_lines` 与 `recovery_preview_lines` 将 Diff 计算与 Rich 终端解耦，
   使 Web API、GUI 前端与命令行能够 100% 保持一致的高保真彩色呈现。
"""

import difflib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from uuid import uuid4
from typing import Callable, Sequence

from rich.console import Console

from obsai.errors import (
    CollisionError, ConflictError, RecoveryRequiredError, SafeWriteError,
    TransactionError,
)
from obsai.safe_write.models import ChangeSet, FileChange, PreviewLine
from obsai.safe_write.service import (
    SafeWriteService, _encode, _hash, _sync_directory, change_preview_lines, patch_frontmatter,
)
from obsai.transactions.backlinks import rewrite_explicit_links
from obsai.transactions.journal import TransactionJournal, UNFINISHED, journal_base, list_journals
from obsai.transactions.models import TransactionOperation, TransactionPlan, TransactionResult
from obsai.vault.scanner import scan_markdown_files
from obsai.shutdown import check_shutdown, defer_shutdown, is_process_stop


def plan_preview_lines(plan: TransactionPlan) -> list[PreviewLine]:
    """为整场事务方案生成统一的结构化差异预览行列表。

    遍历方案中的全部物理变更提案，将 Diff 格式化为携带 Rich 样式提示的 PreviewLine 对象；
    若包含被保守跳过的模糊短双链（ambiguous_backlinks），追加黄色提示行。
    """
    lines: list[PreviewLine] = []
    for change in plan.changes:
        lines.extend(change_preview_lines(ChangeSet(change)))
    if plan.ambiguous_backlinks:
        lines.append(PreviewLine("Ambiguous WikiLinks left unchanged in:", "yellow"))
        lines.extend(PreviewLine(f"  {path}") for path in plan.ambiguous_backlinks)
    return lines


def recovery_preview_lines(service: "TransactionService", transaction_id: str) -> list[PreviewLine]:
    """生成从“当前实际磁盘状态”向“崩溃前快照原始状态”回滚的高保真差异预览行。

    用于在用户执行 `obsai transaction recover` 批准前，直观审阅回滚将对磁盘做出的全部反向修改。
    """
    journal = TransactionJournal.load(service.root, transaction_id)
    if journal.data["status"] not in UNFINISHED:
        raise TransactionError(f"Transaction {transaction_id} is not awaiting Vault recovery")
    originals, _, _ = service._recovery_state(journal)
    lines: list[PreviewLine] = []
    for path, original in sorted(originals.items()):
        current = service._current(path)
        if current == original:
            continue
        old_lines = (current or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        new_lines = (original or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
        for line in difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}" if current is not None else "/dev/null",
            tofile=f"b/{path}" if original is not None else "/dev/null",
        ):
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else "cyan" if line.startswith("@@") else None
            lines.append(PreviewLine(line.rstrip("\n"), style, highlight=False))
    return lines


class TransactionService:
    """多文件事务编排服务。

    负责全库多笔记事务的意图规划（Plan）、物理前置安全审查（Preflight）、
    预写日志与两阶段提交执行（Execute）、断电崩溃状态对齐与灾难自愈（Recover）。
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        database_path: Path | None = None,
        indexer: Callable[[Path], object] | None = None,
    ):
        """初始化事务服务。

        Args:
            vault_root: 目标 Vault 知识库根目录。
            database_path: 可选的 SQLite 衍生索引数据库路径。
            indexer: 可选的索引刷新回调函数。
        """
        self.safe = SafeWriteService(vault_root)
        self.root = self.safe.root
        self.database_path = database_path
        self.indexer = indexer

    @staticmethod
    def journals(vault_root: Path) -> list[dict]:
        """静态工具方法：扫描并列出目标知识库下的所有未竟或历史事务日志。"""
        root = vault_root.expanduser().resolve(strict=True)
        return list_journals(root)

    def _ensure_available(self) -> None:
        """检查知识库是否存在未完成的未决事务，若存在则立即阻断所有新操作。"""
        pending = [item for item in list_journals(self.root) if item["status"] in UNFINISHED]
        if pending:
            ids = ", ".join(item["id"] for item in pending)
            raise RecoveryRequiredError(
                f"Recovery required for transaction(s) {ids}; run 'obsai transaction recover ID'"
            )

    def ensure_ready(self) -> None:
        """公开门禁方法：当存在未决事务时阻断新写入或重新索引。"""
        self._ensure_available()

    def _path(self, relative: str, *, internal: bool = False) -> Path:
        """通过 SafeWriteService 解析并严格校验相对物理路径。"""
        return self.safe.path(relative, internal=internal)

    def plan(self, operations: Sequence[TransactionOperation]) -> TransactionPlan:
        """编译一组高层事务操作意图，推导出零副作用的纯内存事务执行方案（TransactionPlan）。

        规划推导流程：
        1. 确保当前库无阻塞的未决事务；
        2. 建立虚拟文件状态追踪表（virtual）、初始文件字节快照表（originals）及 POSIX 模式表（original_modes）；
        3. 顺序模拟执行每项操作：
           - create: 校验当前虚拟状态为空，更新虚拟状态为新内容；
           - replace / append / frontmatter / rewrite_backlinks:
             校验文件虚拟存在，执行精确替换或追加，记录 original_hash，更新虚拟状态；
           - move / trash:
             校验源存在、目标不存在且目标路径不同，自动推导受影响的反链，更新源虚拟状态为 None，目标为源内容；
        4. 统计事务执行将产生的新建父目录树集合（absent_directories）；
        5. 打包返回强类型且不可变的 TransactionPlan。

        Args:
            operations: 高层抽象操作意图序列。

        Returns:
            完全准备就绪的 TransactionPlan 方案。

        Raises:
            TransactionError / CollisionError / ConflictError / SafeWriteError: 意图参数冲突或语法不合法。
        """
        self._ensure_available()
        if not operations:
            raise TransactionError("Transaction needs at least one operation")
        virtual: dict[str, str | None] = {}
        originals: dict[str, bytes | None] = {}
        original_modes: dict[str, int | None] = {}
        changes: list[FileChange] = []

        def state(path: str, *, internal: bool = False) -> str | None:
            file = self._path(path, internal=internal)
            if path not in virtual:
                if file.exists() or file.is_symlink():
                    content, _ = self.safe._read(file)
                    originals[path] = _encode(content)
                    original_modes[path] = stat.S_IMODE(file.stat().st_mode)
                    virtual[path] = content
                else:
                    self.safe._vacant(file)
                    originals[path] = None
                    original_modes[path] = None
                    virtual[path] = None
            return virtual[path]

        for operation in operations:
            path = operation.path
            current = state(path)
            if operation.kind == "create":
                if current is not None or operation.new is None:
                    raise CollisionError(f"Cannot create occupied or empty proposal: {path}")
                _encode(operation.new)
                changes.append(FileChange("create", path, None, None, None, operation.new))
                virtual[path] = operation.new
            elif operation.kind in ("replace", "append", "rewrite_backlinks", "frontmatter"):
                if current is None:
                    raise ConflictError(f"Note disappeared: {path}")
                if operation.kind == "frontmatter":
                    updated = patch_frontmatter(current, operation.updates)
                    kind = "frontmatter"
                elif operation.kind == "append":
                    if not operation.new:
                        raise SafeWriteError(f"Append requires nonempty content: {path}")
                    updated = current + operation.new
                    kind = "update"
                elif operation.kind == "replace":
                    if not operation.old or current.count(operation.old) != 1 or operation.new is None:
                        raise SafeWriteError(f"Exact replacement must match once: {path}")
                    updated = current.replace(operation.old, operation.new, 1)
                    kind = "update"
                else:
                    if operation.old != current or operation.new is None:
                        raise ConflictError(f"Backlink source changed during planning: {path}")
                    updated = operation.new
                    kind = "update"
                if updated == current:
                    raise SafeWriteError(f"No change to note: {path}")
                _encode(updated)
                changes.append(FileChange(kind, path, None, _hash(_encode(current)), current, updated))
                virtual[path] = updated
            elif operation.kind in ("move", "trash"):
                if current is None:
                    raise ConflictError(f"Note disappeared: {path}")
                destination = operation.destination
                if operation.kind == "trash":
                    destination = f".obsai-trash/{uuid4().hex}/{path}"
                if not destination or destination == path:
                    raise TransactionError("Move requires a distinct destination")
                if state(destination, internal=operation.kind == "trash") is not None:
                    raise CollisionError(f"Destination already exists: {destination}")
                impacts = self.safe._backlink_impact(path) if operation.kind == "move" else ()
                changes.append(FileChange(
                    operation.kind, path, destination, _hash(_encode(current)),
                    current, current if operation.kind == "move" else None, impacts,
                ))
                virtual[path] = None
                virtual[destination] = current
            else:
                raise TransactionError(f"Unknown transaction operation: {operation.kind}")

        finals = {path: _encode(value) if value is not None else None for path, value in virtual.items()}
        absent_dirs: set[str] = set()
        for path in originals:
            parent = self._path(path, internal=path.startswith(".obsai-trash/")).parent
            while parent != self.root and not parent.exists():
                absent_dirs.add(parent.relative_to(self.root).as_posix())
                parent = parent.parent
        return TransactionPlan(
            str(self.root),
            tuple(operations),
            tuple(changes),
            originals,
            finals,
            original_modes,
            tuple(sorted(absent_dirs, key=lambda item: (item.count("/"), item))),
        )

    def plan_move_with_backlinks(self, path: str, destination: str) -> TransactionPlan:
        """为单篇笔记的移动规划事务方案，自动级联改写全库显式反向链接。"""
        return self.plan_moves_with_backlinks([(path, destination)])

    def plan_moves_with_backlinks(
        self,
        moves: Sequence[tuple[str, str]],
        extra_operations: Sequence[TransactionOperation] = (),
    ) -> TransactionPlan:
        """为批量笔记移动及关联反链级联改写规划统一的原子事务方案。

        处理流程：
        1. 校验入参：拒绝重复移动同一源文件或多个移动竞争同一目标；
        2. 遍历知识库中所有包含 `[[` 的 Markdown 文件；
        3. 针对每个文件调用 `rewrite_explicit_links`，安全改写指向旧路径的显式 WikiLink；
        4. 将改写操作作为 `rewrite_backlinks` 操作追加进事务；
        5. 将模糊短链文件路径收集进 `ambiguous_backlinks` 供用户审查；
        6. 调用 `self.plan` 编译最终方案。
        """
        self._ensure_available()
        if not moves:
            raise TransactionError("At least one move is required")
        if len({source for source, _ in moves}) != len(moves):
            raise TransactionError("A source may be moved only once per transaction")
        if len({destination for _, destination in moves}) != len(moves):
            raise CollisionError("Two moves target the same destination")
        operations = []
        for path, destination in moves:
            source = self._path(path)
            self._path(destination)
            if not source.is_file():
                raise ConflictError(f"Note disappeared: {path}")
            operations.append(TransactionOperation.move(path, destination))
        ambiguous: set[str] = set()
        destination_by_source = dict(moves)
        for candidate in scan_markdown_files(self.root):
            source_path = candidate.relative_to(self.root).as_posix()
            content, _ = self.safe._read(candidate)
            if "[[" not in content:
                continue
            updated = content
            changed = False
            for path, destination in moves:
                if PurePosixPath(path).stem not in updated:
                    continue
                updated, count, ambiguous_count = rewrite_explicit_links(
                    updated, source_path, path, destination
                )
                changed |= bool(count)
                if ambiguous_count:
                    ambiguous.add(source_path)
            if changed:
                output_path = destination_by_source.get(source_path, source_path)
                operations.append(TransactionOperation(
                    "rewrite_backlinks", output_path, old=content, new=updated,
                ))
        operations.extend(extra_operations)
        plan = self.plan(operations)
        return TransactionPlan(
            plan.vault_root,
            plan.operations,
            plan.changes,
            plan.originals,
            plan.finals,
            plan.original_modes,
            plan.absent_directories,
            tuple(sorted(ambiguous)),
        )

    def preview(self, plan: TransactionPlan, console: Console) -> None:
        """在 Rich 控制台中输出整场事务的格式化差异对比预览。"""
        for line in plan_preview_lines(plan):
            console.print(line.text, style=line.style, markup=False, highlight=line.highlight)

    def preflight(self, plan: TransactionPlan) -> None:
        """物理落盘前的严苛预检（Preflight Verification）。

        校验防线：
        1. 校验方案归属当前 Vault；
        2. 临门一脚校验所有涉及文件的 CAS 原始内容哈希与可读权限；
        3. 向上回溯父目录，核验物理写与执行权限（W_OK | X_OK）；
        4. **跨设备文件系统边界防御**：比对移动源路径与目标路径的 `st_dev` 设备号，
           坚决阻断跨设备分区移动（跨分区硬链接会导致崩溃不一致）；
        5. **剩余磁盘空间安全红线**：根据快照大小与新写入大小，核验可用空间充足。

        Raises:
            TransactionError: 权限不足、跨设备分区、或磁盘空间告急。
        """
        self._ensure_available()
        if plan.vault_root != str(self.root):
            raise TransactionError("Transaction plan belongs to another Vault")
        for path, original in plan.originals.items():
            target = self._path(path, internal=path.startswith(".obsai-trash/"))
            if original is None:
                self.safe._vacant(target)
            else:
                self.safe._check_current(target, _hash(original))
                if not os.access(target, os.R_OK):
                    raise TransactionError(f"Note is not readable: {path}")
            ancestor = target.parent
            while not ancestor.exists() and ancestor != self.root:
                ancestor = ancestor.parent
            if not os.access(ancestor, os.W_OK | os.X_OK):
                raise TransactionError(f"Directory is not writable: {ancestor}")
        for change in plan.changes:
            if change.operation in ("move", "trash"):
                assert change.destination is not None
                source = self._path(change.path, internal=change.path.startswith(".obsai-trash/"))
                target_parent = self._path(
                    change.destination, internal=change.operation == "trash"
                ).parent
                source_parent = source if source.exists() else source.parent
                while not source_parent.exists():
                    source_parent = source_parent.parent
                while not target_parent.exists():
                    target_parent = target_parent.parent
                if source_parent.stat().st_dev != target_parent.stat().st_dev:
                    raise TransactionError("Move crosses filesystem devices")
        required = sum(len(content) for content in plan.originals.values() if content is not None)
        required += sum(len(content) for content in plan.finals.values() if content is not None)
        if shutil.disk_usage(self.root).free < required + 4096:
            raise TransactionError("Insufficient free space for transaction snapshots and writes")

    def _current(self, path: str) -> bytes | None:
        """读取指定物理相对路径的当前二进制字节流，不存在返回 None。"""
        target = self._path(path, internal=path.startswith(".obsai-trash/"))
        if not target.exists() and not target.is_symlink():
            return None
        if not stat.S_ISREG(target.lstat().st_mode):
            raise RecoveryRequiredError(f"Transaction path is no longer a regular file: {path}")
        return target.read_bytes()

    @staticmethod
    def _state_after(
        originals: dict[str, bytes | None], changes: Sequence[FileChange], count: int
    ) -> dict[str, bytes | None]:
        """推导在顺序执行了前 count 步物理变更后，理论上文件系统所处的预期状态快照。"""
        state = dict(originals)
        for change in changes[:count]:
            if change.operation in ("move", "trash"):
                assert change.destination is not None
                state[change.destination] = state[change.path]
                state[change.path] = None
            else:
                state[change.path] = _encode(change.new_content or "")
        return state

    @staticmethod
    def _changed_paths(changes: Sequence[FileChange]) -> set[str]:
        """提取变更序列所涉及的所有相对物理路径集合（包含源路径与目标路径）。"""
        paths = set()
        for change in changes:
            paths.add(change.path)
            if change.destination is not None:
                paths.add(change.destination)
        return paths

    def _verify(self, expected: dict[str, bytes | None], paths: set[str] | None = None) -> None:
        """核验物理磁盘上的真实文件字节是否与预期状态完全一致。"""
        for path in sorted(paths if paths is not None else expected):
            if self._current(path) != expected[path]:
                raise RecoveryRequiredError(f"Transaction file differs from expected state: {path}")

    def _rollback(
        self,
        originals: dict[str, bytes | None],
        original_modes: dict[str, int | None],
        changes: Sequence[FileChange],
        applied_count: int,
        absent_directories: Sequence[str],
    ) -> None:
        """执行事务物理回滚。

        回滚操作步骤：
        1. 核对磁盘状态处于预期的 `_state_after(..., applied_count)` 断点；
        2. 先还原原始存在的文件：调用原子写入还原字节流，并恢复原 POSIX mode；
        3. 再清理事务新建的文件：unlink 删除并刷盘父目录项；
        4. 逆序清理事务新建的空父目录（从深层到浅层）。
        """
        paths = self._changed_paths(changes[:applied_count])
        expected = self._state_after(originals, changes, applied_count)
        self._verify(expected, paths)
        # 先还原原始文件，再清理新生成文件
        for path in sorted(paths):
            original = originals[path]
            if original is None:
                continue
            current = self._current(path)
            if current == original:
                continue
            target = self._path(path, internal=path.startswith(".obsai-trash/"))
            self.safe._atomic_write(
                target, original, expected_hash=_hash(current) if current is not None else None
            )
            mode = original_modes[path]
            if mode is not None:
                target.chmod(mode)
        for path in sorted(paths):
            if originals[path] is None and self._current(path) is not None:
                target = self._path(path, internal=path.startswith(".obsai-trash/"))
                target.unlink()
                _sync_directory(target.parent)
        for directory in sorted(absent_directories, key=lambda item: item.count("/"), reverse=True):
            relative = PurePosixPath(directory)
            target = self.root / directory
            if (
                relative.is_absolute()
                or any(part in (".", "..") for part in directory.split("/"))
                or not target.resolve(strict=False).is_relative_to(self.root)
            ):
                raise RecoveryRequiredError(f"Unsafe transaction directory: {directory}")
            try:
                target.rmdir()
            except OSError:
                pass

    def _cleanup(self, journal: TransactionJournal) -> None:
        """安全物理移除已成功终结（complete / rolled_back）的事务日志目录。"""
        try:
            shutil.rmtree(journal.directory)
        except OSError:
            return
        try:
            journal_base(self.root).rmdir()
        except OSError:
            pass

    def _reindex(self, plan: TransactionPlan) -> None:
        """在物理文件成功提交后，驱动衍生元数据索引更新。"""
        if self.indexer is not None:
            self.indexer(self.root)
        elif self.database_path is not None:
            from obsai.indexing import IncrementalIndexer
            from obsai.storage import Database, IndexRepository

            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with Database(self.database_path) as database:
                repository = IndexRepository(database)
                with database.transaction():
                    # 针对移动操作预先在索引库更新路径，保持实体稳定 ID
                    for change in plan.changes:
                        if change.operation == "move" and change.destination is not None:
                            existing = repository.notes.get_by_path(change.path)
                            if existing is not None:
                                repository.notes.update_path(existing.id, change.destination)
                    IncrementalIndexer(repository).update(self.root)

    def _mark_index_dirty(self, paths: list[str], reason: str) -> None:
        """当索引更新异常时，尽力向 SQLite 写入脏数据队列标记。"""
        if self.database_path is None:
            return
        try:
            from obsai.storage import Database, IndexRepository

            with Database(self.database_path) as database:
                repository = IndexRepository(database)
                for path in paths:
                    repository.mark_dirty(path, reason)
        except Exception:
            # 即使 SQLite 自身不可用，磁盘上的 journal.json 依旧是权威记录
            pass

    def execute(self, plan: TransactionPlan, *, approved: bool) -> TransactionResult:
        """执行事务物理落地与两阶段提交。

        受人工确认守卫（approved）保护。完整流转：
        1. preflight 前置安全检查；
        2. 持久化创建 WAL 预写日志与快照（status='prepared'）；
        3. 标记 status='applying'，受 defer_shutdown 保护逐个应用变更并累加 applied_count；
        4. 核验终态文件字节完全对齐（_verify），标记 status='committed'；
        5. 触发衍生索引更新（_reindex）：
           - 若索引失败，绝不回滚物理文件，标记 status='index_dirty'，返回 committed=True, index_dirty=True；
        6. 索引成功，标记 status='complete' 并安全清理日志目录。

        Args:
            plan: 经过预审的事务执行方案。
            approved: 是否获得用户或策略显式批准。

        Returns:
            表征最终提交状态的 TransactionResult 对象。
        """
        if not approved:
            return TransactionResult(None, committed=False, cancelled=True)
        self.preflight(plan)
        check_shutdown()
        transaction_id = uuid4().hex
        try:
            with defer_shutdown():
                journal = TransactionJournal.create(self.root, transaction_id, plan)
        except OSError as exc:
            raise TransactionError(f"Cannot prepare transaction snapshots: {exc}") from exc
        applied_count = 0
        try:
            check_shutdown()
            journal.update(status="applying")
            for change in plan.changes:
                check_shutdown()
                with defer_shutdown():
                    self.safe.apply(ChangeSet(change), approved=True)
                    applied_count += 1
                    journal.update(applied_count=applied_count)
                check_shutdown()
            self._verify(plan.finals)
            check_shutdown()
            journal.update(status="committed")
        except BaseException as exc:
            with defer_shutdown():
                try:
                    journal.update(status="rolling_back", error=f"{type(exc).__name__}: {exc}")
                    self._rollback(
                        plan.originals,
                        plan.original_modes,
                        plan.changes,
                        applied_count,
                        plan.absent_directories,
                    )
                    journal.update(status="rolled_back")
                    self._cleanup(journal)
                except BaseException as rollback_exc:
                    journal.update(status="recovery_required", error=f"{type(rollback_exc).__name__}: {rollback_exc}")
                    raise RecoveryRequiredError(
                        f"Rollback failed for {transaction_id}; run 'obsai transaction recover {transaction_id}'"
                    ) from rollback_exc
            # 物理文件已完整恢复为初始状态；向外直接抛出进程中止异常
            if is_process_stop(exc):
                raise
            raise TransactionError(f"Transaction failed and was rolled back: {exc}") from exc

        try:
            check_shutdown()
            self._reindex(plan)
        except BaseException as exc:
            paths = sorted(self._changed_paths(plan.changes))
            reason = f"Index update failed after Vault commit: {type(exc).__name__}: {exc}"
            with defer_shutdown():
                journal.update(status="index_dirty", dirty_paths=paths, error=reason)
                self._mark_index_dirty(paths, reason)
            if is_process_stop(exc):
                raise
            return TransactionResult(transaction_id, committed=True, index_dirty=True, index_error=reason)
        with defer_shutdown():
            journal.update(status="complete")
            self._cleanup(journal)
        return TransactionResult(transaction_id, committed=True)

    def recover(self, transaction_id: str, *, approved: bool) -> TransactionResult:
        """从意外中断的事务中执行断点推导与灾难恢复（Rollback Recovery）。

        受人工确认守卫（approved）保护。
        调用 `_recovery_state` 推导中断时的确切断点步数，执行回滚并清理日志。
        """
        if not approved:
            return TransactionResult(transaction_id, committed=False, cancelled=True)
        journal = TransactionJournal.load(self.root, transaction_id)
        if journal.data["status"] not in UNFINISHED:
            raise TransactionError(f"Transaction {transaction_id} is not awaiting Vault recovery")
        originals, changes, count = self._recovery_state(journal)
        original_modes = {item["path"]: item.get("mode") for item in journal.data["originals"]}
        journal.update(status="rolling_back")
        try:
            self._rollback(
                originals,
                original_modes,
                changes,
                count,
                journal.data.get("absent_directories", []),
            )
        except Exception as exc:
            journal.update(status="recovery_required", error=f"{type(exc).__name__}: {exc}")
            raise RecoveryRequiredError(f"Recovery failed; inspect {journal.directory}") from exc
        journal.update(status="rolled_back")
        self._cleanup(journal)
        return TransactionResult(transaction_id, committed=False)

    def _recovery_state(
        self, journal: TransactionJournal,
    ) -> tuple[dict[str, bytes | None], tuple[FileChange, ...], int]:
        """推导磁盘当前实际状态对应中断事务的哪一步已应用断点。

        遍历从第 0 步到第 N 步的所有可能理论状态，比对当前物理文件。
        若无法与任何已知状态对齐，抛出 RecoveryRequiredError，防止在脏数据上错误回滚。
        """
        originals = journal.originals()
        changes = tuple(FileChange(**item) for item in journal.data["changes"])
        actual = {path: self._current(path) for path in originals}
        matching = []
        for count in range(len(changes) + 1):
            if actual == self._state_after(originals, changes, count):
                matching.append(count)
        if not matching:
            raise RecoveryRequiredError(
                f"Files no longer match a known state for {journal.data['id']}; inspect {journal.directory}"
            )
        return originals, changes, max(matching)

    def preview_recovery(self, transaction_id: str, console: Console) -> None:
        """在 Rich 控制台中展示从当前磁盘状态回滚到快照原始状态的精确 Diff。"""
        for line in recovery_preview_lines(self, transaction_id):
            console.print(line.text, style=line.style, markup=False, highlight=line.highlight)

    def clear_index_dirty(self) -> None:
        """在全量增量索引成功修复后，清理残留的 index_dirty 状态与日志。"""
        for item in list_journals(self.root):
            if item["status"] in ("index_dirty", "committed"):
                if self.database_path is not None:
                    from obsai.storage import Database, IndexRepository

                    with Database(self.database_path) as database:
                        repository = IndexRepository(database)
                        for path in item.get("dirty_paths", []):
                            repository.clear_dirty(path)
                self._cleanup(TransactionJournal.load(self.root, item["id"]))
