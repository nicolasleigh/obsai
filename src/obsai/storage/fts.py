"""FTS5 全文检索引擎数据衍生同步与 CJK 字符归一化处理。

核心设计哲学与技术架构：
1. 衍生倒排索引同步（Derived FTS5 Synchronization）：
   FTS5 虚拟表 `chunk_fts` 作为只读衍生视图存在，不作为唯一数据源。
   当笔记发生增删改时，系统从正规化关系表（`notes`、`tags`、`chunks`）中聚合字段，
   全量刷新对应的 FTS 行记录，并通过统一的 `rowid` 保持与 `chunks` 表的一对一严格映射。
2. 轻量级 CJK 单字分词策略（Zero-Dependency CJK Unigram Tokenization via cjk_text）：
   SQLite 原生 FTS5 的 unicode61 分词器默认依据空白字符和标点切分词项，天然不具备中日韩（CJK）分词能力。
   为避免引入庞大且对跨平台构建不友好的外部 C 扩展（如 ICU 或结巴分词），
   ObsAI 采用优雅的 Unigram 预处理方案：在每个汉字前后插入空格（如 “知识库” -> “ 知   识   库 ”），
   使 unicode61 能够将其识别为独立的单个 Token。配合 FTS5 的短语查询（Phrase Query）机制，
   无需任何外部依赖即可实现高质量、高精度的中文全文匹配与子串检索。
"""

import json
import re
import sqlite3

_HAN = re.compile(r"([\u3400-\u9fff])")
"""匹配中日韩统一表意文字（CJK Unified Ideographs）的正则表达式。

覆盖范围：
- \u3400-\u4dbf: CJK 统一表意文字扩展 A 区（Extension A）；
- \u4e00-\u9fff: CJK 统一表意文字基本区（Basic block）。
"""


def cjk_text(value: str) -> str:
    """将文本中的汉字字符按空格隔开，供 FTS5 unicode61 分词器作为相邻 Token 索引。

    实现原理：
    1. 使用正则在每个汉字字符前后各插入一个空格；
    2. 使用 `.split()` 将连续的多个空白符折叠切分；
    3. 使用 `" ".join(...)` 重新拼接为标准空格分隔的文本。

    示例：
        输入："Obsidian知识库"
        输出："Obsidian 知 识 库"

    Args:
        value: 原始待索引的混合文本字符串。

    Returns:
        汉字被单字分词并用空格分隔后的归一化字符串。
    """
    return " ".join(_HAN.sub(r" \1 ", value).split())


def delete_fts_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    """级联清理指定笔记在 FTS5 虚拟表中的所有切片检索记录。

    利用 `chunk_fts.rowid` 与 `chunks.rowid` 的直接对应关系，
    通过子查询批量删除该笔记名下所有切片的倒排索引数据，防止产生悬挂记录。

    Args:
        connection: SQLite 数据库活跃连接。
        note_id: 待清理的笔记唯一标识符（UUID 或相对路径）。
    """
    connection.execute(
        "DELETE FROM chunk_fts WHERE rowid IN "
        "(SELECT rowid FROM chunks WHERE note_id = ?)",
        (note_id,),
    )


def refresh_fts_for_note(connection: sqlite3.Connection, note_id: str) -> None:
    """从源表（notes, tags, chunks）重新提取并全量刷新指定笔记的 FTS5 全文索引。

    执行流程：
    1. 调用 `delete_fts_for_note` 清除该笔记当前所有历史 FTS 记录；
    2. 查询 `notes` 表获取笔记标题；若笔记不存在则抛出 KeyError；
    3. 查询 `tags` 表获取该笔记挂载的所有标签，并按字母序升序空格拼接；
    4. 查询 `chunks` 表遍历该笔记下的所有切片，提取 rowid、heading_path 与 raw_content；
    5. 将 JSON 格式的 `heading_path` 反序列化并拼接为面包屑路径（如 "一级标题 > 二级标题"）；
    6. 将标题、面包屑、切片正文、标签聚合，并生成对应的 CJK 单字分词副本；
    7. 向 `chunk_fts` 虚拟表插入新的索引行，保持 rowid 与 chunks 表严格一致。

    Args:
        connection: SQLite 数据库活跃连接。
        note_id: 待刷新的笔记唯一标识符。

    Raises:
        KeyError: 当指定的 note_id 在 notes 表中不存在时抛出。
    """
    delete_fts_for_note(connection, note_id)
    note = connection.execute("SELECT title FROM notes WHERE id = ?", (note_id,)).fetchone()
    if note is None:
        raise KeyError(f"Unknown note ID: {note_id}")
    tags = " ".join(
        row[0] for row in connection.execute(
            "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,)
        )
    )
    rows = connection.execute(
        "SELECT rowid, heading_path, raw_content FROM chunks WHERE note_id = ?",
        (note_id,),
    ).fetchall()
    for row in rows:
        heading = " > ".join(json.loads(row["heading_path"]))
        fields = (note["title"], heading, row["raw_content"], tags)
        connection.execute(
            "INSERT INTO chunk_fts (rowid, title, heading, raw_content, tags, cjk_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["rowid"], *fields, cjk_text(" ".join(fields))),
        )
