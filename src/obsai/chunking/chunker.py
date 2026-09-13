"""Group note body by heading and paragraph boundaries without breaking atoms."""

import hashlib
import re
from dataclasses import dataclass

from obsai.chunking.models import Chunk, ChunkingOptions
from obsai.vault.models import Callout, ParsedNote

# Stable, model-independent size estimate. One CJK character, word, or symbol
# counts as one unit; token_count always measures embedding_text.
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]|[^\s]", re.UNICODE)


def count_tokens(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Unit:
    line: int
    end_line: int
    block_id: str | None


def _raw_content(lines: list[str], units: list[_Unit]) -> str:
    return "".join(lines[units[0].line - 1 : units[-1].end_line]).strip("\r\n")


def _embedding_text(note: ParsedNote, heading_path: list[str], raw_content: str) -> str:
    parts = [f"Title: {note.title}"]
    # A first H1 identical to the title is already represented by Title.
    section = heading_path[1:] if heading_path and heading_path[0] == note.title else heading_path
    if section:
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
    if not units:
        return []

    def size(items: list[_Unit]) -> int:
        return count_tokens(_embedding_text(note, heading_path, _raw_content(lines, items)))

    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    for unit in units:
        if not current:
            current = [unit]
            continue
        candidate = [*current, unit]
        candidate_size = size(candidate)
        if candidate_size <= options.target_tokens or (
            size(current) < options.min_tokens and candidate_size <= options.max_tokens
        ):
            current.append(unit)
        else:
            groups.append(current)
            current = [unit]
    groups.append(current)

    if len(groups) > 1 and size(groups[-1]) < options.min_tokens:
        merged = [*groups[-2], *groups[-1]]
        if size(merged) <= options.max_tokens:
            groups[-2:] = [merged]
    return groups


def chunk_note(note: ParsedNote, options: ChunkingOptions | None = None) -> list[Chunk]:
    """Build chunks from ParsedNote; all content stays local and in memory."""
    options = options or ChunkingOptions()
    lines = note.raw_content.splitlines(keepends=True)
    note_id = _hash(note.path)
    heading_path: list[str] = []
    heading_levels: list[int] = []
    heading_by_line = {heading.line: heading for heading in note.headings}
    callouts: list[Callout] = sorted(note.callouts, key=lambda item: item.line)
    used_callouts: set[int] = set()
    section_units: list[_Unit] = []
    chunks: list[Chunk] = []

    def flush_section() -> None:
        for group in _groups(note, lines, heading_path, section_units, options):
            raw = _raw_content(lines, group)
            embedding_text = _embedding_text(note, heading_path, raw)
            token_count = count_tokens(embedding_text)
            block_ids = list(dict.fromkeys(unit.block_id for unit in group if unit.block_id))
            position = len(chunks)
            content_hash = _hash(raw)
            chunks.append(
                Chunk(
                    chunk_id=_hash(f"{note_id}:{position}:{content_hash}"),
                    note_id=note_id,
                    heading_path=list(heading_path),
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
                        "oversized_atomic": token_count > options.max_tokens,
                    },
                )
            )
        section_units.clear()

    for block in note.blocks:
        if block.kind == "heading":
            flush_section()
            heading = heading_by_line[block.line]
            while heading_levels and heading_levels[-1] >= heading.level:
                heading_levels.pop()
                heading_path.pop()
            heading_levels.append(heading.level)
            heading_path.append(heading.text)
            continue

        containing = next(
            (callout for callout in callouts if callout.line <= block.line <= callout.end_line),
            None,
        )
        if containing is not None:
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

        section_units.append(
            _Unit(line=block.line, end_line=block.end_line, block_id=block.block_id)
        )
    flush_section()
    return chunks
