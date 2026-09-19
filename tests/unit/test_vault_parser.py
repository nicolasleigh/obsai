import json
from pathlib import Path

import pytest

from obsai.errors import ParseError
from obsai.vault.models import Block
from obsai.vault.parser import parse_markdown, parse_note, strip_block_id, wikilink_display_text

VAULT = Path(__file__).parents[1] / "fixtures" / "vault"


def note(name: str):
    return parse_note(VAULT / name, vault_root=VAULT)


def test_frontmatter_nested_headings_and_multiple_tags() -> None:
    parsed = note("frontmatter.md")
    assert parsed.title == "Frontmatter Title"
    assert parsed.frontmatter["status"] == "active"
    assert [(heading.level, heading.text) for heading in parsed.headings] == [
        (1, "Visible Heading"),
        (2, "Nested Heading"),
        (3, "Deep Heading"),
    ]
    assert [heading.line for heading in parsed.headings] == [8, 10, 12]
    assert parsed.tags == ["project/obsai", "research", "inline", "another"]


def test_wikilinks_alias_heading_block_and_embed() -> None:
    links = note("wikilinks.md").wikilinks
    assert len(links) == 6
    assert links[0].target_path == "Note"
    assert links[1].target_path == "Other"
    assert links[1].display_text == "Alias"
    assert links[2].target_heading == "Heading"
    assert links[3].target_block_id == "block-id"
    assert links[4].is_embed is True
    assert links[4].target_path == "diagram.png"
    assert links[5].target_path is None
    assert links[5].target_heading == "Local Heading"
    assert links[5].display_text == "Here"
    assert [link.line for link in links] == [3, 3, 3, 3, 4, 4]


def test_callout_and_block_reference() -> None:
    callout = note("callout.md").callouts[0]
    assert callout.callout_type == "warning"
    assert callout.content == "注意 channel 死锁。\n再检查锁顺序。"
    reference = note("blocks.md").block_references[0]
    assert reference.block_id == "ctx-lifecycle"
    assert reference.content == "Context 用于控制生命周期。"
    assert note("blocks.md").blocks[-1].block_id == "ctx-lifecycle"


def test_dataview_is_text_only_and_code_is_not_metadata() -> None:
    parsed = note("dataview.md")
    assert parsed.dataview_fields == {"status": "done", "rating": "8"}
    assert "status:: done" in parsed.raw_content

    code = note("code.md")
    assert any(block.kind == "code" and block.language == "dataviewjs" for block in code.blocks)
    assert code.wikilinks == []
    assert code.dataview_fields == {}
    assert code.tags == []
    assert code.block_references == []
    assert "[[fake]]" in code.raw_content


def test_bracketed_inline_dataview_field() -> None:
    parsed = parse_markdown("A note with [priority:: high] and `status:: fake`.\n", "inline.md")
    assert parsed.dataview_fields == {"priority": "high"}


def test_external_link_paragraph_list_and_inline_code() -> None:
    parsed = note("basic.md")
    assert parsed.external_links[0].url == "https://example.org"
    assert parsed.tags == ["alpha"]
    assert [block.kind for block in parsed.blocks] == ["heading", "paragraph", "list_item", "list_item"]
    assert "A plain paragraph" in parsed.plain_text


@pytest.mark.parametrize(
    "text",
    [
        "---\na: [\n---\n",
        "---\na: 1\n",
        "---\n- item\n---\n",
        "---\nunsafe: !!python/object/apply:os.system ['echo no']\n---\n",
    ],
)
def test_invalid_frontmatter(text: str) -> None:
    with pytest.raises(ParseError):
        parse_markdown(text, "invalid.md")


def test_parser_is_deterministic() -> None:
    first = note("wikilinks.md")
    assert first == note("wikilinks.md")
    assert isinstance(first.blocks[0], Block)


def test_basic_note_matches_golden_snapshot() -> None:
    expected = json.loads((VAULT / "expected_basic.json").read_text(encoding="utf-8"))
    assert note("basic.md").model_dump(mode="json") == expected


# --------------------------------------------------------------------------- #
# The two rules the note view borrows from the parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("Note", "Note"),
        ("Other|Alias", "Alias"),
        ("Target#Heading", "Target"),
        ("Target#^block-id", "Target"),
        ("#Heading", "Heading"),
        ("#^block-id", "block-id"),
    ],
)
def test_wikilink_display_text_agrees_with_what_the_parser_substitutes(
    target: str, expected: str
) -> None:
    """The helper has to match ``Block.content``, not merely be reasonable.

    ``Block.content`` replaces every ``[[...]]`` with exactly this string, and the
    note view searches for it to put the link back. A helper that disagreed with
    the substitution would still produce correct prose — with no links in it — and
    nothing would fail loudly. So the assertion is against the block, not against
    a hand-written expectation of the helper alone.
    """
    parsed = parse_markdown(f"See [[{target}]].\n", "a.md")

    assert parsed.wikilinks, f"[[{target}]] 没有被解析成 WikiLink"
    assert parsed.blocks[0].content == f"See {expected}."
    link = parsed.wikilinks[0]
    assert (
        wikilink_display_text(
            target_path=link.target_path,
            display_text=link.display_text,
            target_heading=link.target_heading,
            target_block_id=link.target_block_id,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把上限写进 redis.conf 更省事。 ^maxmemory", "把上限写进 redis.conf 更省事。"),
        ("^standalone", ""),
        ("没有块引用的一段话。", "没有块引用的一段话。"),
        ("价格 ^2 元", "价格 ^2 元"),
    ],
)
def test_strip_block_id_removes_only_a_trailing_marker(text: str, expected: str) -> None:
    """``^2`` mid-sentence is arithmetic; ``^maxmemory`` at the end is a target."""
    assert strip_block_id(text) == expected


def test_the_block_id_stays_in_block_content() -> None:
    """The divergence is deliberate and one-directional.

    The index keeps the marker so a note stays findable by the ID of one of its
    blocks; only the reader's view drops it. Pinned here so the day someone
    "fixes" ``Block.content`` they find out it was not a bug.
    """
    parsed = parse_markdown("把上限写进 redis.conf 更省事。 ^maxmemory\n", "a.md")

    assert parsed.blocks[0].block_id == "maxmemory"
    assert parsed.blocks[0].content == "把上限写进 redis.conf 更省事。 ^maxmemory"
    assert parsed.block_references[0].content == "把上限写进 redis.conf 更省事。"
