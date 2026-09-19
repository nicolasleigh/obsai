"""权威事实证据仓储层（Canonical Evidence Repository）。

核心设计哲学：
“从衍生索引读取标准权威证据，绝不从搜索摘要片段中拼凑（Read canonical evidence from the derived index, never from search snippets）”。

背景与架构考量：
在检索增强生成（RAG）管道中，全文检索（如 SQLite FTS5 的 snippet()）或向量检索常返回带有高亮标签（如 <b>...</b>）
或被截断省略（如 ...）的文本片段。若直接将此类片段作为 Prompt 上下文输入大语言模型，
极易因上下文破损、缺失标题层级或丢失代码块闭合等原因导致模型产生幻觉。

因此，ObsAI 强制推行“二级回表架构”：
1. 第一阶段检索（FTS / Vector / Graph）仅负责产出命中切片的高相关度元数据与唯一标识符（chunk_id）；
2. 第二阶段由本仓储根据 chunk_id 执行高效的主键内联单点查询（Point Lookup），从衍生索引中提取
   完整无损的 Markdown 原始正文（raw_content）、笔记元数据（path, title）及层级面包屑（heading_path），
   构建权威的 EvidenceRecord 供大模型推导演绎与溯源引用。
"""

import json

from obsai.answering.models import EvidenceRecord
from obsai.storage.database import Database


class SQLiteEvidenceRepository:
    """基于 SQLite 衍生索引数据库的权威事实证据仓储实现。

    采用仓储模式（Repository Pattern）封装底层 SQL 联结查询，
    为问答系统提供根据切片 ID 高效回表提取完整证据记录的能力。
    """

    def __init__(self, database: Database):
        """初始化证据仓储实例。

        Args:
            database: 知识库 SQLite 数据库连接封装对象。
        """
        self.db = database

    def get(self, chunk_id: str) -> EvidenceRecord | None:
        """根据切片唯一标识符精确查询并组装权威证据记录。

        执行高效的单行主键内联结查询：
        通过 `chunks.id = ?` 索引单点定位切片，并联结 `notes` 表获取其所属笔记的路径与标题。
        将数据库内以 JSON 数组存储的标题层级（heading_path）反序列化为不可变的元组。

        Args:
            chunk_id: 待查询切片的全局唯一标识符（通常为内容哈希）。

        Returns:
            若找到匹配切片则返回强类型的 EvidenceRecord 对象；若不存在（如已被删除或未索引）则返回 None。
        """
        row = self.db.connection.execute(
            """SELECT chunks.id AS chunk_id, chunks.note_id, notes.path, notes.title,
                      chunks.heading_path, chunks.block_id, chunks.raw_content
               FROM chunks JOIN notes ON notes.id = chunks.note_id
               WHERE chunks.id = ?""",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return EvidenceRecord(
            chunk_id=row["chunk_id"],
            note_id=row["note_id"],
            path=row["path"],
            title=row["title"],
            heading_path=tuple(json.loads(row["heading_path"])),
            block_id=row["block_id"],
            raw_content=row["raw_content"],
        )
