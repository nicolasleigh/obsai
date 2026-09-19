"""代际隔离的 sqlite-vec 向量存储与跨笔记嵌入缓存层（Generation-isolated sqlite-vec storage and cross-note embedding cache）。

核心设计哲学与技术架构：
1. 向量模型代际隔离（Generation Isolation）：
   每个 `EmbeddingGeneration`（由提供商 provider、模型 model、模型版本 model_version 及维度 dimensions 共同决定唯一哈希 ID）
   拥有独立的虚拟表 `vec_{generation_id}`（基于 sqlite-vec 的 vec0 引擎，使用余弦距离 distance_metric=cosine）。
   不同模型与维度的数据在物理上完全隔离，升级或切换向量模型无需推倒重建，支持多模型平滑迁移与共存。
2. 全局跨笔记内容寻址嵌入缓存（Cross-Note Content-Addressed Embedding Cache）：
   基于 `embedding_text_hash` 与代际 ID 构建 `embedding_cache` 表。
   当不同笔记或不同版本出现完全相同的正文内容时，直接命中本地向量缓存，彻底杜绝向外部 API（如 OpenAI / Ollama 等）
   重复发起昂贵且耗时的网络向量化请求。
3. 动态表名安全加固（Dynamic Table Name Hardening）：
   针对虚拟表名 `vec_{generation_id}` 采用严格的 64 位十六进制正则校验（`_HEX`），
   从代码底层坚决封堵动态表名拼接带来的 SQL 注入风险。
4. KNN 向量检索与元数据后过滤（KNN Search & Post-Filtering Expansion）：
   利用 `sqlite-vec` 的 MATCH 与 k 参数执行近似最近邻搜索；
   当激活元数据过滤条件（标签、路径、标题、时间戳等）时，自适应扩充候选集规模，
   确保在后置过滤后依然能够精准返回 Top-K 最高余弦相似度切片，且自动将距离转换为 [0, 1] 的余弦相似度分数。
"""

import json
import math
import re
import sqlite3
from datetime import datetime, timezone

import sqlite_vec

from obsai.embedding.models import EmbeddingGeneration
from obsai.errors import EmbeddingError
from obsai.retrieval.filters import metadata_conditions
from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.storage.database import Database

_HEX = re.compile(r"^[0-9a-f]{64}$")
"""校验 64 位标准十六进制哈希字符串的正则表达式，用于动态表名注入防御。"""


def _table(generation_id: str) -> str:
    """根据向量代际 ID 构建安全的 sqlite-vec 虚拟表名称。

    Args:
        generation_id: 64 位十六进制代际哈希 ID。

    Returns:
        形如 'vec_<64位十六进制>' 的虚拟表名。

    Raises:
        EmbeddingError: 当 generation_id 不符合 64 位十六进制安全格式时抛出，阻断非法表名。
    """
    if not _HEX.fullmatch(generation_id):
        raise EmbeddingError("Invalid embedding generation ID")
    return "vec_" + generation_id


def _validate_vector(vector: list[float], dimensions: int) -> None:
    """严格校验向量维度数量、浮点数值有效性（无无穷大或 NaN）及非全零。

    Args:
        vector: 浮点数向量列表。
        dimensions: 期望的向量维度数。

    Raises:
        EmbeddingError: 当维度不匹配、包含 NaN/无穷大、或向量为全零时抛出（全零向量无法计算余弦距离）。
    """
    if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("Embedding dimension mismatch or non-finite value")
    if not any(value != 0 for value in vector):
        raise EmbeddingError("Zero embedding cannot be used for cosine search")


def delete_vectors_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    """级联清理指定笔记名下所有切片在全部代际中的向量记录与虚拟表行。

    清理流程：
    1. 查询与该笔记切片关联的所有 (generation_id, vec_rowid)；
    2. 从对应的 `vec_{generation_id}` 虚拟表中删除物理向量行；
    3. 从 `chunk_embeddings` 映射表中删除切片关联记录。

    Args:
        connection: SQLite 活跃数据库连接。
        note_id: 待清理的笔记唯一标识符。
    """
    rows = connection.execute(
        """SELECT ce.generation_id, ce.vec_rowid
           FROM chunk_embeddings AS ce JOIN chunks AS c ON c.id = ce.chunk_id
           WHERE c.note_id = ?""",
        (note_id,),
    ).fetchall()
    for row in rows:
        connection.execute(
            f"DELETE FROM {_table(row['generation_id'])} WHERE rowid = ?",
            (row["vec_rowid"],),
        )
    connection.execute(
        "DELETE FROM chunk_embeddings WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE note_id = ?)",
        (note_id,),
    )


