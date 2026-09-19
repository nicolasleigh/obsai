"""只读 Markdown AST 解析器；专用于 Obsidian 笔记语法解析与语义元数据提取。

该模块是 ObsAI 解析引擎的核心组件，负责将 Obsidian 笔记源文本转换为结构化的抽象语法树（AST）
模型对象（:class:`~obsai.vault.models.ParsedNote`）。

核心设计原则：
1. **只读且无副作用（Read-Only & Side-Effect Free）**：
   - 绝不渲染 HTML、执行内嵌脚本（如 Dataview JS）或修改文档源内容。
   - 专注于静态语义分析与索引所需的元数据抽取。
2. **标准 CommonMark 与 Obsidian 扩展方言融合**：
   - 底层依托 ``markdown-it-py`` 进行标准的 CommonMark 语法树词法与块级分析（标题、代码块、列表、引用块、段落）。
   - 上层叠加 Obsidian 专有方言的精确正则提取引擎，包括：
     * YAML Frontmatter（前置元数据映射与标签定义）
     * 双链 WikiLinks（包括嵌入引用 ``![[target]]``、标题定位 ``#heading``、块定位 ``#^block-id`` 及别名 ``|alias``）
     * 标签 Tags（``#tag/subtag`` 嵌套层级标签）
     * Dataview 属性字段（行级 ``key:: value`` 与行内 ``[key:: value]``）
     * 块级标识符（``^block-id`` 后缀标记与块引用）
     * Callout 标注块（``> [!type]`` 折叠/展开警示框与自定义类型）
     * 外部链接 ExternalLinks（支持 http/https/mailto 协议及图片外嵌）
3. **精准的源码行号映射（Line Mapping）**：
   - 严格维护 1-based 的源码行号索引（``line``, ``end_line``）。
   - 扣除 YAML Frontmatter 后的正文行偏移量（offset）会被自动补偿，保证 AST 节点所指示的行号与物理文件完全一致。
4. **双轨文本抽象（Dual Representation）**：
   - ``visible_text``：去除标记符号后的可视化可读正文（WikiLink 替换为其呈现文本），用于阅读器与语义搜索。
   - ``semantic_text``：用于元数据抽取的纯净文本，剔除行内代码（inline code）与 HTML，防止代码段中的 ``#`` 或 ``::`` 误识别为标签或字段。
"""

import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import ValidationError

from obsai.errors import ParseError, VaultError
from obsai.vault.models import (
    Block,
    BlockReference,
    Callout,
    ExternalLink,
    Heading,
    ParsedNote,
    WikiLink,
)

# ---------------------------------------------------------------------------
# 全局 MarkdownIt 实例与 Obsidian 语法正则表达式
# ---------------------------------------------------------------------------

#: MarkdownIt 基础解析器实例，采用 CommonMark 规范并启用 HTML 标签识别
MARKDOWN = MarkdownIt("commonmark", {"html": True})

#: Obsidian 双链正则表达式：匹配 !?[[目标|别名]] 或 !?[[文档#章节]] 等
#: 捕获组：
#: - embed: '!' 表示嵌入式引用（图片或文档嵌入），空字符表示普通跳转链接
#: - target: 核心跳转目标及别名文本（不含换行与闭合中括号）
WIKILINK_RE = re.compile(r"(?P<embed>!?)\[\[(?P<target>[^\]\n]+)\]\]")

#: Obsidian 标签正则表达式：匹配 #tag 或 #nested/tag
#: 使用负向后顾 (?<![\w/#]) 防止匹配到 URL 路径、文件路径、十六进制颜色值或 Markdown 标题中的 #
TAG_RE = re.compile(r"(?<![\w/#])#([\w/-]+)", re.UNICODE)

#: Dataview 整行键值对正则表达式：匹配行首的 key:: value
#: 键名必须以英文字母开头，允许包含字母、数字、下划线、点或横线
DATAVIEW_RE = re.compile(r"^\s*([A-Za-z][\w.-]*)::\s*(.*?)\s*$", re.MULTILINE)

