"""Group note body by heading and paragraph boundaries without breaking atoms.

该模块是知识库切片（Chunking）的核心实现引擎。

核心算法与架构设计：
1. 结构感知分块（Heading-aware）：动态维护层级大纲面包屑栈（Breadcrumb Stack），
   在小节切换时自动冲刷切片，保证切片边界严格对齐文档逻辑层级。
2. 原子语义块保护（Atomic Preservation）：Callout 标注框、代码块等作为原子单元（_Unit）
   整体处理，绝不在内部生硬腰斩，确保代码语法与提示框语意完整。
3. 动态平滑贪心聚合（Greedy Window Accumulation）：依据 min/target/max 三级 Token 预算，
   在目标尺寸附近平滑聚合段落，防止产生碎屑切片。
4. 尾部孤儿块智能合并（Post-pass tail merge）：检测并合并小节末尾不足最小阈值的碎片块。
5. 上下文富集（Context Enrichment）：为每个切片自动注入文档标题与层级大纲路径，
   消除向量模型的“语义孤岛”问题。
"""

import hashlib
import re
from dataclasses import dataclass

from obsai.chunking.models import Chunk, ChunkingOptions
from obsai.vault.models import Callout, ParsedNote

# 稳定、无模型依赖的轻量 Token 数量估算正则：
# - [A-Za-z0-9_]+：匹配完整的英文单词或数字
# - [\u3400-\u9fff]：匹配单个中日韩（CJK）统一汉字
# - [^\s]：匹配非空白的单个标点符号
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]|[^\s]", re.UNICODE)


def count_tokens(text: str) -> int:
    """估算文本的 Token 数量（用于分块大小控制，无需依赖外部网络或大模型分词器）。"""
    return len(TOKEN_RE.findall(text))