class SQLiteVectorStore:
    """基于 sqlite-vec 的代际隔离向量存储与检索实现。

    管理向量代际注册、本地向量缓存查询、切片向量入库绑定、全量清除及带元数据过滤的余弦相似度检索。
    """

    def __init__(self, database: Database):
        """初始化向量存储实例。

        Args:
            database: 数据库连接封装对象。
        """
        self.db = database

    def has_generation(self, generation: EmbeddingGeneration) -> bool:
        """检查指定的向量代际是否已在数据库中注册。"""
        return self.db.connection.execute(
            "SELECT 1 FROM embedding_generations WHERE id = ?", (generation.id,)
        ).fetchone() is not None

    def ensure_generation(self, generation: EmbeddingGeneration) -> None:
        """确保指定的向量代际已注册并创建对应的 sqlite-vec 虚拟表（幂等执行）。

        若代际尚不存在，在事务内：
        1. 向 `embedding_generations` 表插入元数据记录；
        2. 执行 DDL 语句创建 `vec_{generation_id}` 虚拟表，配置 `dimensions` 与 `distance_metric=cosine`。
        """
        if self.has_generation(generation):
            return
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO embedding_generations
                   (id, provider, model, model_version, dimensions, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (generation.id, generation.provider, generation.model,
                 generation.model_version, generation.dimensions,
                 datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                f"CREATE VIRTUAL TABLE {_table(generation.id)} USING vec0("
                f"embedding float[{generation.dimensions}] distance_metric=cosine)"
            )

    def has_chunk(self, generation: EmbeddingGeneration, chunk_id: str, text_hash: str) -> bool:
        """检查指定切片在给定代际下是否已经完成了该文本哈希的向量嵌入映射。"""
        return self.db.connection.execute(
            """SELECT 1 FROM chunk_embeddings WHERE generation_id = ?
               AND chunk_id = ? AND embedding_text_hash = ?""",
            (generation.id, chunk_id, text_hash),
        ).fetchone() is not None

    def get_cached(self, generation: EmbeddingGeneration, text_hash: str) -> list[float] | None:
        """从本地全局缓存中获取指定文本在特定代际下的二进制向量，并反序列化为浮点数列表。

        Returns:
            反序列化后的浮点数向量列表；若未命中缓存则返回 None。
        """
        row = self.db.connection.execute(
            "SELECT vector FROM embedding_cache WHERE cache_key = ?",
            (generation.cache_key(text_hash),),
        ).fetchone()
        if row is None:
            return None
        import struct
        return list(struct.unpack(f"<{generation.dimensions}f", row["vector"]))

    def has_cache(self, generation: EmbeddingGeneration, text_hash: str) -> bool:
        """快速判断指定文本哈希在给定代际下是否存在本地向量缓存。"""
        return self.db.connection.execute(
            "SELECT 1 FROM embedding_cache WHERE cache_key = ?",
            (generation.cache_key(text_hash),),
        ).fetchone() is not None

    def upsert(
        self,
        generation: EmbeddingGeneration,
        chunk_id: str,
        embedding_text_hash: str,
        vector: list[float],
        token_count: int,
    ) -> None:
        """将切片的向量嵌入安全入库并建立映射。

        执行严密的一致性与并发检查：
        1. 验证向量维度与数值有效性；
        2. 确保目标代际虚拟表已就绪（ensure_generation）；
        3. 核查切片当前在 `chunks` 表中的 embedding_text_hash，确保与生成向量时的前置文本哈希一致（乐观防脏写）；
        4. 缓存一致性校验：若缓存中已有同 key 向量，验证新旧向量差值在 1e-5 容差范围内，防范缓存污染；
        5. 将序列化的 float32 二进制向量写入 `embedding_cache`（INSERT OR IGNORE）；
        6. 清除切片此前可能存在的旧 `vec_rowid` 行；
        7. 向 `vec_{generation_id}` 插入新向量，并以切片的 `chunks.rowid` 作为虚拟表 rowid；
        8. 更新 `chunk_embeddings` 映射表（ON CONFLICT DO UPDATE）。

        Raises:
            EmbeddingError: 当参数非法、切片在嵌入期间被并发篡改、或与缓存中向量产生冲突时抛出。
        """
        _validate_vector(vector, generation.dimensions)
        if token_count < 0:
            raise EmbeddingError("Token count cannot be negative")
        with self.db.transaction() as connection:
            self.ensure_generation(generation)
            chunk = connection.execute(
                "SELECT rowid, embedding_text_hash FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if chunk is None or chunk["embedding_text_hash"] != embedding_text_hash:
                raise EmbeddingError("Chunk is missing or changed since embedding preflight")
            key = generation.cache_key(embedding_text_hash)
            existing_vector = self.get_cached(generation, embedding_text_hash)
            if (existing_vector is not None
                    and any(abs(a - b) > 1e-5 for a, b in zip(existing_vector, vector, strict=True))):
                raise EmbeddingError("Embedding cache key already has a different vector")
            connection.execute(
                """INSERT OR IGNORE INTO embedding_cache
                   (cache_key, generation_id, embedding_text_hash, vector, token_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, generation.id, embedding_text_hash,
                 sqlite_vec.serialize_float32(vector), token_count,
                 datetime.now(timezone.utc).isoformat()),
            )
            previous = connection.execute(
                """SELECT vec_rowid FROM chunk_embeddings
                   WHERE generation_id = ? AND chunk_id = ?""",
                (generation.id, chunk_id),
            ).fetchone()
            if previous is not None:
                connection.execute(
                    f"DELETE FROM {_table(generation.id)} WHERE rowid = ?",
                    (previous["vec_rowid"],),
                )
            connection.execute(
                f"INSERT INTO {_table(generation.id)} (rowid, embedding) VALUES (?, ?)",
                (chunk["rowid"], sqlite_vec.serialize_float32(vector)),
            )
            connection.execute(
                """INSERT INTO chunk_embeddings
                   (generation_id, chunk_id, embedding_text_hash, cache_key, vec_rowid)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(generation_id, chunk_id) DO UPDATE SET
                   embedding_text_hash = excluded.embedding_text_hash,
                   cache_key = excluded.cache_key, vec_rowid = excluded.vec_rowid""",
                (generation.id, chunk_id, embedding_text_hash, key, chunk["rowid"]),
            )

    def delete(self, generation: EmbeddingGeneration, chunk_id: str) -> None:
        """从指定代际中删除特定切片的向量索引与映射关系。"""
        if not self.has_generation(generation):
            return
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT vec_rowid FROM chunk_embeddings WHERE generation_id = ? AND chunk_id = ?",
                (generation.id, chunk_id),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                f"DELETE FROM {_table(generation.id)} WHERE rowid = ?", (row["vec_rowid"],)
            )
            connection.execute(
                "DELETE FROM chunk_embeddings WHERE generation_id = ? AND chunk_id = ?",
                (generation.id, chunk_id),
            )

    def clear_vectors(self) -> None:
        """物理清除全库所有代际的向量虚拟表、切片映射、嵌入缓存及代际元数据。"""
        with self.db.transaction() as connection:
            ids = [row[0] for row in connection.execute("SELECT id FROM embedding_generations")]
            for generation_id in ids:
                connection.execute(f"DROP TABLE {_table(generation_id)}")
            connection.execute("DELETE FROM chunk_embeddings")
            connection.execute("DELETE FROM embedding_cache")
            connection.execute("DELETE FROM embedding_generations")

    def search(
        self,
        generation: EmbeddingGeneration,
        vector: list[float],
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """在指定的向量代际中执行近似最近邻（KNN）语义向量检索。

        执行流程：
        1. 边界检查与向量有效性验证；
        2. 自适应计算 KNN 取回规模 k：若指定了元数据后置过滤条件（filters.active 为真），
           将 k 扩容至当前代际下的切片总数，防止后置过滤丢弃候选导致结果集不足；否则 k = limit；
        3. 在虚拟表 `vec_{generation_id}` 上执行 MATCH 向量查询，获取候选的 (rowid, distance)；
        4. 联结 `chunks`、`notes` 和 `chunk_embeddings` 表，追加元数据 SQL 过滤条件；
        5. 将余弦距离（distance）转化为相似度得分（score = 1.0 - distance）；
        6. 截取前 240 字符摘要，组装生成语义检索结果列表。

        Args:
            generation: 目标向量代际模型。
            vector: 查询文本对应的嵌入向量。
            limit: 最大返回结果数量，默认为 10。
            filters: 可选的检索元数据过滤条件。

        Returns:
            按余弦相似度降序排列的 SearchResult 搜索结果列表。
        """
        if limit <= 0 or not self.has_generation(generation):
            return []
        _validate_vector(vector, generation.dimensions)
        table = _table(generation.id)
        # 存在元数据后置过滤时需要扩大 KNN 候选召回集，确保最终结果的 Top-K 精度
        filtered = filters.active if filters is not None else False
        metadata_where, metadata_params = metadata_conditions(filters, notes_alias="n")
        k = (
            self.db.connection.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE generation_id = ?",
                (generation.id,),
            ).fetchone()[0]
            if filtered else limit
        )
        if not k:
            return []
        candidates = self.db.connection.execute(
            f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ?",
            (sqlite_vec.serialize_float32(vector), k),
        ).fetchall()
        results: list[SearchResult] = []
        for candidate in candidates:
            row = self.db.connection.execute(
                """SELECT c.id AS chunk_id, c.heading_path, c.raw_content,
                          n.id AS note_id, n.path, n.title
                   FROM chunks AS c JOIN notes AS n ON n.id = c.note_id
                   JOIN chunk_embeddings AS ce ON ce.chunk_id = c.id
                   WHERE ce.generation_id = ? AND ce.vec_rowid = ?"""
                + (" AND " + " AND ".join(metadata_where) if metadata_where else ""),
                (generation.id, candidate["rowid"], *metadata_params),
            ).fetchone()
            if row is None:
                continue
            results.append(SearchResult(
                chunk_id=row["chunk_id"],
                note_id=row["note_id"],
                path=row["path"],
                title=row["title"],
                heading_path=json.loads(row["heading_path"]),
                snippet=row["raw_content"][:240],
                score=1.0 - candidate["distance"],
                source="semantic",
            ))
            if len(results) == limit:
                break
        return results
