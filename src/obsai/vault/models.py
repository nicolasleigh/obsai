"""Obsidian 知识库 Markdown 语法树解析模型体系（AST Domain Models）。

核心设计哲学与架构原则：
1. 稳定、可序列化的解析器产物（Stable & Serializable Parser Output）：
   所有模型均基于 Pydantic 构建，提供原生的 `model_dump()` 与 `model_validate_json()` 支持；
   所有文件路径（path）均统一规范化为相对于知识库（Vault）根目录的 POSIX 相对路径。
2. 严格契约与不可变性保证（Strict Immutability & Closed Schema）：
   基类 `ParserModel` 统一配置 `extra="forbid"`（严禁未声明的非法字段，防止模式漂移与隐蔽拼写错误）
   和 `frozen=True`（实例不可变且天然可哈希），确保解析产物在后续管道传输中的内容确定性。
3. 完备的 Obsidian 扩展语法建模（First-Class Obsidian Syntax Support）：
   全面且精准地对 Obsidian 独有语法进行强类型领域建模，包括：
   - 双向维基链接（WikiLink）：支持子标题锚点 `#Heading`、块引用锚点 `#^block-id` 与展示别名 `|Alias`；
   - 块级引用机制（Block Reference）：支持对任意段落或代码块挂载 `^block-id`；
   - 标注块（Callout）：精准识别 `> [!NOTE]` 等多种折叠/高亮提示块；
   - 元数据增强：统一提取 YAML Frontmatter 字典与 Dataview 行内属性 `[key:: value]`。
4. 精确物理行号定位（Physical Line Number Tracking）：
   各语法元素均记录 1-based 物理行号区间（`line` 与 `end_line`），
   为事务子系统的反链重写、局部精准替换以及前端代码高亮提供微观坐标。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BlockKind = Literal["heading", "paragraph", "list_item", "blockquote", "callout", "code"]
"""Markdown 解析器输出的语义结构块类型枚举：
- 'heading': 各级标题块（H1-H6）；
- 'paragraph': 普通正文段落；
- 'list_item': 有序或无序列表项；
- 'blockquote': 普通引用块（> 引用）；
- 'callout': Obsidian 专用标注/提示块（> [!NOTE] 等）；
- 'code': 围栏代码块（```language ... ```）。
"""


class ParserModel(BaseModel):
    """知识库解析树模型的不可变基类。

    配置：
    - extra="forbid": 禁止传入任何未在模型中显式声明的额外字段；
    - frozen=True: 字段只读不可变，模型实例天然可哈希。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Heading(ParserModel):
    """Markdown 标题模型。"""

    level: int
    """标题层级深度（1 代表一级标题 '# '，最高至 6）。"""

    text: str
    """去除 Markdown 符号 '#' 后的纯文本标题内容。"""

    line: int
    """该标题在源文件中的 1-based 起始物理行号。"""


class Block(ParserModel):
    """通用正文结构块模型。

    将 Markdown 笔记拆解为顺序排列的语义块单元。
    """

    kind: BlockKind
    """语义结构块类型。"""

    content: str
    """去除块级前缀语法（如去除 '> ' 或 '- '）后的清洗正文。"""

    line: int
    """结构块在源文件中的 1-based 起始行号。"""

    end_line: int
    """结构块在源文件中的 1-based 结束行号（闭区间）。"""

    raw_content: str
    """保留完整语法标记（如缩进、提示头、反引号围栏）的原始 Markdown 字符串。"""

    block_id: str | None = None
    """该块末尾挂载的 Obsidian 块标识符（不含 '^' 前缀，例如 'summary-block'）。"""

    language: str | None = None
    """代码块的编程语言标识（仅当 kind == 'code' 且显式声明时有效，如 'python'）。"""