#: Dataview 行内括号键值对正则表达式：匹配 [key:: value]
INLINE_FIELD_RE = re.compile(r"\[([A-Za-z][\w.-]*)::\s*([^\]]*?)\]")

#: Obsidian 块引用标识符正则表达式：匹配行尾的 ^block-id（以空格或行首为前缀）
#: 标识符必须以字母或数字开头，可包含字母、数字、下划线或连字符
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9][\w-]*)\s*$")

#: Obsidian Callout 标注块首行正则表达式：匹配 > [!type] 或 > [!type]+ / > [!type]- 及其后续标题
#: 捕获组：
#: - type: Callout 的类型标识符（如 note, warning, tip, danger 等）
CALLOUT_RE = re.compile(r"^\s{0,3}>\s*\[!(?P<type>[A-Za-z][\w-]*)\][+-]?(?:\s+.*)?$")


def _frontmatter(raw: str, path: str) -> tuple[dict[str, Any], str, int]:
    """解析 Markdown 文档顶部的 YAML Frontmatter 元数据块。

    Frontmatter 必须位于文件最开头，以独立的 ``---`` 行起始，并以 ``---`` 或 ``...`` 行结束。

    Args:
        raw: 原始 Markdown 全文文本。
        path: 笔记的相对路径（用于错误提示信息）。

    Returns:
        tuple[dict[str, Any], str, int]:
            - 第 1 项：解析出的 YAML 字典对象（若无 frontmatter 则返回空字典）。
            - 第 2 项：剔除 frontmatter 后的正文内容（body）。
            - 第 3 项：frontmatter 所占用的行数（offset），用于后续 AST 节点行号校正。

    Raises:
        ParseError: 当 YAML 语法错误、frontmatter 未闭合、或 frontmatter 不是由字符串键组成的字典时抛出。
    """
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, raw, 0
    for end in range(1, len(lines)):
        if lines[end].strip() in {"---", "..."}:
            try:
                data = yaml.safe_load("".join(lines[1:end]))
            except yaml.YAMLError as exc:
                raise ParseError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
                raise ParseError(f"Frontmatter must be a mapping with string keys: {path}")
            return data, "".join(lines[end + 1 :]), end + 1
    raise ParseError(f"Unclosed YAML frontmatter in {path}")


def _wikilink(target: str, embed: bool, line: int) -> WikiLink:
    """解析单个 WikiLink 目标的内部构成（路径、标题、块 ID、别名）。

    例如：
    - ``Note`` -> path='Note', heading=None, block_id=None, display_text=None
    - ``Note|Alias`` -> path='Note', heading=None, block_id=None, display_text='Alias'
    - ``Note#Heading`` -> path='Note', heading='Heading', block_id=None, display_text=None
    - ``Note#^block-id`` -> path='Note', heading=None, block_id='block-id', display_text=None
    - ``#Heading`` -> path=None, heading='Heading', block_id=None, display_text=None
    - ``#^block-id`` -> path=None, heading=None, block_id='block-id', display_text=None

    Args:
        target: 中括号内的链接文本内容（如 "Page#Section|Display"）。
        embed: 是否为嵌入引用（以 '!' 开头）。
        line: 该链接所在的 1-based 行号。

    Returns:
        WikiLink: 结构化的双链对象。
    """
    destination, separator, alias = target.partition("|")
    path, fragment_separator, fragment = destination.partition("#")
    heading = None
    block_id = None
    if fragment_separator:
        if fragment.startswith("^"):
            block_id = fragment[1:] or None
        else:
            heading = fragment or None
    return WikiLink(
        target_path=path.strip() or None,
        target_heading=heading,
        target_block_id=block_id,
        display_text=alias.strip() if separator else None,
        is_embed=embed,
        line=line,
    )


