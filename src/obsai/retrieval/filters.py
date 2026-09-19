"""共享 SQL 元数据过滤语义编译器（Shared SQL metadata filter semantics）。

核心设计哲学与安全准则：
1. 跨引擎统一过滤语义（Unified Filter Semantics）：
   为全文检索（FTS5）、向量语义检索（Vector Search）及知识图谱（Graph Query）提供标准化的 SQL WHERE 子句编译，
   确保全系统对标签、目录、修改时间及 JSON 属性的过滤行为完全一致。
2. 绝对的 SQL 注入防御（Strict SQL Parameterization）：
   所有用户输入的值 100% 通过占位符（`?`）参数化绑定；
   仅内部静态指定的表别名（`notes_alias`）参与 SQL 片段拼接，严禁透传外部输入。
3. 规避 LIKE 通配符歧义（Safe Literal Prefix Matching）：
   目录前缀匹配使用 `substr(path, 1, length(?)) = ?` 替代 `LIKE 'prefix%'`，
   彻底防止文件夹路径中包含 `%` 或 `_` 时被 SQLite 误当做通配符处理。
4. 强类型 SQLite JSON 属性对齐（Strict JSON Typed Comparison）：
   依托 SQLite `json_each` 虚拟表，结合 `field.type = ?` 和 `field.value IS ?`，
   精准区分字符串 `'123'` 与数字 `123`，并安全支持 `NULL` 值的等值判定。
"""

from datetime import datetime, timezone

from obsai.retrieval.models import JsonScalar, SearchFilters


def _timestamp(value: datetime) -> str:
    """将 datetime 对象标准化为 UTC 时区的 ISO-8601 格式文本字符串。

    处理逻辑：
    1. 若为无时区信息的朴素时间（Naive Datetime），默认假定其为 UTC 时区；
    2. 统一转换为 UTC 时区后输出 ISO 格式字符串（例如 '2026-09-19T12:00:00+00:00'）。

    :param value: 输入的时间对象
    :return: 标准化的 UTC ISO-8601 字符串（满足 SQLite 文本字典序时间比较要求）
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json_scalar(value: JsonScalar) -> tuple[str, object]:
    """将 Python 标量值映射为 SQLite json_each 虚拟表的 (type, value) 元组。

    映射规则：
    - None -> ('null', None)
    - bool -> ('true'/'false', 1/0)（必须在 int 之前检查，因 bool 是 int 的子类）
    - int -> ('integer', value)
    - float -> ('real', value)
    - str / 其他 -> ('text', value)

    :param value: 待匹配的 JSON 标量值
    :return: 二元组 (SQLite json_each.type 类型字符串, 供 SQL 占位符绑定的参数值)
    """
    if value is None:
        return "null", None
    # 必须在 isinstance(value, int) 之前检查，因为在 Python 中 bool 是 int 的子类
    if isinstance(value, bool):
        # SQLite 底层将布尔值存为 1 或 0，但 json_each 的 type 列为 'true' 或 'false'
        return ("true" if value else "false"), int(value)
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, float):
        return "real", value
    return "text", value


def metadata_conditions(
    filters: SearchFilters | None, *, notes_alias: str = "notes"
) -> tuple[list[str], list[object]]:
    """将强类型的检索过滤条件编译为参数化的 SQL WHERE 子句与参数列表。

    编译的维度：
    1. 标签过滤（filters.tags）：对 tags 关系表执行 EXISTS 子查询，多标签自动形成 AND 交集；
    2. 目录前缀过滤（filters.folder）：通过 substr 精确匹配前缀，规避 LIKE 通配符风险；
    3. 修改时间范围（modified_after / modified_before）：标准化为 UTC ISO-8601 字符串比较；
    4. Frontmatter 与 Dataview 属性（frontmatter / dataview）：利用 json_each 进行键、类型与值的强类型匹配。

    :param filters: 结构化过滤条件对象（若为 None 则返回空条件）
    :param notes_alias: 笔记主表在当前 SQL 中的别名（内部静态指定，严禁接入外部用户输入）
    :return: 二元组 (WHERE 条件 SQL 片段列表, 参数化占位符对应的值列表)
    """
    if filters is None:
        return [], []
    clauses: list[str] = []
    params: list[object] = []

    # 1. 标签（Tags）过滤：通过关联 tags 表的 EXISTS 子查询实现
    for tag in filters.tags:
        clauses.append(
            f"EXISTS (SELECT 1 FROM tags WHERE tags.note_id = {notes_alias}.id AND tags.tag = ?)"
        )
        # 剥离前缀 '#'，因为数据库底层 tags 表仅存储纯标签名
        params.append(tag.lstrip("#"))

    # 2. 目录（Folder）前缀过滤：使用 substr 精准字面量匹配，避免 LIKE 通配符解析陷阱
    if filters.folder:
        folder = filters.folder.strip("/")
        if folder:
            prefix = folder + "/"
            clauses.append(f"substr({notes_alias}.path, 1, length(?)) = ?")
            params.extend((prefix, prefix))

    # 3. 笔记修改时间（Modified Time）范围过滤
    if filters.modified_after:
        clauses.append(f"{notes_alias}.modified_at >= ?")
        params.append(_timestamp(filters.modified_after))
    if filters.modified_before:
        clauses.append(f"{notes_alias}.modified_at <= ?")
        params.append(_timestamp(filters.modified_before))

    # 4. Frontmatter（YAML 头属性）与 Dataview（内联属性）动态 JSON 字段匹配
    for column, fields in (
        ("frontmatter_json", filters.frontmatter),
        ("dataview_json", filters.dataview),
    ):
        for key, value in fields.items():
            kind, sql_value = _json_scalar(value)
            # 借助 SQLite 内置的 json_each() 虚拟表展开 JSON 对象，同时校验 key、type 与 value
            # 注意：使用 'value IS ?' 从而在 sql_value 为 None 时也能正确执行 SQL NULL 比较
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each({notes_alias}.{column}) AS field "
                "WHERE field.key = ? AND field.type = ? AND field.value IS ?)"
            )
            params.extend((key, kind, sql_value))

    return clauses, params
