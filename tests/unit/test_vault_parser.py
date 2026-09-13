import json
from pathlib import Path

import pytest

from obsai.errors import ParseError
from obsai.vault.models import Block
from obsai.vault.parser import parse_markdown, parse_note

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