def strip_block_id(text: str) -> str:
    """移除文本末尾的 Obsidian 块引用标记（如 ``^block-id``）。

    设计考量与双重需求：
    - ``Block.content`` 保留末尾的块引用标记，这对索引（Index）非常重要：
      ``plain_text``、文本切片（chunks）以及搜索高亮摘要都是基于它构建的，
      使得用户可以通过某个块的 ID 直接检索到该笔记。
    - 但在面向读者的阅读视图（View）或富文本展示时，用户看到的是自然段落，
      类似 ``^maxmemory`` 的标记是定位锚点而非阅读用词，应当予以剔除。
    - 将该辅助函数暴露在解析器层而非在视图层重新实现，是为了确保“何为合法块 ID”的判定规则
      全局单一收敛，防止双重实现随着时间推移产生逻辑漂移（drift）。

    Args:
        text: 待处理的原始单行或段落文本。

    Returns:
        str: 剔除末尾 ``^block-id`` 后的纯文本内容。
    """
    return BLOCK_ID_RE.sub("", text)


def wikilink_display_text(
    *,
    target_path: str | None,
    display_text: str | None,
    target_heading: str | None,
    target_block_id: str | None,
) -> str:
    """计算 WikiLink 在无显式别名或阅读流中呈现时的可视显示文本。

    回退优先级规则：
    ``display_text``（显式别名） > ``target_path``（目标笔记名） >
    ``target_block_id``（块引用标识符） > ``target_heading``（目标章节标题） > ``""``

    设计考量：
    - 此函数被设为公开接口，是因为它与 ``Block.content`` 中应用的替换规则完全一致：
      段落块的可视化文本中，所有 ``[[target]]`` 都被精准替换为此字符串。
      当阅读视图需要反向还原链接位置时，必须基于完全相同的逻辑进行匹配。
    - 接收细粒度的解析分量参数而非直接接收 :class:`WikiLink` 对象，
      以便能够同时服务于解析器内部基于正则匹配字符串的即时替换（避免构造额外对象的开销）。

    Args:
        target_path: 目标笔记路径或名称。
        display_text: 显式声明的别名（alias）。
        target_heading: 锚定的章节标题。
        target_block_id: 锚定的块引用 ID。

    Returns:
        str: 最终呈现的显示文本。
    """
    return display_text or target_path or target_block_id or target_heading or ""


def _wikilink_display(match: re.Match[str]) -> str:
    """根据正则匹配项提取 WikiLink 组件并计算其可视呈现文本。

    内部作为 ``WIKILINK_RE.sub(_wikilink_display, content)`` 的回调函数使用。

    Args:
        match: ``WIKILINK_RE`` 的正则匹配结果。

    Returns:
        str: 替换后的可视显示文本。
    """
    destination, _, alias = match.group("target").partition("|")
    path, _, fragment = destination.partition("#")
    is_block = fragment.startswith("^")
    return wikilink_display_text(
        target_path=path or None,
        display_text=alias or None,
        target_heading=None if is_block else (fragment or None),
        target_block_id=(fragment[1:] or None) if is_block else None,
    )


def _visible_text(children: list[Token]) -> str:
    """从 CommonMark 行内 Token 列表中提取人类可读的可视文本。

    转换规则：
    - 普通文本（``text``）：将其中的 ``[[target]]`` 替换为可读显示文本。
    - 换行符（``softbreak``, ``hardbreak``）：保留为单个换行符 ``\n``。
    - 行内代码（``code_inline``）：保留代码字符串。
    - 图片（``image``）：保留图片的替代文本（alt text）。

    Args:
        children: CommonMark 的 inline token 列表。

    Returns:
        str: 拼接并去除首尾空白后的可视字符串。
    """
    parts: list[str] = []
    for child in children:
        if child.type == "text":
            parts.append(WIKILINK_RE.sub(_wikilink_display, child.content))
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif child.type == "code_inline":
            parts.append(child.content)
        elif child.type == "image":
            parts.append(child.content)
    return "".join(parts).strip()


