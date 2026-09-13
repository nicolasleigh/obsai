"""Read-only Markdown parser; never renders or evaluates document content."""

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

MARKDOWN = MarkdownIt("commonmark", {"html": True})
WIKILINK_RE = re.compile(r"(?P<embed>!?)\[\[(?P<target>[^\]\n]+)\]\]")
TAG_RE = re.compile(r"(?<![\w/#])#([\w/-]+)", re.UNICODE)
DATAVIEW_RE = re.compile(r"^\s*([A-Za-z][\w.-]*)::\s*(.*?)\s*$", re.MULTILINE)
INLINE_FIELD_RE = re.compile(r"\[([A-Za-z][\w.-]*)::\s*([^\]]*?)\]")
BLOCK_ID_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9][\w-]*)\s*$")
CALLOUT_RE = re.compile(r"^\s{0,3}>\s*\[!(?P<type>[A-Za-z][\w-]*)\][+-]?(?:\s+.*)?$")


def _frontmatter(raw: str, path: str) -> tuple[dict[str, Any], str, int]:
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


def _wikilink_display(match: re.Match[str]) -> str:
    destination, _, alias = match.group("target").partition("|")
    path, _, fragment = destination.partition("#")
    return alias or path or fragment.lstrip("^")


def _visible_text(children: list[Token]) -> str:
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
    """Text eligible for metadata extraction (excludes inline code and HTML)."""
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
    if token.map is None:
        return None
    start, end = token.map
    match = CALLOUT_RE.match(body_lines[start].rstrip("\r\n"))
    if match is None:
        return None
    content = "\n".join(
        re.sub(r"^\s{0,3}>\s?", "", line.rstrip("\r\n"))
        for line in body_lines[start + 1 : end]
    ).strip()
    return Callout(callout_type=match.group("type").lower(), content=content, line=offset + start + 1)


def parse_markdown(raw_content: str, path: str) -> ParsedNote:
    """Parse one note from memory; path is a vault-relative POSIX path."""
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
        tag = value.lstrip("#").strip()
        if tag and tag not in tags:
            tags.append(tag)

    frontmatter_tags = frontmatter.get("tags", [])
    if isinstance(frontmatter_tags, str):
        add_tag(frontmatter_tags)
    elif isinstance(frontmatter_tags, list):
        for tag in frontmatter_tags:
            if isinstance(tag, str):
                add_tag(tag)

    for token in tokens:
        if token.type == "heading_open":
            heading_level = int(token.tag[1:])
        elif token.type == "heading_close":
            heading_level = None
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_depth -= 1
        elif token.type == "blockquote_open":
            callout = _callout(token, body_lines, offset)
            if callout is not None:
                callouts.append(callout)
            blockquote_stack.append(callout is not None)
        elif token.type == "blockquote_close":
            blockquote_stack.pop()
        elif token.type in {"fence", "code_block"}:
            line = offset + (token.map[0] if token.map else 0) + 1
            code = token.content.rstrip("\n")
            language = token.info.split()[0] if token.info.strip() else None
            blocks.append(Block(kind="code", content=code, line=line, language=language))
            text_parts.append(code)
        elif token.type == "inline":
            children = token.children or []
            line = offset + (token.map[0] if token.map else 0) + 1
            visible = _visible_text(children)
            semantic = _semantic_text(children)
            if heading_level is not None:
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

            blocks.append(Block(kind=kind, content=visible, line=line, block_id=block_id))
            if visible:
                text_parts.append(visible)

            child_line = line
            for child in children:
                if child.type == "text":
                    for match in WIKILINK_RE.finditer(child.content):
                        link_line = child_line + child.content[: match.start()].count("\n")
                        wikilinks.append(_wikilink(match.group("target"), bool(match.group("embed")), link_line))
                    without_links = WIKILINK_RE.sub(" ", child.content)
                    for match in TAG_RE.finditer(without_links):
                        add_tag(match.group(1))
                    child_line += child.content.count("\n")
                elif child.type in {"softbreak", "hardbreak"}:
                    child_line += 1

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
    """Read a UTF-8 Markdown file and parse it without modifying it."""
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
