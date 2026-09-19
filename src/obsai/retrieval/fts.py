"""基于 SQLite FTS5 的本地全文关键词检索服务（SQLite FTS5 keyword retrieval over indexed chunks）。

核心设计哲学与安全架构：
1. FTS5 语法注入绝对防护（FTS Query Syntax Immunity）：
   将用户外部输入的检索词强制规范化为转义后的双引号短语（Literal Phrase Match），
   彻底杜绝因包含 `AND`, `OR`, `NOT`, `NEAR`, 冒号或未配对引号而触发的 `sqlite3.OperationalError: fts5: syntax error` 崩溃。
2. CJK 中日韩汉字检索分词平滑（CJK Search Enhancement）：
   针对无天然空格分隔的连写汉字，利用索引阶段预先维护的 `cjk_text` 汉字分词扩展列进行 `OR` 联合匹配，
   弥补 SQLite 默认分词器（Unicode61）对中文切词检索能力的不足。
3. 多列差异化 BM25 权重模型（Fine-Tuned Column Weights）：
   调用内置 `bm25()` 函数并调优各列权重：
   `title (5.0) > heading (3.0) > tags (2.0) > raw_content (1.0) > cjk_text (0.8)`，
   优先凸显笔记标题、大纲标题及标签的核心权重。
4. 正向相关度分值单调性对齐（Score Normalization）：
   SQLite FTS5 的 BM25 函数计算结果为负数（越负代表越相关）；
   本模块在装配领域模型时取反为正数（`score = -row["rank"]`），与向量检索的余弦相似度保持“越大越优”的一致性，
   便于下游多路混合融合（Hybrid Fusion / RRF）。
"""

import json

from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.retrieval.filters import metadata_conditions
from obsai.storage import Database
from obsai.storage.fts import cjk_text
from obsai.telemetry import measured


def _phrase(value: str) -> str:
    """将输入字符串转义并包装为 FTS5 安全的双引号字面量短语。

    双引号转义规则：将内部的所有 `"` 替换为连续的 `""`，并在外层包裹双引号，
    使 FTS5 引擎将其作为纯文本字面量比对，防止用户输入解析为 FTS 查询操作符。

    :param value: 原始用户输入字符串
    :return: 转义后的 FTS5 安全短语
    """
    return '"' + value.replace('"', '""') + '"'


class FTSRetriever:
    """基于 SQLite FTS5 的全文关键词检索器，实现标准 Retriever 协议契约。"""

    def __init__(self, database: Database):
        """初始化全文检索器。

        :param database: 已连接并就绪的 SQLite Database 上下文实例
        """
        self.db = database

    @measured("retrieval.fts")
    def search(
        self,
        query: str,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """执行全文检索，并根据 BM25 算法与元数据过滤器输出排序后的命中切片。

        执行流程：
        1. 查询清洗与快速判空剪枝（无字母数字或 limit <= 0 直接返回空）；
        2. 短语转义防御，并根据是否含 CJK 汉字构建联合检索表达式；
        3. 编译组装元数据过滤 SQL 条件（tags, folder, modified_at, frontmatter, dataview）；
        4. 执行三表联合查询，计算 BM25 排名并提取 snippet 高亮片段；
        5. 构造强类型的 SearchResult 列表，并将负数 rank 取反为正数 score。

        :param query: 搜索查询词
        :param limit: 最大返回结果数量（默认 10）
        :param filters: 可选的高级元数据过滤条件
        :return: 包含命中切片、所属笔记、高亮摘要与相关度得分的 SearchResult 列表
        """
        query = query.strip()
        # 快速剪枝：查询为空、不包含任何字母或数字（纯标点符号）或 limit 非正数时，直接返回空
        if not query or not any(char.isalnum() for char in query) or limit <= 0:
            return []

        # 1. 安全构造 FTS 查询：视作纯字面量短语，绝不暴露 FTS 语法解析
        phrase = _phrase(query)
        # 若包含 CJK 汉字，构建双路联合检索（原始短语 OR 汉字分词扩展列短语）
        if cjk_text(query) != query:
            match = f"({phrase} OR cjk_text:{_phrase(cjk_text(query))})"
        else:
            match = phrase

        # 2. 条件装配：全文匹配与通用元数据过滤
        where = ["chunk_fts MATCH ?"]
        params: list[object] = [match]
        metadata_where, metadata_params = metadata_conditions(filters)
        where.extend(metadata_where)
        params.extend(metadata_params)

        # 3. 联表执行全文检索与 BM25 排序打分
        # chunk_fts 虚拟表关联 chunks 表（切片实体）与 notes 表（笔记实体）
        rows = self.db.connection.execute(
            """SELECT chunks.id AS chunk_id, notes.id AS note_id, notes.path, notes.title,
                      chunks.heading_path,
                      snippet(chunk_fts, 2, '[', ']', '…', 24) AS snippet,
                      bm25(chunk_fts, 5.0, 3.0, 1.0, 2.0, 0.8) AS rank
               FROM chunk_fts
               JOIN chunks ON chunks.rowid = chunk_fts.rowid
               JOIN notes ON notes.id = chunks.note_id
               WHERE """
            + " AND ".join(where)
            + " ORDER BY rank, notes.path, chunks.position LIMIT ?",
            (*params, limit),
        ).fetchall()

        # 4. 装配强类型的 SearchResult 结果列表
        return [
            SearchResult(
                chunk_id=row["chunk_id"],
                note_id=row["note_id"],
                path=row["path"],
                title=row["title"],
                heading_path=json.loads(row["heading_path"]),
                snippet=row["snippet"],
                # 关键：将 SQLite FTS5 的负数 rank 取反为正数相关度得分（分值越大越优）
                score=-row["rank"],
                source="keyword",
            )
            for row in rows
        ]
