"""SQL stays behind a graph DAO; the Vault remains the source of truth.

该模块是知识图谱子系统的数据访问对象（Data Access Object, DAO），
封装所有针对 SQLite 数据库底层 links 和 notes 表的 SQL 查询。

核心架构与设计原则：
1. 仓储抽象与防腐隔离（DAO Encapsulation）：
   所有复杂的关系图查询与 SQL 拼装均收敛在 GraphRepository 内部，上层业务与 CLI
   直接消费强类型的 GraphEdge 领域实体，使持久化细节与业务逻辑解耦。
2. 幽灵/断链保留机制（Broken Link Retention via LEFT JOIN）：
   在查询边信息时，对目标笔记采用 LEFT JOIN 而非 INNER JOIN。
   这确保当目标笔记尚未创建或文件缺失时，仍然能获取到原始链接元数据，
   供上层判断 broken 状态并提供孤岛与死链分析支持。
3. 遵循 Markdown 物理顺序与聚类：
   - 出边（outgoing）：严格按照正文字符偏移量（position）排序，还原用户的自然阅读顺序；
   - 反链（backlinks）：优先按照源笔记路径（source.path）分组聚合，便于前端折叠分层展示。
4. 元数据过滤引擎无缝集成：
   复用检索系统的 metadata_conditions 机制，支持针对笔记路径前缀、标签等多维条件的图节点动态过滤。
"""

from obsai.graph.models import GraphEdge
from obsai.retrieval.filters import metadata_conditions
from obsai.retrieval.models import SearchFilters
from obsai.storage import Database


# 基础关系边查询 SQL 模板
# 注意：对 source 使用 INNER JOIN（发出链接的源笔记必须存在），
# 对 target 使用 LEFT JOIN（目标笔记可能尚未创建，允许 target 为 NULL 以保留幽灵断链数据）
_EDGE_SELECT = """SELECT l.id, l.source_note_id, source.path AS source_path,
       l.source_block_id, l.target_path, l.target_note_id,
       target.path AS resolved_target_path, l.target_heading, l.target_block_id,
       l.display_text, l.is_embed, l.position
FROM links AS l
JOIN notes AS source ON source.id = l.source_note_id
LEFT JOIN notes AS target ON target.id = l.target_note_id
"""


def _edges(rows) -> list[GraphEdge]:
    """将数据库查询的 Row 记录列表转换为强类型的 GraphEdge 实体列表。

    :param rows: sqlite3.Row 或类似可解包映射的对象序列
    :return: 经过布尔值规范化转换的 GraphEdge 实体列表
    """
    # 显式将 SQLite 存储的整数 0/1 转换为 Python 原生 bool 类型，确保类型安全
    return [GraphEdge(**{**dict(row), "is_embed": bool(row["is_embed"])}) for row in rows]


class GraphRepository:
    """知识图谱数据仓储，负责提供出链、反向链接查询与节点过滤校验。"""

    def __init__(self, database: Database):
        """初始化图谱数据仓储。

        :param database: 数据库访问实例
        """
        self.database = database

    def outgoing(self, note_id: str, *, limit: int | None = None) -> list[GraphEdge]:
        """查询指定笔记向外发出的所有正向出边（WikiLinks）。

        结果严格按照链接在正文中的物理偏移量排序（position），还原阅读顺序。

        :param note_id: 源笔记 ID
        :param limit: 可选的最大返回条数限制
        :return: 出边列表
        """
        sql = _EDGE_SELECT + "WHERE l.source_note_id = ? ORDER BY l.position, l.id"
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.database.connection.execute(
            sql, (note_id,) if limit is None else (note_id, limit),
        ).fetchall()
        return _edges(rows)

    def backlinks(self, note_id: str, *, limit: int | None = None) -> list[GraphEdge]:
        """查询所有指向指定笔记的反向入边（Backlinks）。

        结果优先按照发出链接的源笔记路径分组聚类，同笔记内按偏移量排序。

        :param note_id: 目标笔记 ID
        :param limit: 可选的最大返回条数限制
        :return: 反向链接列表
        """
        sql = _EDGE_SELECT + "WHERE l.target_note_id = ? ORDER BY source.path, l.position, l.id"
        if limit is not None:
            sql += " LIMIT ?"
        rows = self.database.connection.execute(
            sql, (note_id,) if limit is None else (note_id, limit),
        ).fetchall()
        return _edges(rows)

    def note_matches(self, note_id: str, filters: SearchFilters | None) -> bool:
        """断言判定指定笔记是否满足给定的元数据过滤条件（如路径前缀、标签过滤等）。

        在局部图谱遍历时，用于快速判断邻居节点是否在探索的作用域范围内。

        :param note_id: 待判定的笔记 ID
        :param filters: 检索过滤条件对象
        :return: 若满足过滤条件则为 True，否则为 False
        """
        clauses, params = metadata_conditions(filters)
        sql = "SELECT 1 FROM notes WHERE notes.id = ?"
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        return self.database.connection.execute(sql, (note_id, *params)).fetchone() is not None
