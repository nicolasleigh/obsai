"""可重建 SQLite 元数据索引仓储层（Repository API for the rebuildable SQLite metadata index）。

核心设计哲学与架构原则：
1. 索引的可衍生性与可重建性（Derived & Rebuildable Index）：
   SQLite 数据库中的所有表均是 Obsidian Markdown 文本物理文件的派生索引（Derived Cache）。
   任何数据损坏或丢失均可通过重新解析 Vault 完整重建，物理文件是唯一的真相之源（Single Source of Truth）。
2. 稳定标识符与重命名透明度（Stable IDs Across Path Renames）：
   每篇笔记在初次入库时被赋予唯一的 UUID（stable_id）。当发生文件重命名或路径移动时，
   系统更新 `notes.path` 及相关切片元数据，但严格保留其 `note_id` 和 `chunk_id`，
   避免重命名导致全量向量重新计算或外键引用断裂。
3. 强内容确定性的切片 ID（Deterministic Content-Addressed Chunk IDs）：
   切片 ID 由 `_hash(f"{note_id}:{chunk.position}:{chunk.content_hash}")` 唯一生成，
   具备内容寻址特征与顺序确定性，切片内容或顺序未变时哈希完全稳定。
4. 两阶段双链解析与对齐（Two-Stage Link Resolution & Reconciliation）：
   解析单篇笔记时，引用的目标笔记可能尚未被索引；
   通过 `_target_id` 执行前缀/后缀候选模糊匹配，并由 `reconcile_links` 在全库入库后统一重试解析，
   保证跨笔记维基双链（WikiLinks）关联的最终一致性。
5. 脏标记与增量同步队列（Dirty State Tracking）：
   内置 `dirty_notes` 队列与 `index_state` 键值状态表，支持文件系统变更监听（Watchdog）
   或 Git 提交比对后的优雅增量增量补偿。
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from obsai.chunking.models import Chunk
from obsai.storage.database import Database
from obsai.storage.fts import delete_fts_for_note, refresh_fts_for_note
from obsai.storage.vectors import SQLiteVectorStore, delete_vectors_for_note
from obsai.vault.models import Block, ParsedNote


def _now() -> str:
    """获取当前 UTC 时间的 ISO 8601 格式字符串（例如 '2026-09-19T13:20:00.000000+00:00'）。"""
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    """计算 UTF-8 编码文本的标准 SHA-256 十六进制哈希值。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    """序列化对象为确定性 JSON 文本（保留 Unicode 且键名按字典序升序排序）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class NoteRecord:
    """笔记元数据快照领域模型。

    封装 SQLite `notes` 表中的一行持久化记录，采用不可变数据类设计。
    """

    id: str
    """笔记在索引库中的唯一稳定标识符（UUID 或固定 ID）。"""

    path: str
    """笔记在 Vault 中的相对物理路径（例如 'Work/Meeting.md'）。"""

    title: str
    """笔记标题（来自 Frontmatter title 或正文首个一级标题，降级为文件名）。"""

    created_at: str
    """笔记被首次索引并记录到数据库的时间戳（ISO 8601 UTC）。"""

    modified_at: str
    """笔记内容发生实质改变时的时间戳（ISO 8601 UTC）。"""

    indexed_at: str
    """本次索引刷新的最新时间戳（ISO 8601 UTC）。"""

    content_hash: str
    """笔记全文原始文本的 SHA-256 内容哈希。"""

    frontmatter: dict
    """笔记头部 YAML Frontmatter 解析得到的属性字典。"""

    dataview_fields: dict[str, str]
    """从正文中提取的 Dataview 行内属性字典（如 [key:: value]）。"""


@dataclass(frozen=True)
class IndexedFile:
    """文件索引轻量事实元组（Lightweight Index Fact）。

    仅提取物理文件比对所需的最小字段集，用于增量扫描时极速判断文件的新增、修改与删除。
    """

    id: str
    """已索引笔记的稳定 ID。"""

    path: str
    """已索引笔记的相对路径。"""

    content_hash: str
    """已索引笔记的 SHA-256 内容哈希。"""


@dataclass(frozen=True)
class LinkRecord:
    """WikiLink 引用关系数据模型。

    记录一条从源笔记到目标笔记的双向链接细粒度信息。
    """

    source_note_id: str
    """发起引用的源笔记 ID。"""

    source_block_id: str | None
    """引用所在的源笔记块 ID（若链接位于具名块内，例如 `^block-1`）。"""

    target_path: str | None
    """WikiLink 中书写的原始目标路径（如 'Daily/2026-09-19'）。"""

    target_note_id: str | None
    """解析出的目标笔记 ID；若目标笔记尚未入库或为悬空链接，则为 None。"""

    target_heading: str | None
    """引用的目标子标题锚点（如 '#架构设计'）。"""

    target_block_id: str | None
    """引用的目标块级锚点（如 '#^block-xyz'）。"""

    display_text: str | None
    """WikiLink 的别名或展示文本（如 '[[Note|别名]]' 中的 '别名'）。"""

    is_embed: bool
    """是否为嵌入式引用（即以惊叹号开头的 `![[Note]]`）。"""

    position: int
    """该链接在源笔记的所有链接中的零基顺序位置。"""


@dataclass(frozen=True)
class DirtyNote:
    """待补偿处理的脏笔记队列项。"""

    path: str
    """需要重新索引或核对的目标笔记相对路径。"""

    note_id: str | None
    """已关联的笔记 ID（若该文件此前已入库，否则为 None）。"""

    reason: str
    """标记为脏状态的原因（例如 'file_modified', 'backlink_reconcile' 等）。"""

    marked_at: str
    """记录标记时间（ISO 8601 UTC）。"""


@dataclass(frozen=True)
class LinkImpact:
    """反向链接级联影响条目。

    用于在笔记移动或重命名时，向用户呈现或供事务级联重写的来源引用定位信息。
    """

    source_note_id: str
    """包含该引用的来源笔记 ID。"""

    source_path: str
    """来源笔记的相对物理路径。"""

    target_path: str | None
    """来源笔记中记录的原始目标路径。"""

    position: int
    """该引用在来源笔记中的链接序列号。"""


def _note_record(row: sqlite3.Row | None) -> NoteRecord | None:
    """将 SQLite 查询行字典安全映射为 NoteRecord 数据类实例。"""
    if row is None:
        return None
    return NoteRecord(
        id=row["id"],
        path=row["path"],
        title=row["title"],
        created_at=row["created_at"],
        modified_at=row["modified_at"],
        indexed_at=row["indexed_at"],
        content_hash=row["content_hash"],
        frontmatter=json.loads(row["frontmatter_json"]),
        dataview_fields=json.loads(row["dataview_json"]),
    )


class NoteRepository:
    """笔记主元数据实体仓储。

    管理 `notes` 表的读写、路径更新及级联删除。
    """

    def __init__(self, database: Database):
        """初始化笔记仓储实例。

        Args:
            database: 数据库连接封装对象。
        """
        self.db = database

    def get(self, note_id: str) -> NoteRecord | None:
        """根据笔记唯一稳定 ID 查询笔记元数据。"""
        row = self.db.connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _note_record(row)

    def get_by_path(self, path: str) -> NoteRecord | None:
        """根据相对文件路径查询已索引笔记。"""
        row = self.db.connection.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()
        return _note_record(row)

    def list_all(self) -> list[NoteRecord]:
        """列出全库所有已索引笔记，按文件相对路径升序排序。"""
        rows = self.db.connection.execute("SELECT * FROM notes ORDER BY path").fetchall()
        records = [_note_record(row) for row in rows]
        return [record for record in records if record is not None]

    def list_index_facts(self) -> list[IndexedFile]:
        """仅提取全库笔记的 ID、路径与内容哈希，专用于增量扫描的高速比对。"""
        rows = self.db.connection.execute(
            "SELECT id, path, content_hash FROM notes ORDER BY path"
        ).fetchall()
        return [IndexedFile(**dict(row)) for row in rows]

    def get_parsed(self, note_id: str) -> ParsedNote | None:
        """获取笔记的完整 AST 解析树结构对象（ParsedNote）。

        从 `parsed_json` 字段中反序列化恢复 Pydantic 模型。
        """
        row = self.db.connection.execute(
            "SELECT parsed_json FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        return ParsedNote.model_validate_json(row[0]) if row is not None else None

    def update_path(self, note_id: str, new_path: str) -> None:
        """重命名笔记的物理相对路径，保持其 note_id 与 chunk_id 绝对不变。

        在一个事务内同步更新：
        1. `notes` 表中的 `path` 和 `parsed_json` 快照；
        2. `chunks` 表中每个切片的 `metadata_json` 冗余路径；
        3. `dirty_notes` 队列中对应的待处理记录。

        Args:
            note_id: 目标笔记 ID。
            new_path: 新的 Vault 相对路径。

        Raises:
            KeyError: 当指定 note_id 不存在时抛出。
        """
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT parsed_json, path FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            parsed = json.loads(row["parsed_json"])
            parsed["path"] = new_path
            connection.execute(
                "UPDATE notes SET path = ?, parsed_json = ?, indexed_at = ? WHERE id = ?",
                (new_path, _json(parsed), _now(), note_id),
            )
            for chunk in connection.execute(
                "SELECT id, metadata_json FROM chunks WHERE note_id = ?", (note_id,)
            ).fetchall():
                metadata = json.loads(chunk["metadata_json"])
                metadata["path"] = new_path
                connection.execute(
                    "UPDATE chunks SET metadata_json = ? WHERE id = ?",
                    (_json(metadata), chunk["id"]),
                )
            connection.execute(
                "UPDATE dirty_notes SET path = ? WHERE note_id = ?", (new_path, note_id)
            )

    def delete(self, note_id: str) -> None:
        """删除笔记及其名下所有的衍生子行。

        在一个原子事务内：
        1. 清除对应的向量嵌入（delete_vectors_for_note）；
        2. 清除全文倒排索引（delete_fts_for_note）；
        3. 删除 `notes` 主记录（依赖 SQLite 外键 ON DELETE CASCADE 自动级联清理 chunks/blocks/tags/links）。
        """
        with self.db.transaction() as connection:
            delete_vectors_for_note(connection, note_id)
            delete_fts_for_note(connection, note_id)
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))


class ChunkRepository:
    """笔记文本切片实体仓储。

    管理正规化切片记录在 `chunks` 表中的原子替换与查询。
    """

    def __init__(self, database: Database):
        """初始化切片仓储实例。

        Args:
            database: 数据库连接封装对象。
        """
        self.db = database

    def replace_for_note(
        self, note_id: str, chunks: list[Chunk], *, refresh_fts: bool = True
    ) -> None:
        """原子替换指定笔记名下的所有切片，并将临时切片 ID 绑定到持久化 note_id。

        执行严苛的数据一致性校验与落地流程：
        1. 验证切片序号（position）必须从 0 开始严格连续递增；
        2. 验证每个切片的元数据 path、raw_content 哈希以及 embedding_text 哈希完全匹配；
        3. 清除旧的向量与 FTS 索引记录；
        4. 物理删除旧的切片行；
        5. 基于 `_hash(f"{note_id}:{chunk.position}:{chunk.content_hash}")` 重新计算稳定的 chunk_id 并批量落库；
        6. 可选触发全文倒排索引刷新。

        Args:
            note_id: 归属笔记 ID。
            chunks: 待持久化的切片对象列表。
            refresh_fts: 是否在写入切片后立即刷新 FTS 全文索引（在整笔记索引流中通常延迟统一刷新）。

        Raises:
            KeyError: 当 note_id 不存在时抛出；
            ValueError: 当切片位置非连续递增或内容哈希校验不一致时抛出。
        """
        ordered = sorted(chunks, key=lambda chunk: chunk.position)
        if [chunk.position for chunk in ordered] != list(range(len(ordered))):
            raise ValueError("Chunk positions must be contiguous from zero")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            delete_vectors_for_note(connection, note_id)
            delete_fts_for_note(connection, note_id)
            connection.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
            for chunk in ordered:
                if chunk.metadata.get("path") != row["path"]:
                    raise ValueError("Chunk path does not match the indexed note")
                if _hash(chunk.raw_content) != chunk.content_hash:
                    raise ValueError("Chunk content hash does not match its content")
                if _hash(chunk.embedding_text) != chunk.embedding_text_hash:
                    raise ValueError("Chunk embedding hash does not match its text")
                metadata = {**chunk.metadata, "path": row["path"]}
                chunk_id = _hash(f"{note_id}:{chunk.position}:{chunk.content_hash}")
                connection.execute(
                    """INSERT INTO chunks (
                        id, note_id, heading_path, block_id, raw_content, embedding_text,
                        content_hash, embedding_text_hash, token_count, position, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        note_id,
                        _json(chunk.heading_path),
                        chunk.block_id,
                        chunk.raw_content,
                        chunk.embedding_text,
                        chunk.content_hash,
                        chunk.embedding_text_hash,
                        chunk.token_count,
                        chunk.position,
                        _json(metadata),
                    ),
                )
            if refresh_fts:
                refresh_fts_for_note(connection, note_id)

    def list_for_note(self, note_id: str) -> list[Chunk]:
        """获取指定笔记名下的所有切片列表，按 position 升序排列。"""
        rows = self.db.connection.execute(
            "SELECT * FROM chunks WHERE note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            Chunk(
                chunk_id=row["id"],
                note_id=row["note_id"],
                heading_path=json.loads(row["heading_path"]),
                block_id=row["block_id"],
                raw_content=row["raw_content"],
                embedding_text=row["embedding_text"],
                content_hash=row["content_hash"],
                embedding_text_hash=row["embedding_text_hash"],
                token_count=row["token_count"],
                position=row["position"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]


class IndexRepository:
    """知识库衍生元数据索引聚合仓储（Facade & Orchestrator）。

    整合 NoteRepository、ChunkRepository，负责单篇笔记的完整持久化事务、
    标签抽取、块索引、双链解析与全局状态管理。
    """

    def __init__(self, database: Database):
        """初始化索引聚合仓储。

        Args:
            database: 数据库连接封装对象。
        """
        self.db = database
        self.notes = NoteRepository(database)
        self.chunks = ChunkRepository(database)

    def index_note(
        self,
        note: ParsedNote,
        chunks: list[Chunk],
        *,
        note_id: str | None = None,
        reconcile: bool = True,
    ) -> str:
        """在单个原子事务内，持久化解析后的笔记及其全部衍生索引数据。

        完整工作流：
        1. 解析/复用稳定 ID（stable_id）：若指定或已存在则复用，否则生成新 UUID；
        2. 若路径发生变动，调用 `notes.update_path` 同步更新相关表；
        3. 插入或更新 `notes` 主表（仅当 content_hash 变化时更新 modified_at）；
        4. 调用 `chunks.replace_for_note` 原子更新切片表；
        5. 刷新标签集合（`tags` 表），去重后批量插入；
        6. 触发 `refresh_fts_for_note` 重建倒排索引；
        7. 刷新段落块集合（`blocks` 表），按顺序批量插入；
        8. 刷新链接关系（`links` 表），将 Wikilink 关联到对应的源 block_id 与目标 note_id；
        9. 清理 `dirty_notes` 中的脏标记；
        10. 若 reconcile=True，触发双链对齐（`reconcile_links`）。

        Args:
            note: 已解析的笔记 AST 模型。
            chunks: 该笔记切分生成的切片列表。
            note_id: 可选的指定稳定笔记 ID。
            reconcile: 是否在落库后立即对齐未决的外部链接。

        Returns:
            最终持久化的稳定笔记 ID（stable_id）。

        Raises:
            KeyError: 当显式传入的 note_id 在数据库中不存在时抛出。
        """
        with self.db.transaction() as connection:
            existing = self.notes.get(note_id) if note_id is not None else self.notes.get_by_path(note.path)
            if note_id is not None and existing is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            stable_id = existing.id if existing else uuid4().hex
            if existing is not None and existing.path != note.path:
                self.notes.update_path(stable_id, note.path)
            now = _now()
            content_hash = _hash(note.raw_content)
            parsed_data = note.model_dump(mode="json")
            if existing is None:
                connection.execute(
                    """INSERT INTO notes (
                        id, path, title, created_at, modified_at, indexed_at,
                        content_hash, frontmatter_json, dataview_json, parsed_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stable_id,
                        note.path,
                        note.title,
                        now,
                        now,
                        now,
                        content_hash,
                        _json(parsed_data["frontmatter"]),
                        _json(note.dataview_fields),
                        _json(parsed_data),
                    ),
                )
            else:
                modified_at = now if existing.content_hash != content_hash else existing.modified_at
                connection.execute(
                    """UPDATE notes SET title = ?, modified_at = ?, indexed_at = ?,
                        content_hash = ?, frontmatter_json = ?, dataview_json = ?, parsed_json = ?
                        WHERE id = ?""",
                    (
                        note.title,
                        modified_at,
                        now,
                        content_hash,
                        _json(parsed_data["frontmatter"]),
                        _json(note.dataview_fields),
                        _json(parsed_data),
                        stable_id,
                    ),
                )

            self.chunks.replace_for_note(stable_id, chunks, refresh_fts=False)
            connection.execute("DELETE FROM tags WHERE note_id = ?", (stable_id,))
            connection.executemany(
                "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
                [(stable_id, tag) for tag in dict.fromkeys(note.tags)],
            )
            refresh_fts_for_note(connection, stable_id)
            connection.execute("DELETE FROM blocks WHERE note_id = ?", (stable_id,))
            connection.executemany(
                """INSERT INTO blocks (
                    note_id, position, kind, content, raw_content, line, end_line, block_id, language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        stable_id, position, block.kind, block.content, block.raw_content,
                        block.line, block.end_line, block.block_id, block.language,
                    )
                    for position, block in enumerate(note.blocks)
                ],
            )
            connection.execute("DELETE FROM links WHERE source_note_id = ?", (stable_id,))
            for position, link in enumerate(note.wikilinks):
                source_block_id = next(
                    (
                        block.block_id
                        for block in note.blocks
                        if block.block_id and block.line <= link.line <= block.end_line
                    ),
                    None,
                )
                connection.execute(
                    """INSERT INTO links (
                        source_note_id, source_block_id, target_path, target_note_id,
                        target_heading, target_block_id, display_text, is_embed, position
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stable_id,
                        source_block_id,
                        link.target_path,
                        self._target_id(stable_id, note.path, link.target_path),
                        link.target_heading,
                        link.target_block_id,
                        link.display_text,
                        int(link.is_embed),
                        position,
                    ),
                )
            connection.execute("DELETE FROM dirty_notes WHERE path = ?", (note.path,))
            if reconcile:
                self.reconcile_links()
            return stable_id

    def _target_id(self, source_id: str, source_path: str, target: str | None) -> str | None:
        """尝试根据 WikiLink 中的目标路径解析出对应的 target_note_id。

        解析策略：
        1. target 为 None：指向笔记内部锚点，直接返回 source_id；
        2. 精确路径匹配：优先匹配原始 target 字符串；
        3. 自动补全后缀：尝试自动追加 `.md` 后缀匹配；
        4. 相对路径推导：结合 source_path 的父目录进行同级相对路径拼接匹配。
        """
        if target is None:
            return source_id
        path = PurePosixPath(target)
        candidates = [target]
        if path.suffix.lower() != ".md":
            candidates.append(f"{target}.md")
        parent = PurePosixPath(source_path).parent
        if str(parent) != ".":
            candidates.extend(str(parent / candidate) for candidate in tuple(candidates))
        for candidate in candidates:
            record = self.notes.get_by_path(candidate)
            if record is not None:
                return record.id
        return None

    def reconcile_links(self, *, full: bool = False) -> None:
        """重新对齐和解析双链引用指向（Link Reconciliation）。

        当新笔记入库或旧笔记重命名后，先前无法解析的悬挂链接（target_note_id IS NULL）
        可能现在已经可以找到对应实体。通过本方法可以在事务中批量重算并修补 `links` 表。

        Args:
            full: 若为 True，则重新检查全库所有链接；若为 False，仅检查尚未解析成功的悬空链接。
        """
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT links.id, links.source_note_id, links.target_path,
                          links.target_note_id, notes.path AS source_path
                   FROM links JOIN notes ON notes.id = links.source_note_id
                   WHERE ? OR links.target_note_id IS NULL""",
                (int(full),),
            ).fetchall()
            for row in rows:
                target_id = self._target_id(
                    row["source_note_id"], row["source_path"], row["target_path"]
                )
                if target_id != row["target_note_id"]:
                    connection.execute(
                        "UPDATE links SET target_note_id = ? WHERE id = ?",
                        (target_id, row["id"]),
                    )

    def tags_for_note(self, note_id: str) -> list[str]:
        """获取指定笔记的所有标签列表，按字母序升序排列。"""
        return [
            row[0] for row in self.db.connection.execute(
                "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,)
            )
        ]

    def links_for_note(self, note_id: str) -> list[LinkRecord]:
        """获取指定笔记对外发起的全部链接列表，按 position 升序排列。"""
        rows = self.db.connection.execute(
            "SELECT * FROM links WHERE source_note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            LinkRecord(
                source_note_id=row["source_note_id"],
                source_block_id=row["source_block_id"],
                target_path=row["target_path"],
                target_note_id=row["target_note_id"],
                target_heading=row["target_heading"],
                target_block_id=row["target_block_id"],
                display_text=row["display_text"],
                is_embed=bool(row["is_embed"]),
                position=row["position"],
            )
            for row in rows
        ]

    def backlinks_for_path(self, note_id: str, old_path: str) -> list[LinkImpact]:
        """查询全库中所有指向目标笔记的反向引用条目（Backlinks）。

        用于在目标笔记被移动或重命名时，定位受影响的来源笔记及链接位置。
        同时比对 `target_note_id` 以及未解析的文本路径匹配（完整路径与去除 .md 后缀）。
        """
        without_suffix = old_path[:-3] if old_path.lower().endswith(".md") else old_path
        rows = self.db.connection.execute(
            """SELECT links.source_note_id, notes.path AS source_path,
                      links.target_path, links.position
               FROM links JOIN notes ON notes.id = links.source_note_id
               WHERE links.target_note_id = ? OR links.target_path IN (?, ?)
               ORDER BY notes.path, links.position""",
            (note_id, old_path, without_suffix),
        ).fetchall()
        return [LinkImpact(**dict(row)) for row in rows]

    def blocks_for_note(self, note_id: str) -> list[Block]:
        """获取指定笔记解析出的全部结构块列表（段落、标题、代码块等），按 position 升序排列。"""
        rows = self.db.connection.execute(
            "SELECT * FROM blocks WHERE note_id = ? ORDER BY position", (note_id,)
        ).fetchall()
        return [
            Block(
                kind=row["kind"],
                content=row["content"],
                raw_content=row["raw_content"],
                line=row["line"],
                end_line=row["end_line"],
                block_id=row["block_id"],
                language=row["language"],
            )
            for row in rows
        ]

    def get_state(self, key: str) -> str | None:
        """从 `index_state` 状态表中读取指定键的持久化值。"""
        row = self.db.connection.execute(
            "SELECT value FROM index_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        """向 `index_state` 表原子写入或更新键值对（带更新时间戳）。"""
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO index_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = excluded.updated_at""",
                (key, value, _now()),
            )

    def mark_dirty(self, path: str, reason: str) -> None:
        """将指定路径的笔记标记为脏数据（Dirty），加入待补偿队列。"""
        with self.db.transaction() as connection:
            record = self.notes.get_by_path(path)
            connection.execute(
                """INSERT INTO dirty_notes (path, note_id, reason, marked_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET
                   note_id = excluded.note_id, reason = excluded.reason,
                   marked_at = excluded.marked_at""",
                (path, record.id if record else None, reason, _now()),
            )

    def list_dirty(self) -> list[DirtyNote]:
        """获取当前待处理的全部脏笔记队列项，按路径升序排列。"""
        rows = self.db.connection.execute(
            "SELECT * FROM dirty_notes ORDER BY path"
        ).fetchall()
        return [DirtyNote(**dict(row)) for row in rows]

    def clear_dirty(self, path: str) -> None:
        """从脏数据队列中移除指定路径的笔记。"""
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM dirty_notes WHERE path = ?", (path,))

    def clear(self) -> None:
        """清空全库所有派生索引数据与向量嵌入，但保留初始化的表结构（Truncate-like clear）。"""
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM dirty_notes")
            SQLiteVectorStore(self.db).clear_vectors()
            connection.execute("DELETE FROM chunk_fts")
            connection.execute("DELETE FROM notes")
            connection.execute("DELETE FROM index_state")