class WikiLink(ParserModel):
    """Obsidian 双向维基链接模型（WikiLink）。

    捕获形如 `[[Path/Target#Heading^block-id|Display]]` 或嵌入式 `![[Note]]` 的链接。
    """

    target_path: str | None
    """链接指向的目标文件路径（若链接仅指向当前笔记内锚点如 '[[#总结]]'，则为 None）。"""

    target_heading: str | None = None
    """引用的目标笔记子标题锚点（不含 '#'，例如 '架构设计'）。"""

    target_block_id: str | None = None
    """引用的目标笔记块级锚点（不含 '#^'，例如 'block-abc'）。"""

    display_text: str | None = None
    """链接别名或自定义展示文本（'|' 之后的内容）。"""

    is_embed: bool = False
    """是否为嵌入式引用（以 '!' 开头，如 '![[Image.png]]'）。"""

    line: int
    """该链接在源文件中的 1-based 出现行号。"""


class ExternalLink(ParserModel):
    """外部标准 Markdown 链接模型。

    捕获形如 `[text](https://...)` 或外部图片 `![alt](https://...)` 的链接。
    """

    url: str
    """链接指向的外部网络 URL 或绝对 URI 地址。"""

    display_text: str
    """方括号中的链接锚文本。"""

    is_embed: bool = False
    """是否为嵌入式外部媒体（以 '!' 开头）。"""

    line: int
    """该链接在源文件中的 1-based 出现行号。"""


class Callout(ParserModel):
    """Obsidian 专用 Callout 提示块模型。

    捕获形如 `> [!NOTE] Title` 或 `> [!WARNING]` 的折叠高亮信息块。
    """

    callout_type: str
    """提示类型标识（统一转换为大写，例如 'NOTE', 'WARNING', 'TIP', 'INFO' 等）。"""

    content: str
    """去除引用标记和类型头之后的内部正文文本。"""

    line: int
    """Callout 在源文件中的 1-based 起始行号。"""

    end_line: int
    """Callout 在源文件中的 1-based 结束行号。"""

    raw_content: str
    """完整的未经修改的原始 Callout 文本块。"""


class BlockReference(ParserModel):
    """块引用锚点定义模型。

    记录笔记中通过 `^id` 显式命名的引用目标锚点。
    """

    block_id: str
    """块唯一 ID（不含 '^' 前缀）。"""

    content: str
    """挂载该块 ID 的宿主文本内容。"""

    line: int
    """定义该块引用的 1-based 物理行号。"""


class ParsedNote(ParserModel):
    """笔记 AST 语法树解析根模型（Aggregate Root）。

    聚合单篇 Markdown 笔记经解析器提取的全部元数据、层级结构、出链网络与正文快照。
    """

    path: str
    """笔记相对于知识库根目录的物理相对路径（例如 'Inbox/Meeting.md'）。"""

    title: str
    """笔记权威标题（优先取 Frontmatter title，次优先取首个 H1，最后降级为文件主名）。"""

    frontmatter: dict[str, Any] = Field(default_factory=dict)
    """笔记头部 YAML Frontmatter 解析出的结构化属性字典。"""

    dataview_fields: dict[str, str] = Field(default_factory=dict)
    """从正文中提取的 Dataview 风格行内键值对字典（如 `[status:: completed]`）。"""

    tags: list[str] = Field(default_factory=list)
    """笔记关联的全部标签集合（聚合自 Frontmatter tags 以及正文中所有的 #tag 标签）。"""

    headings: list[Heading] = Field(default_factory=list)
    """笔记中所有标题按出现顺序排列的模型列表。"""

    blocks: list[Block] = Field(default_factory=list)
    """笔记按段落/语法块切分后的顺序块列表。"""

    wikilinks: list[WikiLink] = Field(default_factory=list)
    """笔记中所有双向维基出链列表。"""

    external_links: list[ExternalLink] = Field(default_factory=list)
    """笔记中所有外部 HTTP/HTTPS 出链列表。"""

    callouts: list[Callout] = Field(default_factory=list)
    """笔记中包含的全部 Callout 提示块列表。"""

    block_references: list[BlockReference] = Field(default_factory=list)
    """笔记中声明的全部具名块引用锚点列表。"""

    raw_content: str
    """未经任何修改的完整物理文件原始内容（保留换行与空格）。"""

    plain_text: str
    """剥离 Markdown 语法标记后的纯净正文（用于无干扰词频统计或向量化补充）。"""