def _hash(text: str) -> str:
    """计算文本的 SHA-256 哈希值。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Unit:
    """不可分割的最小原子行号区间单元（如单段落、代码块或整个 Callout 框）。"""

    line: int  # 起始行号（1-indexed）
    end_line: int  # 结束行号（包含）
    block_id: str | None  # 关联的 Obsidian 块引用 ID（如 ^block-123）


def _raw_content(lines: list[str], units: list[_Unit]) -> str:
    """根据单元列表的行范围，从原始行列表中切片提取出真实的 Markdown 文本。"""
    return "".join(lines[units[0].line - 1 : units[-1].end_line]).strip("\r\n")


def _embedding_text(note: ParsedNote, heading_path: list[str], raw_content: str) -> str:
    """构造携带文档标题与层级大纲的富文本，专供向量模型生成高质量 Embedding。"""
    parts = [f"Title: {note.title}"]
    # 若首个 H1 标题与文件名标题完全一致，则剔除冗余的首个 H1
    section = heading_path[1:] if heading_path and heading_path[0] == note.title else heading_path
    if section:
        # 面包屑层级路径拼接（如 Section: 技术架构 > 存储层 > SQLite）
        parts.append(f"Section: {' > '.join(section)}")
    parts.append(raw_content)
    return "\n\n".join(parts)


def _groups(
    note: ParsedNote,
    lines: list[str],
    heading_path: list[str],
    units: list[_Unit],
    options: ChunkingOptions,
) -> list[list[_Unit]]:
    """将同章节下的多个原子单元智能组合成符合 Token 预算的若干切片组。"""
    if not units:
        return []

    def size(items: list[_Unit]) -> int:
        """计算指定单元组合并成切片后的实际 Token 大小（包含富文本标题）。"""
        return count_tokens(_embedding_text(note, heading_path, _raw_content(lines, items)))

    groups: list[list[_Unit]] = []
    current: list[_Unit] = []

    # 1. 贪心累加逻辑：平滑构建切片
    for unit in units:
        if not current:
            current = [unit]
            continue
        candidate = [*current, unit]
        candidate_size = size(candidate)

        # 判定准则：
        # - 若合并后仍在目标预算内（<= target_tokens），继续吸纳；
        # - 若当前块太小（< min_tokens），只要合并后不超过硬上限（<= max_tokens），允许继续吸纳以防碎片化
        if candidate_size <= options.target_tokens or (
            size(current) < options.min_tokens and candidate_size <= options.max_tokens
        ):
            current.append(unit)
        else:
            groups.append(current)
            current = [unit]
    groups.append(current)

    # 2. 尾部孤儿块智能回退合并（Post-pass tail merge）：
    # 如果最后一个分组小于 min_tokens，且与前一个组合并后不超过 max_tokens，则自动合并，消除零碎尾巴
    if len(groups) > 1 and size(groups[-1]) < options.min_tokens:
        merged = [*groups[-2], *groups[-1]]
        if size(merged) <= options.max_tokens:
            groups[-2:] = [merged]
    return groups


def chunk_note(note: ParsedNote, options: ChunkingOptions | None = None) -> list[Chunk]:
    """对解析后的 ParsedNote 笔记进行分块处理，全流程纯内存本地计算。

    :param note: 已解析的笔记对象（包含 AST 语法块、标题树、Callout 列表等）
    :param options: 切片窗口配置选项（默认使用标准 ChunkingOptions）
    :return: 结构化的 Chunk 实体对象列表
    """
    options = options or ChunkingOptions()
    lines = note.raw_content.splitlines(keepends=True)
    note_id = _hash(note.path)
    heading_path: list[str] = []  # 标题文本面包屑栈
    heading_levels: list[int] = []  # 标题级别栈（用于维护树形深度）
    heading_by_line = {heading.line: heading for heading in note.headings}
    callouts: list[Callout] = sorted(note.callouts, key=lambda item: item.line)
    used_callouts: set[int] = set()
    section_units: list[_Unit] = []
    chunks: list[Chunk] = []

    def flush_section() -> None:
        """将当前小节内累积的原子单元切片并产出 Chunk 实体对象。"""
        for group in _groups(note, lines, heading_path, section_units, options):
            raw = _raw_content(lines, group)
            embedding_text = _embedding_text(note, heading_path, raw)
            token_count = count_tokens(embedding_text)
            block_ids = list(dict.fromkeys(unit.block_id for unit in group if unit.block_id))
            position = len(chunks)
            content_hash = _hash(raw)
            chunks.append(
                Chunk(
                    # 确定性且唯一的 chunk_id：绑定笔记 ID、顺序位置和正文哈希
                    chunk_id=_hash(f"{note_id}:{position}:{content_hash}"),
                    note_id=note_id,
                    heading_path=list(heading_path),
                    # 仅当该切片完全等于单个原子块且具有块引用时，赋予精确 block_id
                    block_id=block_ids[0] if len(group) == 1 and len(block_ids) == 1 else None,
                    raw_content=raw,
                    embedding_text=embedding_text,
                    token_count=token_count,
                    content_hash=content_hash,
                    embedding_text_hash=_hash(embedding_text),
                    position=position,
                    metadata={
                        "path": note.path,
                        "title": note.title,
                        "source_lines": [group[0].line, group[-1].end_line],
                        "block_ids": block_ids,
                        "oversized_atomic": token_count > options.max_tokens,  # 标记不可分割的超大原子块
                    },
                )
            )
        section_units.clear()

    # 遍历 AST 解析出的所有语法块
    for block in note.blocks:
        # 情况 1：遇到标题块，冲刷前一小节并更新大纲面包屑栈
        if block.kind == "heading":
            flush_section()
            heading = heading_by_line[block.line]
            # 弹栈：若当前标题级别更高或相同，弹出栈顶直到栈顶级别严格小于当前级别
            while heading_levels and heading_levels[-1] >= heading.level:
                heading_levels.pop()
                heading_path.pop()
            heading_levels.append(heading.level)
            heading_path.append(heading.text)
            continue

        # 情况 2：检查当前块是否属于某个 Callout 标注框（原子性保护）
        containing = next(
            (callout for callout in callouts if callout.line <= block.line <= callout.end_line),
            None,
        )
        if containing is not None:
            # 整个 Callout 作为一个整体单元打包，仅在第一次遇见时记录
            if containing.line not in used_callouts:
                used_callouts.add(containing.line)
                references = [
                    ref.block_id
                    for ref in note.block_references
                    if containing.line <= ref.line <= containing.end_line
                ]
                section_units.append(
                    _Unit(
                        line=containing.line,
                        end_line=containing.end_line,
                        block_id=references[0] if len(references) == 1 else None,
                    )
                )
            continue

        # 情况 3：普通语法块（段落、列表、代码块等），作为独立单元追加
        section_units.append(
            _Unit(line=block.line, end_line=block.end_line, block_id=block.block_id)
        )

    # 遍历结束，冲刷最后一个小节
    flush_section()
    return chunks
