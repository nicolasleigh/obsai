import json
from pathlib import Path

import pytest

from obsai.chunking import ChunkingOptions, chunk_note
from obsai.chunking.chunker import count_tokens
from obsai.vault.parser import parse_markdown, parse_note

VAULT = Path(__file__).parents[1] / "fixtures" / "vault"


def fixture(name: str):
    return parse_note(VAULT / name, vault_root=VAULT)


def test_simple_heading_preserves_raw_body_and_context() -> None:
    chunk = chunk_note(fixture("basic.md"))[0]
    assert chunk.heading_path == ["Basic Note"]
    assert chunk.raw_content.startswith("A plain paragraph")
    assert "[site](https://example.org)" in chunk.raw_content
    assert not chunk.raw_content.startswith("Title:")
    assert chunk.embedding_text.startswith("Title: Basic Note\n\n")
    assert "Section: Basic Note" not in chunk.embedding_text
    assert "basic.md" not in chunk.embedding_text
    assert chunk.metadata["path"] == "basic.md"
    assert chunk.metadata["source_lines"] == [3, 6]
    assert chunk.token_count == count_tokens(chunk.embedding_text)
    assert len(chunk.content_hash) == 64
    assert len(chunk.embedding_text_hash) == 64


def test_chunk_output_matches_golden_snapshot() -> None:
    expected = json.loads((VAULT / "expected_chunks.json").read_text(encoding="utf-8"))
    assert [chunk.model_dump(mode="json") for chunk in chunk_note(fixture("basic.md"))] == expected


def test_multilevel_breadcrumb_and_empty_parent_headings() -> None:
    chunk = chunk_note(fixture("frontmatter.md"))[0]
    assert chunk.heading_path == ["Visible Heading", "Nested Heading", "Deep Heading"]
    assert "Section: Visible Heading > Nested Heading > Deep Heading" in chunk.embedding_text
    assert chunk.raw_content == "Text with #inline and #another tag."
    assert chunk.metadata["source_lines"] == [14, 14]
    assert len(chunk_note(parse_markdown("# Empty\n\n## Also Empty\n", "empty.md"))) == 0


def test_very_long_section_splits_only_at_paragraph_boundaries() -> None:
    paragraphs = [f"Paragraph {i} explains alpha beta gamma delta." for i in range(8)]
    raw = "# Guide\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = chunk_note(
        parse_markdown(raw, "guide.md"),
        ChunkingOptions(min_tokens=8, target_tokens=17, max_tokens=25),
    )
    assert len(chunks) > 1
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.heading_path == ["Guide"] for chunk in chunks)
    assert all(chunk.token_count <= 25 for chunk in chunks)
    for paragraph in paragraphs:
        assert sum(paragraph in chunk.raw_content for chunk in chunks) == 1


def test_fenced_code_is_atomic_even_when_over_max() -> None:
    code = "\n".join(f"print({i})" for i in range(14))
    raw = f"# Code\n\nBefore.\n\n```python\n{code}\n```\n\nAfter.\n"
    chunks = chunk_note(
        parse_markdown(raw, "code.md"),
        ChunkingOptions(min_tokens=2, target_tokens=10, max_tokens=16),
    )
    code_chunks = [chunk for chunk in chunks if "```python" in chunk.raw_content]
    assert len(code_chunks) == 1
    assert code_chunks[0].raw_content.count("```") == 2
    assert "print(0)" in code_chunks[0].raw_content
    assert "print(13)" in code_chunks[0].raw_content
    assert code_chunks[0].metadata["oversized_atomic"] is True


def test_callout_is_atomic_even_when_over_max() -> None:
    lines = "\n".join(f"> Important warning line {i}." for i in range(12))
    raw = f"# Safety\n\n> [!warning]\n{lines}\n\nAfter callout.\n"
    chunks = chunk_note(
        parse_markdown(raw, "safety.md"),
        ChunkingOptions(min_tokens=2, target_tokens=10, max_tokens=20),
    )
    callout_chunks = [chunk for chunk in chunks if "[!warning]" in chunk.raw_content]
    assert len(callout_chunks) == 1
    assert "Important warning line 0" in callout_chunks[0].raw_content
    assert "Important warning line 11" in callout_chunks[0].raw_content
    assert callout_chunks[0].metadata["oversized_atomic"] is True


def test_heading_inside_callout_does_not_split_the_callout() -> None:
    raw = "# Note\n\n> [!note]\n> # Quoted heading\n> Body text.\n"
    chunks = chunk_note(parse_markdown(raw, "quoted.md"), ChunkingOptions(1, 8, 12))
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Note"]
    assert chunks[0].raw_content == "> [!note]\n> # Quoted heading\n> Body text."


def test_block_reference_line_is_kept_together() -> None:
    chunk = chunk_note(fixture("blocks.md"))[0]
    assert chunk.block_id == "ctx-lifecycle"
    assert chunk.metadata["block_ids"] == ["ctx-lifecycle"]
    assert chunk.raw_content == "Context 用于控制生命周期。 ^ctx-lifecycle"


def test_short_paragraphs_merge_to_satisfy_minimum_when_possible() -> None:
    note = parse_markdown("# Tiny\n\nOne.\n\nTwo.\n\nThree.\n", "tiny.md")
    chunks = chunk_note(note, ChunkingOptions(min_tokens=10, target_tokens=10, max_tokens=25))
    assert len(chunks) == 1
    assert all(word in chunks[0].raw_content for word in ("One.", "Two.", "Three."))


def test_rename_does_not_change_embedding_text_or_hash() -> None:
    raw = "# Go Context\n\nContext 可以设置超时。\n"
    before = chunk_note(parse_markdown(raw, "Go/context.md"))[0]
    after = chunk_note(parse_markdown(raw, "Backend/context.md"))[0]
    assert before.embedding_text == after.embedding_text
    assert before.embedding_text_hash == after.embedding_text_hash
    assert before.content_hash == after.content_hash
    assert before.metadata["path"] == "Go/context.md"
    assert after.metadata["path"] == "Backend/context.md"
    assert before.note_id != after.note_id


@pytest.mark.parametrize(
    "values",
    [
        {"min_tokens": 0, "target_tokens": 10, "max_tokens": 20},
        {"min_tokens": 12, "target_tokens": 10, "max_tokens": 20},
        {"min_tokens": 5, "target_tokens": 30, "max_tokens": 20},
    ],
)
def test_invalid_size_options(values: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ChunkingOptions(**values)
