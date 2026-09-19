"""基于语法感知与保守策略的显式知识库 WikiLink 双向链接路径重写引擎。

核心设计哲学与安全架构：
1. 保守改写原则（Conservative Rewriting Principle）：
   Markdown 语法灵活性极高，文本中可能包含行内代码（如 `[[Note]]`）、代码块或复杂转义。
   任何单纯依靠全量正则表达式的字符串替换，都存在误改写代码片段或污染非链接语法的严重风险。
   本模块坚持“宁可不改，绝不误改”的保守底线。
2. 双重交叉校验机制（Parser-Regex Dual Verification）：
   - 第一阶段（AST 语法确认）：
     首先调用 `parse_markdown` 严格解析源文档 AST，提取经过语法树确认的合法 WikiLink，
     并统计每行确凿存在的显式链接数量（`explicit_by_line`）；
   - 第二阶段（逐行一致性校验与安全落盘）：
     在按行执行正则替换时，校验该行正则捕获的候选数必须与 AST 确认的数量严格相等（`len(candidates) == explicit_by_line[number]`）。
     若数量不一致（说明本行存在被 AST 忽略的行内代码反引号、注释或嵌套字符），立即跳过该行改写，实现 100% 零误伤。
3. 显式路径与短链模糊引用的精细区分（Explicit vs. Ambiguous Links）：
   - 显式路径（Explicit Path）：链接目标包含目录斜线 `/`（如 `Folder/Note`）或包含显式扩展名 `.md`（如 `Note.md`）。
     移动文件时，此类绝对/相对指向必须被精准改写为新路径；
   - 模糊引用（Ambiguous Short Link）：仅使用短文件名（如 `[[Note]]`）。Obsidian 原生具备最短路径自动解析机制，
     盲目改写短链接会破坏用户的短链书写习惯。本模块跳过短链接改写，但在返回值中累加 `ambiguous` 计数，提示用户人工核对。
4. 锚点与别名的高保真保留（Anchor & Alias Fidelity）：
   在改写路径主干的同时，百分之百逐字保留笔记子标题锚点（`#Heading`）、块引用锚点（`#^block-id`）以及自定义别名（`|Alias`）。
"""

from collections import Counter
from pathlib import PurePosixPath

from obsai.vault.parser import WIKILINK_RE, parse_markdown


def _replacement(inner: str, old: str, new: str) -> str | None:
    """计算单个 WikiLink 内部文本的目标替换字符串；若不属于目标显式路径则返回 None。

    处理流程：
    1. 以 '|' 拆分路径与展示别名（Alias）；
    2. 以 '#' 拆分目标文件与子锚点（Heading / Block ID）；
    3. 校验路径前后无空白符（非合法规范路径返回 None）；
    4. 判断是否为显式路径（包含 '/' 或以 '.md' 结尾）；
    5. 归一化去除 '.md' 后缀后比对是否与旧路径完全匹配；
    6. 组装新路径，保持原有的 '.md' 后缀风格（原先有则保留，无则去除）；
    7. 重新拼装锚点与别名。

    Args:
        inner: WikiLink 双括号内的原始文本（如 'Folder/Old#Heading|别名'）。
        old: 移动前的旧文件相对路径（如 'Folder/Old.md'）。
        new: 移动后的新文件相对路径（如 'Archive/New.md'）。

    Returns:
        改写后的新 WikiLink 内部字符串；若无需改写或不匹配则返回 None。
    """
    destination, alias_separator, alias = inner.partition("|")
    target, fragment_separator, fragment = destination.partition("#")
    if target != target.strip():
        return None
    explicit = "/" in target or target.lower().endswith(".md")
    normalized = target[:-3] if target.lower().endswith(".md") else target
    if not explicit or normalized != old[:-3]:
        return None
    new_target = new if target.lower().endswith(".md") else new[:-3]
    return new_target + (fragment_separator + fragment if fragment_separator else "") + (
        alias_separator + alias if alias_separator else ""
    )


def rewrite_explicit_links(
    content: str, source_path: str, old_path: str, new_path: str,
) -> tuple[str, int, int]:
    """语法感知地改写正文中的显式 WikiLink 目标路径，跳过模糊短引用与混合语法行。

    执行流程：
    1. 调用 `parse_markdown` 解析语法树，统计各行经 AST 确认的显式目标链接数，并统计模糊短名引用数；
    2. 逐行遍历源码：
       - 利用 `WIKILINK_RE` 检索该行中可被替换的候选正则匹配；
       - 若候选数与 AST 确认数不一致（说明本行存在混杂语法），保守跳过整行；
       - 否则执行精确替换，保持 `![[` 嵌入标记与 `]]` 闭合标记完好；
    3. 返回改写后的全文、成功改写的显式链接总数以及被跳过的模糊链接总数。

    Args:
        content: 待处理来源笔记的完整 Markdown 原始内容。
        source_path: 来源笔记在知识库中的相对物理路径（用于 AST 诊断）。
        old_path: 被引用的目标笔记旧相对路径（如 'Docs/Spec.md'）。
        new_path: 被引用的目标笔记新相对路径（如 'Archive/Spec.md'）。

    Returns:
        (改写后的完整正文字符串, 成功改写的显式链接数, 跳过的模糊匹配短链接数)。
    """
    parsed = parse_markdown(content, source_path)
    explicit_by_line: Counter[int] = Counter()
    ambiguous = 0
    old_stem = PurePosixPath(old_path).stem
    for link in parsed.wikilinks:
        target = link.target_path
        if not target:
            continue
        if _replacement(target, old_path, new_path) is not None:
            explicit_by_line[link.line] += 1
        elif (target[:-3] if target.lower().endswith(".md") else target) == old_stem:
            ambiguous += 1

    rewritten = 0
    lines = content.splitlines(keepends=True)
    for number, line in enumerate(lines, start=1):
        candidates = [
            match for match in WIKILINK_RE.finditer(line)
            if _replacement(match.group("target"), old_path, new_path) is not None
        ]
        if not candidates or len(candidates) != explicit_by_line[number]:
            continue

        def replace_match(match):
            replacement = _replacement(match.group("target"), old_path, new_path)
            if replacement is None:
                return match.group(0)
            return match.group(0)[:match.start("target") - match.start()] + replacement + "]]"

        updated = WIKILINK_RE.sub(replace_match, line)
        if updated != line:
            lines[number - 1] = updated
            rewritten += len(candidates)
    return "".join(lines), rewritten, ambiguous