def _semantic_text(children: list[Token]) -> str:
    """提取适于元数据分析的语义文本（严格排除行内代码与 HTML 内容）。

    在提取标签（Tags）、Dataview 字段（``key:: value``）和块引用（``^block-id``）时，
    不能直接使用包含所有字符的文本，因为代码片段（如 ``print("#not_a_tag")``）或 HTML
    中可能包含相同字符格式。本函数仅保留真正的正文文本，其余非文本 Token 用空格替换以保持相对行结构。

    Args:
        children: CommonMark 的 inline token 列表。

    Returns:
        str: 仅由文本节点与换行符组成的语义分析文本。
    """
    parts: list[str] = []
    for child in children:
        if child.type == "text":
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        else:
            parts.append(" ")
    return "".join(parts)


def _callout(token: Token, body_lines: list[str], offset: int) -> Callout | None:
    """检查并解析 Obsidian 样式的 Callout 标注块。

    Obsidian Callout 基于引用块语法扩展而来，首行格式形如 ``> [!note] 可选标题`` 或 ``> [!warning]+``。

    Args:
        token: CommonMark 的 ``blockquote_open`` 词法 Token。
        body_lines: 剔除 frontmatter 后的正文原始行列表。
        offset: frontmatter 占用的行偏移量。

    Returns:
        Callout | None: 若首行匹配 Callout 语法则返回结构化标注块对象，普通引用块则返回 None。
    """
    if token.map is None:
        return None
    start, end = token.map
    match = CALLOUT_RE.match(body_lines[start].rstrip("\r\n"))
    if match is None:
        return None
    # 剥离后续各行开头的引用前缀 '> '
    content = "\n".join(
        re.sub(r"^\s{0,3}>\s?", "", line.rstrip("\r\n"))
        for line in body_lines[start + 1 : end]
    ).strip()
    return Callout(
        callout_type=match.group("type").lower(),
        content=content,
        line=offset + start + 1,
        end_line=offset + end,
        raw_content="".join(body_lines[start:end]).rstrip("\r\n"),
    )


