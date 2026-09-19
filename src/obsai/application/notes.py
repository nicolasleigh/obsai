"""One note, resolved out of the derived index.

``/search`` answers "what does this Vault say about X"; this answers "what does
this one note say". Both read the derived index and neither needs the Vault on
disk — for the same reason: the index *is* the snapshot the search result came
from, so a note opened from a result is the note that matched. Reading the file
instead would mix two snapshots (the ``note_id`` comes from the index, the bytes
would not) and would turn "the Vault moved since the last ``index update``" into a
failure on a page whose entire job is to show a result the user just clicked.

The trade-off is staleness: a note edited on disk and not re-indexed renders as it
was indexed. That is already true of the search results that lead here, and the
fix is the same ``obsai index update``; ``/status`` is where the fact becomes
visible.

**Nothing here renders Markdown, and that is the security design.** A parsed note
is reduced to *text runs*: every segment is either a string the browser puts in a
text node or a link this module has already resolved. ``raw_content`` is never
returned, so no note source reaches a code path that could build markup from it,
and there is no sanitiser whose configuration could be relaxed later. Two
properties of the parser do the heavy lifting:

* ``Block.content`` is already the note's *visible* text. ``html_inline`` and
  ``html_block`` tokens are dropped by :func:`obsai.vault.parser.parse_markdown`,
  so a ``<script>`` in a note is not escaped on the way out — it never entered the
  parse. An HTML *block* produces no block at all, and an inline one leaves no
  trace in the text.
* The same substitution is why a link has to be put back: ``content`` replaces
  ``[[Redis]]`` with its display text. The position is recovered by searching for
  that text (:func:`_segments`), which is exact whenever a note's link text is
  distinct from the prose around it and degrades to plain text when it is not.
  It can never produce a *wrong* target, because the target is never derived from
  the search — see :func:`_resolve`.

The second point is the one honest limitation of this module, and it is recorded
in the plan document rather than hidden: the parser flattens the link into the
text, so the column is gone by the time anyone can ask for it.
"""

from __future__ import annotations

from obsai.application.dto import NoteBlockView, NoteSegment, NoteView
from obsai.errors import NotFoundError
from obsai.storage import Database, IndexRepository, LinkRecord
from obsai.vault.models import ParsedNote, WikiLink
from obsai.vault.parser import strip_block_id, wikilink_display_text


def read_note(note_id: str, *, database: Database) -> NoteView:
    """One note from the index, with every WikiLink resolved or explicitly not.

    Raises :class:`obsai.errors.NotFoundError` when the ID is unknown — which is
    the ordinary outcome of a bookmark into a note that has since been deleted and
    re-indexed, not a programming error.
    """
    repository = IndexRepository(database)
    parsed = repository.notes.get_parsed(note_id)
    if parsed is None:
        raise NotFoundError(f"Unknown note: {note_id}")

    resolved = _resolve(parsed.wikilinks, repository.links_for_note(note_id))
    return NoteView(
        note_id=note_id,
        path=parsed.path,
        title=parsed.title,
        frontmatter=parsed.frontmatter,
        tags=tuple(parsed.tags),
        blocks=_blocks(parsed, resolved),
        unresolved_links=tuple(
            dict.fromkeys(
                link.target_path or ""
                for link, answer in zip(parsed.wikilinks, resolved)
                if answer is None
            )
        ),
    )


def _resolve(wikilinks: list[WikiLink], records: list[LinkRecord]) -> list[str | None]:
    """The index's answer for each WikiLink, in document order.

    ``links`` and ``parsed_json`` are written in one transaction from one parse, so
    the two lists describe the same links in the same order and ``position`` is the
    index. The identity check is not paranoia about that: it is what makes a
    disagreement *survivable*. If the two ever drift, every link degrades to
    unresolved — the page loses its links — rather than one note's link silently
    pointing at another note's target.

    ``None`` in the result means "not in this Vault", which the UI renders as inert
    text. That is the whole of B-7's "WikiLink 只跳转服务端校验过的 Vault 内目标".
    """
    if len(wikilinks) != len(records):
        return [None] * len(wikilinks)
    answers: list[str | None] = []
    for position, (link, record) in enumerate(zip(wikilinks, records)):
        if record.position != position or not _same_link(link, record):
            return [None] * len(wikilinks)
        answers.append(record.target_note_id)
    return answers


def _same_link(link: WikiLink, record: LinkRecord) -> bool:
    """Everything that identifies a link, minus the resolution and the position."""
    return (
        link.target_path == record.target_path
        and link.target_heading == record.target_heading
        and link.target_block_id == record.target_block_id
        and link.display_text == record.display_text
        and link.is_embed == record.is_embed
    )


def _blocks(parsed: ParsedNote, resolved: list[str | None]) -> tuple[NoteBlockView, ...]:
    """Every block in order, each carrying the links that belong to it.

    Ownership is decided by line, the same rule the indexer uses to attach a link
    to a block: a link belongs to the block whose ``[line, end_line]`` contains it.
    Both lists are in document order, so a single cursor walks them together and
    no block is ever visited twice.

    ``strip_block_id`` is the one place this view disagrees with ``Block.content``:
    a trailing ``^block-id`` is a reference target, and a reader should not have to
    read past it. The index keeps it so the block stays findable by its ID.
    """
    levels = {heading.line: heading.level for heading in parsed.headings}
    views: list[NoteBlockView] = []
    cursor = 0
    for block in parsed.blocks:
        owned: list[tuple[WikiLink, str | None]] = []
        while cursor < len(parsed.wikilinks) and parsed.wikilinks[cursor].line <= block.end_line:
            link = parsed.wikilinks[cursor]
            if link.line >= block.line:
                owned.append((link, resolved[cursor]))
            cursor += 1
        views.append(
            NoteBlockView(
                kind=block.kind,
                level=levels.get(block.line) if block.kind == "heading" else None,
                segments=_segments(strip_block_id(block.content), owned),
                block_id=block.block_id,
                language=block.language,
                line=block.line,
            )
        )
    return tuple(views)


def _segments(content: str, owned: list[tuple[WikiLink, str | None]]) -> tuple[NoteSegment, ...]:
    """The block's text with its links put back where the parser took them out.

    Two properties keep the search honest. It moves strictly forward, so links can
    never be reordered relative to each other; and a display text that cannot be
    found leaves the *whole* block as a single text run, so an ambiguous block
    loses its links rather than gaining a wrong one. Returning nothing for an empty
    block is deliberate too — a paragraph that was entirely HTML has no content to
    show, and an empty text run would only give the renderer something to style.
    """
    if not owned:
        return () if content == "" else (NoteSegment(kind="text", text=content),)

    segments: list[NoteSegment] = []
    cursor = 0
    for link, target_note_id in owned:
        display = wikilink_display_text(
            target_path=link.target_path,
            display_text=link.display_text,
            target_heading=link.target_heading,
            target_block_id=link.target_block_id,
        )
        found = content.find(display, cursor) if display else -1
        if found < 0:
            return (NoteSegment(kind="text", text=content),)
        if found > cursor:
            segments.append(NoteSegment(kind="text", text=content[cursor:found]))
        segments.append(
            NoteSegment(
                kind="link",
                text=display,
                target_note_id=target_note_id,
                target_path=link.target_path,
                target_heading=link.target_heading,
                target_block_id=link.target_block_id,
                is_embed=link.is_embed,
            )
        )
        cursor = found + len(display)

    if cursor < len(content):
        segments.append(NoteSegment(kind="text", text=content[cursor:]))
    return tuple(segments)
