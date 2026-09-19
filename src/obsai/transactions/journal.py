"""持久化、可审查的事务日志与短生命周期二进制快照引擎（Transaction Journal & Snapshots）。

核心设计哲学与安全架构：
1. 预写日志与崩溃一致性（Write-Ahead Logging & Crash-Consistency）：
   在对知识库（Vault）物理磁盘进行任何批量或级联修改前，事务引擎必须先在 `.obsai-transactions/{transaction_id}/`
   中完成全部原始文件的物理快照持久化并生成结构化 `journal.json`。若进程意外崩溃或掉电，
   重启时系统能基于日志 100% 精确还原断点或执行原子回滚。
2. 字节级短生命周期快照（Byte-Level Binary Snapshots）：
   所有待修改或待删除的源文件原始字节，被顺序写入 `snapshots/{number}.bin`，
   并在物理层面执行 `fsync` 刷盘；同时在日志元数据中记录原始文件的 SHA-256 哈希值与 POSIX 文件权限位（mode），
   使得回滚操作不仅能精确还原正文内容，还能还原文件权限模式。
3. 严密的状态机流转控制（Finite State Machine Transitions）：
   - 未决状态集合（UNFINISHED）：
     `prepared`（就绪） -> `applying`（应用中） -> `rolling_back`（回滚中） -> `recovery_required`（必须人工/工具介入恢复）；
   - 终态与索引状态集合：
     `committed`（磁盘物理写入已提交） -> `index_dirty`（物理成功但索引待补偿） -> `complete`（全流程圆满完成）或 `rolled_back`（已彻底回滚）。
4. 工业级原子保存与双重 fsync（Atomic Persistence & Directory Durability）：
   `journal.json` 的更新通过同目录 `.journal-*.tmp` 临时文件执行，写入后通过 `os.fsync` 刷盘，
   再通过 `os.replace` 原子替换，最后对父目录执行 `_sync_directory`，彻底避免半写损坏（Torn Writes）。
5. 符号链接穿透防御与内容校验（Symlink Resistance & Checksum Verification）：
   严密检测日志目录、子目录、快照文件及父目录的符号链接（Symlink），坚决防范软链接提权攻击；
   在回滚读取快照前，强制校验二进制字节流的 SHA-256 与日志中记录的 `hash`，防止快照损坏。
"""

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from obsai.errors import RecoveryRequiredError, TransactionError
from obsai.safe_write.service import _hash, _sync_directory
from obsai.transactions.models import TransactionPlan

JOURNAL_DIR = ".obsai-transactions"
"""事务日志在 Vault 根目录下的保留物理目录名。"""

UNFINISHED = frozenset({"prepared", "applying", "rolling_back", "recovery_required"})
"""代表事务尚未安全终结、可能处于崩溃中断或需要恢复的未决状态集合。"""

KNOWN_STATUSES = UNFINISHED | {"committed", "index_dirty", "complete", "rolled_back"}
"""系统所允许识别的全部合法事务状态枚举集合。"""


def journal_base(root: Path) -> Path:
    """获取并校验事务日志根目录路径（拒绝符号链接）。

    Args:
        root: Vault 知识库根物理目录。

    Returns:
        指向 `.obsai-transactions` 的 Path 对象。

    Raises:
        TransactionError: 当日志根目录是一个符号链接时抛出（防止越权攻击）。
    """
    base = root / JOURNAL_DIR
    if base.is_symlink():
        raise TransactionError("Transaction journal directory must not be a symlink")
    return base


