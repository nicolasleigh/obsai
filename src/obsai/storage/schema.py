"""版本化 SQLite 数据库架构定义与前向迁移引擎（Versioned SQLite schema and migrations）。

核心设计哲学与架构演进：
1. 单向线性版本演进（Linear Forward Migrations via PRAGMA user_version）：
   利用 SQLite 内置的 `PRAGMA user_version` 维护架构版本号。
   全新空库版本号为 0；系统通过自包含的迁移步进函数（_migrate_v2, _migrate_v3）
   实现零停机、原子事务性的平滑前向升级。
2. 架构版本演进历史：
   - Schema V1（结构化元数据基础）：
     - `notes`: Markdown 文件核心属性与 AST 解析快照；
     - `chunks`: 具备内容寻址特征的正规化切片；
     - `tags`: 多对多标签映射；
     - `links`: WikiLink 出链拓扑与锚点；
     - `blocks`: AST 块级结构（段落、标题、代码块）；
     - `index_state`: 全局键值状态字典；
     - `dirty_notes`: 增量变更监听补偿队列。
   - Schema V2（全文本地检索增强）：
     - `chunk_fts`: FTS5 全文检索虚拟表，支持 unicode61 分词器与 CJK 字符归一化索引。
   - Schema V3（跨模型向量嵌入与去重缓存）：
     - `embedding_generations`: 向量模型代际版本管理（支持多 Provider、多模型、不同维度共存）；
     - `embedding_cache`: 基于文本哈希的全局全局向量嵌入去重缓存（避免重复文本重复调用 API 计算）；
     - `chunk_embeddings`: 切片与向量缓存的多对多映射，并绑定 `sqlite-vec` 虚拟表的 `vec_rowid`。
3. 防御性校验与脏库防覆盖（Defensive Integrity & Safety）：
   - 在已升至最新版本的库中，校验 `REQUIRED_TABLES` 完整性；
   - 若版本号为 0 但数据库中已存在非 SQLite 系统表，坚决报错拒绝，防止无意覆写用户个人 SQLite 文件；
   - 每次版本升级均在独立的 `BEGIN IMMEDIATE` / `COMMIT` 事务保护下进行，失败时自动回滚。
"""

import sqlite3

from obsai.errors import SchemaError
from obsai.storage.fts import refresh_fts_for_note

SCHEMA_VERSION = 3
"""当前系统所期望的目标 SQLite 架构版本号。"""

REQUIRED_TABLES = {
    "notes",
    "chunks",
    "tags",
    "links",
    "blocks",
    "index_state",
    "dirty_notes",
    "chunk_fts",
    "embedding_generations",
    "embedding_cache",
    "chunk_embeddings",
}
"""完整 V3 版本架构下必须存在的全部业务表与虚拟表集合。"""