def parse_markdown(raw_content: str, path: str) -> ParsedNote:
    """从内存中的 Markdown 字符串解析整篇笔记的结构化 AST。

    解析流程：
    1. **Frontmatter 隔离**：提取开头的 YAML 元数据，并计算正文行号偏移量（offset）。
    2. **CommonMark 词法扫描**：通过 MarkdownIt 解析正文，生成 Token 流。
    3. **标签初筛**：从 Frontmatter 的 ``tags`` 属性中提取标签列表。
    4. **Token 流状态机遍历**：
       - 跟踪标题级别（heading_level）与列表嵌套深度（list_depth）。
       - 维护引用块/Callout 栈（blockquote_stack）。
       - 识别代码块（fence / code_block）并记录编程语言与行号跨度。
       - 处理行内节点（inline）：
         * 根据上下文状态判定块类型（heading, callout, blockquote, list_item, paragraph）。
         * 从 semantic_text 中识别块级锚点（``^block-id``）并记录 BlockReference。
         * 提取 Dataview 属性（行级 ``key:: value`` 与行内 ``[key:: value]``）。
         * 精准定位并提取 WikiLinks（双链跳转与嵌入）与行内 Tags。
         * 提取外部链接（ExternalLinks，包含 http/https/mailto 链接与外链图片）。
    5. **标题决策**：
       - 优先级：Frontmatter 中的 ``title`` 字段 > 文档中第一个 1 级标题（# Title） > 文件名去除后缀。
    6. **构建并校验 ParsedNote**：通过 Pydantic 校验模型，确保输出不可变 AST。

    Args:
        raw_content: 原始 Markdown 字符串全文。
        path: 笔记相对于 Vault 根目录的 POSIX 相对路径（如 ``folder/note.md``）。

    Returns:
        ParsedNote: 解析完成的只读笔记 AST 模型对象。

    Raises:
        ParseError: 当 Markdown、YAML 格式异常或 AST 数据模型校验失败时抛出。
    """
    frontmatter, body, offset = _frontmatter(raw_content, path)
    body_lines = body.splitlines(keepends=True)
    tokens = MARKDOWN.parse(body)
    headings: list[Heading] = []
    blocks: list[Block] = []
    wikilinks: list[WikiLink] = []
    external_links: list[ExternalLink] = []
    callouts: list[Callout] = []
    references: list[BlockReference] = []
    dataview: dict[str, str] = {}
    tags: list[str] = []
    heading_level: int | None = None
    list_depth = 0
    blockquote_stack: list[bool] = []
    text_parts: list[str] = []

    def add_tag(value: str) -> None:
        """规范化并添加标签（去除开头的 '#' 符号，避免重复）。"""
        tag = value.lstrip("#").strip()
        if tag and tag not in tags:
            tags.append(tag)

    # 提取 Frontmatter 中的 tags（支持单个字符串或字符串列表）
    frontmatter_tags = frontmatter.get("tags", [])
    if isinstance(frontmatter_tags, str):
        add_tag(frontmatter_tags)
    elif isinstance(frontmatter_tags, list):
        for tag in frontmatter_tags:
            if isinstance(tag, str):
                add_tag(tag)

    # 遍历 CommonMark Token 词法流
    for token in tokens:
        # --- 标题状态追踪 ---
        if token.type == "heading_open":
            heading_level = int(token.tag[1:])
        elif token.type == "heading_close":
            heading_level = None

        # --- 列表嵌套深度追踪 ---
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_depth -= 1

        # --- 引用块与 Callout 栈追踪 ---
        elif token.type == "blockquote_open":
            callout = _callout(token, body_lines, offset)
            if callout is not None:
                callouts.append(callout)
            blockquote_stack.append(callout is not None)
        elif token.type == "blockquote_close":
            blockquote_stack.pop()

        # --- 代码块处理（围栏代码块或缩进代码块） ---
        elif token.type in {"fence", "code_block"}:
            start, end = token.map or (0, 0)
            line = offset + start + 1
            code = token.content.rstrip("\n")
            language = token.info.split()[0] if token.info.strip() else None
            blocks.append(
                Block(
                    kind="code",
                    content=code,
                    line=line,
                    end_line=offset + end,
                    raw_content="".join(body_lines[start:end]).rstrip("\r\n"),
                    language=language,
                )
            )
            text_parts.append(code)

        # --- 行内内容处理（段落、标题正文、列表项正文等） ---
        elif token.type == "inline":
            children = token.children or []
            start, end = token.map or (0, 0)
            line = offset + start + 1
            visible = _visible_text(children)
            semantic = _semantic_text(children)

            # 根据当前上下文环境决定 Block 的语义类别
            if heading_level is not None and not blockquote_stack:
                kind = "heading"
                headings.append(Heading(level=heading_level, text=visible, line=line))
            elif any(blockquote_stack):
                kind = "callout"
                visible = re.sub(r"^\[![^\]]+\][+-]?\s*", "", visible).strip()
            elif blockquote_stack:
                kind = "blockquote"
            elif list_depth:
                kind = "list_item"
            else:
                kind = "paragraph"

            # 从语义纯净文本中扫描块标识符（^block-id）与 Dataview 属性
            block_id = None
            for index, semantic_line in enumerate(semantic.splitlines()):
                reference = BLOCK_ID_RE.search(semantic_line)
                if reference:
                    block_id = reference.group(1)
                    content = semantic_line[: reference.start()].strip()
                    references.append(BlockReference(block_id=block_id, content=content, line=line + index))
                field = DATAVIEW_RE.fullmatch(semantic_line)
                if field:
                    dataview[field.group(1)] = field.group(2)
                for inline_field in INLINE_FIELD_RE.finditer(semantic_line):
                    dataview[inline_field.group(1)] = inline_field.group(2).strip()

            blocks.append(
                Block(
                    kind=kind,
                    content=visible,
                    line=line,
                    end_line=offset + end,
                    raw_content="".join(body_lines[start:end]).rstrip("\r\n"),
                    block_id=block_id,
                )
            )
            if visible:
                text_parts.append(visible)

            # 扫描行内双链 WikiLink 与标签 Tag
            child_line = line
            for child in children:
                if child.type == "text":
                    for match in WIKILINK_RE.finditer(child.content):
                        link_line = child_line + child.content[: match.start()].count("\n")
                        wikilinks.append(_wikilink(match.group("target"), bool(match.group("embed")), link_line))
                    # 避免将 WikiLink 中的 '#'（如 [[Doc#Heading]]）误识别为标签
                    without_links = WIKILINK_RE.sub(" ", child.content)
                    for match in TAG_RE.finditer(without_links):
                        add_tag(match.group(1))
                    child_line += child.content.count("\n")
                elif child.type in {"softbreak", "hardbreak"}:
                    child_line += 1

            # 扫描外部网络链接与嵌入图片
            child_line = line
            for index, child in enumerate(children):
                if child.type == "link_open":
                    url = child.attrGet("href") or ""
                    if urlsplit(url).scheme.lower() in {"http", "https", "mailto"}:
                        label = []
                        for nested in children[index + 1 :]:
                            if nested.type == "link_close":
                                break
                            if nested.type in {"text", "code_inline"}:
                                label.append(nested.content)
                        external_links.append(
                            ExternalLink(url=url, display_text="".join(label), line=child_line)
                        )
                elif child.type == "image":
                    url = child.attrGet("src") or ""
                    if urlsplit(url).scheme.lower() in {"http", "https"}:
                        external_links.append(
                            ExternalLink(
                                url=url, display_text=child.content, is_embed=True, line=child_line
                            )
                        )
                if child.type in {"softbreak", "hardbreak"}:
                    child_line += 1
                else:
                    child_line += child.content.count("\n")

    # --- 标题决策优先级 ---
    # 1. Frontmatter 中的 title 字段
    # 2. 文档中第一个 1 级标题（# Title）
    # 3. 笔记文件名（去除扩展名）
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        title = next((heading.text for heading in headings if heading.level == 1), PurePosixPath(path).stem)
    else:
        title = title.strip()

    try:
        return ParsedNote(
            path=path,
            title=title,
            frontmatter=frontmatter,
            dataview_fields=dataview,
            tags=tags,
            headings=headings,
            blocks=blocks,
            wikilinks=wikilinks,
            external_links=external_links,
            callouts=callouts,
            block_references=references,
            raw_content=raw_content,
            plain_text="\n".join(text_parts),
        )
    except ValidationError as exc:
        raise ParseError(f"Invalid parsed note {path}: {exc}") from exc