def list_journals(root: Path) -> list[dict]:
    """扫描并列出知识库中所有持久化的事务日志元数据。

    安全检查流程：
    1. 若日志根目录不存在，返回空列表；
    2. 验证根目录是合法物理目录（非软链接）；
    3. 遍历每个子事务目录，逐一验证非符号链接、且包含合法的 `journal.json`；
    4. 反序列化 JSON 并核验状态是否属于 `KNOWN_STATUSES`。

    Returns:
        按事务 ID 升序排列的各个事务日志数据字典列表。

    Raises:
        RecoveryRequiredError: 当发现损坏、被非法篡改或存在不安全符号链接的日志时抛出。
    """
    base = journal_base(root)
    if not base.exists():
        return []
    if not base.is_dir():
        raise RecoveryRequiredError(f"Transaction journal path is not a directory: {base}")
    journals = []
    for directory in sorted(base.iterdir()):
        if directory.is_symlink() or not directory.is_dir():
            raise RecoveryRequiredError(f"Unsafe transaction journal entry: {directory}")
        path = directory / "journal.json"
        if not path.is_file() or path.is_symlink():
            raise RecoveryRequiredError(f"Transaction journal missing or unsafe: {directory}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryRequiredError(f"Cannot read transaction journal: {path}") from exc
        if data.get("id") != directory.name or data.get("status") not in KNOWN_STATUSES:
            raise RecoveryRequiredError(f"Invalid transaction journal: {path}")
        journals.append(data)
    return journals


class TransactionJournal:
    """单个事务的预写日志与快照持久化管理器。

    控制单场事务生命周期内的快照创建、原子状态持久化、以及回滚前的数据完整性校验。
    """

    def __init__(self, root: Path, transaction_id: str):
        """初始化事务日志实例。

        Args:
            root: Vault 知识库根目录。
            transaction_id: 16 进制字符组成的唯一事务标识符。

        Raises:
            TransactionError: 当事务 ID 为空或包含非十六进制非法字符时抛出。
        """
        if not transaction_id or any(char not in "0123456789abcdef" for char in transaction_id):
            raise TransactionError("Invalid transaction ID")
        self.root = root
        self.directory = journal_base(root) / transaction_id
        self.path = self.directory / "journal.json"
        self.data: dict = {}

    @classmethod
    def create(cls, root: Path, transaction_id: str, plan: TransactionPlan) -> "TransactionJournal":
        """根据预先规划的事务方案（TransactionPlan）创建并持久化全新的预写日志与二进制快照。

        执行原子步骤：
        1. 创建事务专有目录 `.obsai-transactions/{transaction_id}/`；
        2. 创建 `snapshots/` 子目录；
        3. 遍历执行方案中需要修改或删除的原始文件：
           - 将其原始字节流写入 `snapshots/{number}.bin` 并调用 `fsync` 确保落盘；
           - 记录原始路径、SHA-256 哈希、快照相对路径及原始 POSIX 文件权限模式（mode）；
        4. 刷盘 `snapshots/` 目录项；
        5. 初始化日志数据结构（初始状态为 `prepared`，应用计数为 0，记录创建前尚不存在的目录以备回滚清理）；
        6. 原子保存 `journal.json`；
        7. 若初始化期间发生任何异常，彻底清理本事务目录并重新抛出异常。

        Args:
            root: Vault 根目录。
            transaction_id: 唯一事务 ID。
            plan: 经过 Dry-Run 分析与安全校验的事务执行方案。

        Returns:
            状态为 'prepared' 的就绪 TransactionJournal 实例。
        """
        journal = cls(root, transaction_id)
        journal.directory.mkdir(parents=True, exist_ok=False)
        try:
            snapshot_dir = journal.directory / "snapshots"
            snapshot_dir.mkdir()
            originals = []
            for number, (path, content) in enumerate(sorted(plan.originals.items())):
                snapshot = None
                if content is not None:
                    snapshot = f"snapshots/{number}.bin"
                    with (journal.directory / snapshot).open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                originals.append({
                    "path": path,
                    "hash": _hash(content) if content is not None else None,
                    "snapshot": snapshot,
                    "mode": plan.original_modes[path],
                })
            _sync_directory(snapshot_dir)
            journal.data = {
                "id": transaction_id,
                "status": "prepared",
                "applied_count": 0,
                "originals": originals,
                "changes": [asdict(change) for change in plan.changes],
                "absent_directories": list(plan.absent_directories),
                "dirty_paths": [],
                "error": None,
            }
            journal.save()
        except Exception:
            shutil.rmtree(journal.directory, ignore_errors=True)
            raise
        return journal

    @classmethod
    def load(cls, root: Path, transaction_id: str) -> "TransactionJournal":
        """从磁盘加载现有的事务日志，并执行严苛的安全与完整性校验。

        Args:
            root: Vault 根目录。
            transaction_id: 事务 ID。

        Returns:
            加载就绪的 TransactionJournal 实例。

        Raises:
            RecoveryRequiredError: 当目录或文件是符号链接、JSON 损坏、ID 不符或状态未知时抛出。
        """
        journal = cls(root, transaction_id)
        if journal.directory.is_symlink() or journal.path.is_symlink():
            raise RecoveryRequiredError(f"Unsafe transaction journal: {journal.path}")
        try:
            journal.data = json.loads(journal.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryRequiredError(f"Cannot read transaction journal: {journal.path}") from exc
        if journal.data.get("id") != transaction_id or journal.data.get("status") not in KNOWN_STATUSES:
            raise RecoveryRequiredError(f"Invalid transaction journal: {journal.path}")
        return journal

    def save(self) -> None:
        """执行工业级原子保存，将当前日志状态安全刷盘写入 `journal.json`。

        流程：
        1. 在当前事务目录下生成 `.journal-*.tmp` 临时文件；
        2. 将 JSON 文本序列化写入并刷新缓冲区；
        3. 调用底层 `os.fsync` 强制物理刷盘；
        4. 调用 `os.replace` 原子替换 `journal.json`；
        5. 调用 `_sync_directory` 刷盘父目录项，确保目录索引持久性；
        6. `finally` 块确保未竟临时文件绝对不残留。
        """
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".journal-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.data, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _sync_directory(self.directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def update(
        self,
        *,
        status: str | None = None,
        applied_count: int | None = None,
        error: str | None = None,
        dirty_paths: list[str] | None = None,
    ) -> None:
        """更新日志中的指定状态字段并立即执行原子刷盘保存。

        Args:
            status: 新的事务流转状态。
            applied_count: 已成功物理落盘的变更步数计数器。
            error: 故障发生时的错误摘要。
            dirty_paths: 崩溃或中断时已发生修改的脏路径列表。
        """
        if status is not None:
            self.data["status"] = status
        if applied_count is not None:
            self.data["applied_count"] = applied_count
        if error is not None:
            self.data["error"] = error
        if dirty_paths is not None:
            self.data["dirty_paths"] = dirty_paths
        self.save()

    def originals(self) -> dict[str, bytes | None]:
        """读取并严密校验全部原始快照数据，供事务回滚或状态审查使用。

        安全检验机制：
        1. 若快照文件存在，校验其物理路径必须符合 `snapshots/{number}.bin` 规范，且绝对不能是符号链接；
        2. 读取二进制字节流并计算其 SHA-256 哈希值；
        3. 比对计算得到的哈希与 `journal.json` 中记录的 `hash`；
           若哈希不一致，说明快照被外部破坏或损毁，立即抛出 `RecoveryRequiredError` 阻断错误回滚。

        Returns:
            字典映射：`{ 相对物理路径: 原始二进制内容（新建文件时为 None） }`。

        Raises:
            RecoveryRequiredError: 当快照路径非法、为符号链接或 SHA-256 校验失败时抛出。
        """
        states = {}
        for item in self.data["originals"]:
            snapshot = item["snapshot"]
            if snapshot is not None:
                candidate = self.directory / snapshot
                parts = Path(snapshot).parts
                if (
                    len(parts) != 2
                    or parts[0] != "snapshots"
                    or not parts[1].endswith(".bin")
                    or not parts[1][:-4].isdigit()
                    or candidate.is_symlink()
                    or candidate.parent.is_symlink()
                ):
                    raise RecoveryRequiredError(f"Unsafe transaction snapshot: {snapshot}")
                content = candidate.read_bytes()
            else:
                content = None
            if (None if content is None else _hash(content)) != item["hash"]:
                raise RecoveryRequiredError(f"Transaction snapshot is corrupt: {item['path']}")
            states[item["path"]] = content
        return states
