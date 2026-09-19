"""Hash-verified incremental index with conservative exact rename matching.

该模块是知识库增量索引引擎（IncrementalIndexer）的核心实现，
负责将 Obsidian 物理知识库（Vault）中的 Markdown 笔记高效、增量、原子地同步到 SQLite 数据库。

核心架构与设计原则：
1. 哈希指纹增量校验（Hash-Verified Incremental Fact Checking）：
   通过数据库轻量级事实（list_index_facts）与磁盘文件的 SHA-256 哈希比对，
   对未修改（unchanged）的笔记实现毫秒级跳过，避免不必要的文件重读、Markdown 重解析与切片向量计算。
2. 保守精确重命名/移动推导（Conservative 1:1 Rename/Move Matching）：
   通过对消失文件与新增文件的 content_hash 进行倒排索引匹配，能够智能识别重命名与跨目录移动。
   - 歧义消除守卫（Disambiguation Guard）：严格要求旧哈希与新哈希必须为 1 对 1 唯一映射；
     若出现多份相同内容（如复制文件或空模板），宁可保守地作为“删除+新建”处理，绝不盲目猜测继承 ID；
   - 核心收益：成功重命名时保留原始 note_id，避免关联的向量切片失效或耗费 API 费用重新生成。
3. 符号链接穿透防御（Symlink Traversal Defense）：
   _read_note 显式拒绝读取符号链接（is_symlink），坚决防止通过软链接逃逸到 Vault 目录之外窃取系统文件。
4. 写时并发内容漂移检测（Read-Time Drift Detection）：
   在落库解析阶段（parse_changed）再次校验文件哈希是否与扫描时一致。
   若用户在同步期间正在编辑文件，立即抛出 VaultError 触发事务回滚，绝不写入不一致的脏状态。
5. 单事务全量原子提交（Single Transaction Atomicity）：
   所有删除（deleted）、路径变更（renamed/moved）、新笔记写入（modified/created）与全局双链重对齐
   均在一个 SQLite 物理事务中执行，失败时自动完整回滚，确保索引的强一致性。
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from obsai.chunking import chunk_note
from obsai.errors import VaultError
from obsai.storage import IndexRepository, LinkImpact
from obsai.vault import scan_markdown_files
from obsai.vault.models import ParsedNote
from obsai.vault.parser import parse_markdown
from obsai.shutdown import check_shutdown

# 笔记变更状态生命周期字面量类型
ChangeKind = Literal["unchanged", "created", "modified", "deleted", "renamed", "moved"]


@dataclass(frozen=True)
class FileChange:
    """单个笔记文件的增量变动明细报告实体。"""

    kind: ChangeKind                           # 变动形态（6 态之一）
    path: str                                  # 变动后的当前相对路径
    note_id: str | None = None                 # 笔记持久化唯一 ID（删除时为旧 ID，新建时为新 ID）
    old_path: str | None = None                # 变动前的原相对路径（仅 renamed 与 moved 有效）
    affected_links: tuple[LinkImpact, ...] = () # 受本次重命名/移动影响的反向链接元组


@dataclass(frozen=True)
class UpdateResult:
    """增量索引同步的汇总报告实体。"""

    changes: tuple[FileChange, ...]  # 本轮扫描并处理的所有文件变更列表（含 unchanged）

    def count(self, kind: ChangeKind) -> int:
        """统计指定变更类型的文件总数。"""
        return sum(change.kind == kind for change in self.changes)

    @property
    def affected_link_count(self) -> int:
        """统计因笔记重命名或移动而受影响的反向链接总数。"""
        return sum(len(change.affected_links) for change in self.changes)


def _hash(content: str) -> str:
    """计算文本内容的 SHA-256 Hex 哈希值。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_note(path: Path) -> str:
    """以 UTF-8 编码安全读取笔记内容，严格拒绝符号链接。

    :param path: 待读取的文件物理路径
    :return: 文本内容
    :raises VaultError: 若文件为符号链接或发生 IO / 解码故障
    """
    # 安全边界：拒绝读取符号链接，防止路径穿越攻击
    if path.is_symlink():
        raise VaultError(f"Refusing to read symlinked note: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VaultError(f"Cannot read note {path}: {exc}") from exc


class IncrementalIndexer:
    """增量索引器，负责比对文件系统与 SQLite 数据库，并原子应用增量变更。"""

    def __init__(self, repository: IndexRepository):
        """初始化增量索引器。

        :param repository: 索引数据库仓储
        """
        self.repository = repository

    def update(self, vault_root: Path) -> UpdateResult:
        """以物理 Vault 为唯一事实源，执行哈希校验并将增量变动原子写入数据库。

        执行阶段：
        1. 磁盘全量扫描与哈希比对，初筛出新增、修改与未变更文件；
        2. 基于内容哈希对消失文件与新增文件执行 1 对 1 保守重命名/移动推导；
        3. 开启数据库物理事务，按顺序处理删除、移动、修改与新建；
        4. 写时防漂移校验，发现并发修改立即抛出异常触发事务回滚；
        5. 全局反向链接重对齐并记录更新时间戳。

        :param vault_root: 知识库物理根目录
        :return: 包含所有变更明细的 UpdateResult 实体
        :raises VaultError: 读写冲突或文件异常时抛出
        """
        # 扫描磁盘上所有有效的 Markdown 文件
        files = scan_markdown_files(vault_root)
        root = vault_root.resolve(strict=True)
        # 从数据库中拉取轻量事实记录：path -> NoteFact(path, id, content_hash)
        previous = {record.path: record for record in self.repository.notes.list_index_facts()}
        current_paths: set[str] = set()
        new_hashes: dict[str, str] = {}
        modified_hashes: dict[str, str] = {}
        unchanged: list[FileChange] = []

        # 阶段 1：遍历磁盘文件并进行哈希初筛
        for path in files:
            check_shutdown()
            relative = path.relative_to(root).as_posix()
            current_paths.add(relative)
            content_hash = _hash(_read_note(path))
            record = previous.get(relative)
            if record is None:
                new_hashes[relative] = content_hash
            elif content_hash == record.content_hash:
                # 内容未变，毫秒级跳过，不重新解析和生成向量
                unchanged.append(FileChange("unchanged", relative, note_id=record.id))
            else:
                modified_hashes[relative] = content_hash

        # 阶段 2：基于哈希倒排索引推导重命名（renamed）与跨目录移动（moved）
        # 找出数据库中存在但在当前磁盘扫描中已消失的文件
        disappeared = {path: record for path, record in previous.items() if path not in current_paths}
        old_by_hash: dict[str, list[str]] = defaultdict(list)
        new_by_hash: dict[str, list[str]] = defaultdict(list)
        for path, record in disappeared.items():
            old_by_hash[record.content_hash].append(path)
        for path, content_hash in new_hashes.items():
            new_by_hash[content_hash].append(path)

        moves: list[FileChange] = []
        move_hashes: dict[str, str] = {}
        for content_hash, old_paths in old_by_hash.items():
            new_paths = new_by_hash.get(content_hash, [])
            # 歧义消除守卫：存在多个旧文件或多个新文件拥有相同哈希时（如空文件或复制的模板），
            # 匹配存在歧义，绝不随意继承 ID，保守放弃匹配，交由常规“删除+新建”处理
            if len(old_paths) != 1 or len(new_paths) != 1:
                continue
            old_path, new_path = old_paths[0], new_paths[0]
            record = disappeared.pop(old_path)
            new_hashes.pop(new_path)
            move_hashes[new_path] = content_hash
            # 若父目录一致则仅为重命名，若父目录不一致则为跨目录移动
            kind: ChangeKind = (
                "renamed"
                if PurePosixPath(old_path).parent == PurePosixPath(new_path).parent
                else "moved"
            )
            # 获取该笔记改名前受影响的所有反向链接
            impacts = tuple(self.repository.backlinks_for_path(record.id, old_path))
            moves.append(FileChange(kind, new_path, record.id, old_path, impacts))

        # 阶段 3：带内容防漂移断言的 Markdown 安全解析闭包
        def parse_changed(path: str, expected_hash: str) -> ParsedNote:
            content = _read_note(root / path)
            # 若哈希发生漂移，说明用户在索引更新过程中并发修改了该文件，立即报错回滚事务
            if _hash(content) != expected_hash:
                raise VaultError(f"Note changed during index update: {path}")
            return parse_markdown(content, path)

        changes: list[FileChange] = [*unchanged]

        # 阶段 4：单一数据库物理事务提交
        with self.repository.db.transaction():
            # 4.1 处理物理删除的笔记
            for path, record in sorted(disappeared.items()):
                check_shutdown()
                if (root / path).exists():
                    raise VaultError(f"Note reappeared during index update: {path}")
                self.repository.notes.delete(record.id)
                self.repository.clear_dirty(path)
                changes.append(FileChange("deleted", path, note_id=record.id))

            # 4.2 处理重命名与移动的笔记（无缝继承原始 note_id）
            for move in sorted(moves, key=lambda item: item.path):
                check_shutdown()
                if _hash(_read_note(root / move.path)) != move_hashes[move.path]:
                    raise VaultError(f"Note changed during index update: {move.path}")
                assert move.note_id is not None
                self.repository.notes.update_path(move.note_id, move.path)
                self.repository.clear_dirty(move.path)
                changes.append(move)

            # 4.3 处理内容修改的笔记
            for path in sorted(modified_hashes):
                check_shutdown()
                parsed = parse_changed(path, modified_hashes[path])
                note_id = self.repository.index_note(
                    parsed, chunk_note(parsed), reconcile=False
                )
                changes.append(FileChange("modified", path, note_id=note_id))

            # 4.4 处理新创建的笔记
            for path in sorted(new_hashes):
                check_shutdown()
                parsed = parse_changed(path, new_hashes[path])
                note_id = self.repository.index_note(
                    parsed, chunk_note(parsed), reconcile=False
                )
                changes.append(FileChange("created", path, note_id=note_id))

            check_shutdown()
            # 4.5 全局双向链接重对齐（若发生结构性增删改，触发 full 全量对齐）
            self.repository.reconcile_links(full=bool(moves or disappeared or new_hashes))
            # 4.6 记录 UTC 最后更新时间戳状态
            self.repository.set_state("last_update", datetime.now(timezone.utc).isoformat())

        # 按路径与变动类型稳定排序并返回汇总报告
        return UpdateResult(tuple(sorted(changes, key=lambda item: (item.path, item.kind))))