def parse_note(path: Path, *, vault_root: Path | None = None) -> ParsedNote:
    """从本地文件系统读取 UTF-8 编码的 Markdown 笔记文件并解析为 AST。

    纯只读操作，不修改文件内容或元数据。

    Args:
        path: 笔记文件的物理路径。
        vault_root: 可选的 Vault 根目录路径。若提供，将校验 path 是否处于 vault_root
            范围之内，并计算相对于 Vault 根目录的 POSIX 相对路径；若未提供，则默认以文件名作为相对路径。

    Returns:
        ParsedNote: 解析得到的笔记结构化 AST 模型。

    Raises:
        VaultError: 当文件不存在、位于 Vault 目录之外、无权限读取或非有效 UTF-8 编码时抛出。
        ParseError: 当笔记内部语法或结构无法通过模型校验时抛出。
    """
    if vault_root is None:
        relative_path = path.name
    else:
        try:
            relative_path = (
                path.resolve(strict=True).relative_to(vault_root.resolve(strict=True)).as_posix()
            )
        except (OSError, ValueError) as exc:
            raise VaultError(f"Note is outside vault or inaccessible: {path}") from exc
    try:
        raw_content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VaultError(f"Cannot read note {path}: {exc}") from exc
    return parse_markdown(raw_content, relative_path)