SCHEMA_V1 = (
    """CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        modified_at TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        frontmatter_json TEXT NOT NULL,
        dataview_json TEXT NOT NULL,
        parsed_json TEXT NOT NULL
    )""",
    """CREATE TABLE chunks (
        id TEXT PRIMARY KEY,
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        heading_path TEXT NOT NULL,
        block_id TEXT,
        raw_content TEXT NOT NULL,
        embedding_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        embedding_text_hash TEXT NOT NULL,
        token_count INTEGER NOT NULL CHECK(token_count >= 0),
        position INTEGER NOT NULL CHECK(position >= 0),
        metadata_json TEXT NOT NULL,
        UNIQUE(note_id, position)
    )""",
    """CREATE TABLE tags (
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY(note_id, tag)
    )""",
    """CREATE TABLE links (
        id INTEGER PRIMARY KEY,
        source_note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        source_block_id TEXT,
        target_path TEXT,
        target_note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
        target_heading TEXT,
        target_block_id TEXT,
        display_text TEXT,
        is_embed INTEGER NOT NULL CHECK(is_embed IN (0, 1)),
        position INTEGER NOT NULL,
        UNIQUE(source_note_id, position)
    )""",
    """CREATE TABLE blocks (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        raw_content TEXT NOT NULL,
        line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        block_id TEXT,
        language TEXT,
        UNIQUE(note_id, position)
    )""",
    """CREATE TABLE index_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE dirty_notes (
        path TEXT PRIMARY KEY,
        note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
        reason TEXT NOT NULL,
        marked_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_chunks_note ON chunks(note_id, position)",
    "CREATE INDEX idx_tags_tag ON tags(tag)",
    "CREATE INDEX idx_links_target ON links(target_note_id)",
    "CREATE INDEX idx_blocks_reference ON blocks(note_id, block_id)",
)
"""Schema V1 初始建表 DDL 语句元组（包含基础元数据表及核心外键索引）。"""

SCHEMA_V2 = (
    "CREATE VIRTUAL TABLE chunk_fts USING fts5("
    "title, heading, raw_content, tags, cjk_text, tokenize='unicode61')"
)
"""Schema V2 引入的 FTS5 全文检索虚拟表 DDL 语句。"""

SCHEMA_V3 = (
    """CREATE TABLE embedding_generations (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        model_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions > 0),
        created_at TEXT NOT NULL,
        UNIQUE(provider, model, model_version, dimensions)
    )""",
    """CREATE TABLE embedding_cache (
        cache_key TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
        embedding_text_hash TEXT NOT NULL,
        vector BLOB NOT NULL,
        token_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE chunk_embeddings (
        generation_id TEXT NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
        chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
        embedding_text_hash TEXT NOT NULL,
        cache_key TEXT NOT NULL REFERENCES embedding_cache(cache_key),
        vec_rowid INTEGER NOT NULL,
        PRIMARY KEY(generation_id, chunk_id),
        UNIQUE(generation_id, vec_rowid)
    )""",
    "CREATE INDEX idx_cache_generation_hash ON embedding_cache(generation_id, embedding_text_hash)",
    "CREATE INDEX idx_chunk_embeddings_chunk ON chunk_embeddings(chunk_id)",
)
"""Schema V3 引入的跨模型向量嵌入缓存与映射体系 DDL 语句元组。"""


def _tables(connection: sqlite3.Connection) -> set[str]:
    """查询当前 SQLite 数据库中所有已创建的物理表与虚拟表表名集合。"""
    return {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _migrate_v2(connection: sqlite3.Connection) -> None:
    """执行 V1 -> V2 架构迁移：创建 FTS5 虚拟表并对全库笔记填充倒排索引。

    流程：
    1. 开启 IMMEDIATE 事务；
    2. 执行 SCHEMA_V2 创建 `chunk_fts` 虚拟表；
    3. 遍历 `notes` 表中的全部笔记，调用 `refresh_fts_for_note` 聚合数据并写入 FTS5；
    4. 将 `PRAGMA user_version` 更新为 2 并提交事务；失败则回滚。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(SCHEMA_V2)
        for (note_id,) in connection.execute("SELECT id FROM notes"):
            refresh_fts_for_note(connection, note_id)
        connection.execute("PRAGMA user_version = 2")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _migrate_v3(connection: sqlite3.Connection) -> None:
    """执行 V2 -> V3 架构迁移：引入跨模型向量代际与内容去重嵌入缓存。

    流程：
    1. 开启 IMMEDIATE 事务；
    2. 执行 SCHEMA_V3 创建 embedding_generations, embedding_cache, chunk_embeddings 表及索引；
    3. 将 `PRAGMA user_version` 更新为 3 并提交事务；失败则回滚。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_V3:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def initialize_schema(connection: sqlite3.Connection) -> None:
    """检查当前数据库版本，并自动执行所需的前向迁移，直至对齐 SCHEMA_VERSION。

    处理策略：
    - 当前版本 == 3：校验 REQUIRED_TABLES，若缺失表则抛出 SchemaError，校验通过直接返回；
    - 当前版本不在 (0, 1, 2) 中：抛出 SchemaError("Unsupported SQLite schema version")；
    - 当前版本 == 2：校验 V2 基础表，执行 `_migrate_v3`；
    - 当前版本 == 1：校验 V1 基础表，依次执行 `_migrate_v2` 与 `_migrate_v3`；
    - 当前版本 == 0（空库初始化）：
      - 先核查是否存在非系统表（若非空但版本为0，说明可能为用户未加管辖的已有数据库，报错阻断防破坏）；
      - 在事务中执行 SCHEMA_V1 并设 user_version = 1；
      - 链式执行 `_migrate_v2` 与 `_migrate_v3` 完成最终对齐。

    Args:
        connection: SQLite 活跃数据库连接。

    Raises:
        SchemaError: 当数据库结构损坏、缺失表、存在不受支持的版本、或非空库版本未受管理时抛出。
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        if missing := REQUIRED_TABLES - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        return
    if version not in (0, 1, 2):
        raise SchemaError(f"Unsupported SQLite schema version: {version}")

    if version == 2:
        if missing := (REQUIRED_TABLES - {"embedding_generations", "embedding_cache", "chunk_embeddings"}) - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        _migrate_v3(connection)
        return

    if version == 1:
        if missing := (REQUIRED_TABLES - {"chunk_fts", "embedding_generations", "embedding_cache", "chunk_embeddings"}) - _tables(connection):
            raise SchemaError(f"SQLite schema is incomplete: {', '.join(sorted(missing))}")
        _migrate_v2(connection)
        _migrate_v3(connection)
        return

    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    if existing is not None:
        raise SchemaError("Unversioned SQLite database is not empty")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_V1:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    _migrate_v2(connection)
    _migrate_v3(connection)
