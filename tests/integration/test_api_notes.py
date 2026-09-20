"""B-7 acceptance: ``GET /api/v1/notes/{note_id}`` renders a note, safely.

The plan's acceptance criterion for the note page is two-sided, and only one side
is about the happy path:

> 含 ``<script>``、``<iframe>`` 的笔记不执行任何内容；WikiLink 只跳转服务端校验过的
> Vault 内目标。

Both halves are pinned here as *shape* rather than as filtering, because a test that
asserts "no script executes" against a sanitiser only proves that this sanitiser,
today, with this configuration, drops that one tag. The stronger property is that
nothing in the response can become markup at all:

1. **The raw source never crosses the wire.** ``raw_content`` is not a field, and a
   test greps the whole serialized response for it. A renderer cannot be handed a
   Markdown string it might interpret, because it is never handed one.
2. **An HTML block leaves no trace; inline HTML leaves only its text.** The parser
   drops ``html_block`` entirely and ``html_inline`` from the visible text, so
   ``<script>`` is not escaped on the way out — it never entered the parse. The
   test note below therefore has to assert on the *tag* (``<script``), not on the
   word ``alert``, which legitimately survives as the paragraph's text.
3. **A link target is the index's answer or nothing.** A WikiLink whose target is
   not in the Vault comes back with ``target_note_id: null`` and is listed in
   ``unresolved_links``. The frontend renders exactly that as inert text, so
   "only server-validated targets are jumpable" is a property of the data.

One decision is pinned as-is rather than as-desired, the same way the search tests
pin ``strict_semantic``: the note is read from the **index**, not from disk, so a
file edited since the last ``index update`` renders as it was indexed. See
``test_an_edited_note_renders_as_indexed`` for why that is recorded instead of
changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.paths import MISSING_INDEX_MESSAGE
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"

REDIS = "Backend/Redis.md"
DEPLOY = "Guides/Deploy.md"
HOSTILE = "Notes/Hostile.md"

REDIS_NOTE = """---
tags: [redis, cache]
---
# Redis

Redis 是内存缓存，见 [[Guides/Deploy|部署指南]] 与 [[Notes/Gone]]。

## 配置

```bash
redis-cli CONFIG SET maxmemory 2gb  # [[not a link]]
```

把上限写进 `redis.conf` 更省事。 ^maxmemory
"""

DEPLOY_NOTE = "# Deploy\n\nSee [[Backend/Redis]].\n"

#: One note carrying every shape the acceptance criterion names. The HTML tags sit
#: on their own lines, which is what makes them ``html_block`` tokens — the case
#: that produces no block at all rather than an emptied one.
HOSTILE_NOTE = """# 危险笔记

正文 <script>alert(1)</script> 之后还有字。

<script>alert('block')</script>

<iframe src="https://example.com/tracker"></iframe>

<img src="x" onerror="alert('img')">

见 [[Notes/Gone]]。
"""


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A three-note Vault with a keyword-only index, and the index path.

    ``IncrementalIndexer`` writes chunks, blocks and links but never vectors, so the
    embedding generation is absent — the state a Vault is in before
    ``obsai index embeddings`` has ever run. Nothing on this endpoint needs it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, REDIS, REDIS_NOTE)
    write(vault, DEPLOY, DEPLOY_NOTE)
    write(vault, HOSTILE, HOSTILE_NOTE)
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def note_id_of(database_path: Path, path: str) -> str:
    """The ID the index assigned, which is what the UI has to work from."""
    with Database(database_path) as database:
        record = IndexRepository(database).notes.get_by_path(path)
    assert record is not None, f"{path} 没有被索引到，后面的断言都会失去意义"
    return record.id


def settings_for(vault: Path | None, database_path: Path) -> Settings:
    """Settings with the index configured and **no embedding configuration**.

    ``vault`` is optional because the note is read from the index: the route never
    resolves ``vault.path``. Passing ``None`` is how the "no Vault needed" test
    states that.
    """
    vault_section = {} if vault is None else {"path": vault}
    return Settings(vault=vault_section, index={"database": database_path})


def client_for(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def note_of(settings: Settings, note_id: str) -> tuple[int, dict]:
    with client_for(settings) as client:
        response = client.get(f"/api/v1/notes/{note_id}")
    return response.status_code, response.json()


def note_ok(settings: Settings, note_id: str) -> dict:
    status, payload = note_of(settings, note_id)
    assert status == 200, payload
    return payload


def text_of(block: dict) -> str:
    """The block's whole text, as a renderer would concatenate it."""
    return "".join(segment["text"] for segment in block["segments"])


def kinds_of(payload: dict) -> list[str]:
    return [block["kind"] for block in payload["blocks"]]


def block_of(payload: dict, kind: str) -> dict:
    """The first block of ``kind``; asserts rather than returning ``None`` so a
    missing block fails with "no such block" instead of a ``TypeError``."""
    for block in payload["blocks"]:
        if block["kind"] == kind:
            return block
    raise AssertionError(f"没有 {kind} 块，实际是 {kinds_of(payload)}")


# --------------------------------------------------------------------------- #
# The acceptance criterion, first half: nothing executes
# --------------------------------------------------------------------------- #


def test_the_raw_markdown_never_crosses_the_wire(tmp_path: Path) -> None:
    """The mechanism behind "不执行任何内容" — there is no source to interpret.

    Asserting on the field name *and* on a body fragment: the first catches a
    rename, the second catches someone smuggling the source in under another key.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    assert "raw_content" not in json.dumps(payload, ensure_ascii=False)
    assert "```bash" not in json.dumps(payload, ensure_ascii=False), (
        "代码围栏出现在响应里，说明返回的是原文而不是解析结果"
    )


def test_a_script_tag_leaves_no_tag_behind(tmp_path: Path) -> None:
    """An inline ``<script>`` contributes its *text* and nothing else.

    ``alert(1)`` is expected in the output — it is a word inside a paragraph. What
    must not appear is the tag, because the tag is the only thing that could become
    an element.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, HOSTILE))
    dump = json.dumps(payload, ensure_ascii=False)

    assert "<script" not in dump
    assert "</script" not in dump
    assert "alert(1)" in dump, "行内 HTML 的正文应当留下 —— 否则这条断言在空响应上也成立"


def test_an_html_block_produces_no_block_at_all(tmp_path: Path) -> None:
    """``<script>`` / ``<iframe>`` / ``<img onerror>`` on their own lines vanish.

    A block that rendered as an empty paragraph would still be a place for a
    renderer to hang an element on; producing no block is unambiguous.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, HOSTILE))
    dump = json.dumps(payload, ensure_ascii=False)

    assert "<iframe" not in dump
    assert "onerror" not in dump
    assert "example.com" not in dump, "外链地址不该出现在正文里 —— 没有东西会自动加载它"
    assert kinds_of(payload) == ["heading", "paragraph", "paragraph"], (
        f"HTML 块应当完全不产生块，实际是 {kinds_of(payload)}"
    )


def test_a_link_inside_a_code_fence_is_not_a_link(tmp_path: Path) -> None:
    """The parser never scans code for WikiLinks, and the view must not either.

    ``[[not a link]]`` in the fence has to stay literal text — this is where a
    renderer that pre-processed the source with a regex would get it wrong.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))
    code = block_of(payload, "code")

    assert code["language"] == "bash"
    assert len(code["segments"]) == 1
    assert code["segments"][0]["kind"] == "text"
    assert "[[not a link]]" in code["segments"][0]["text"]


# --------------------------------------------------------------------------- #
# The acceptance criterion, second half: only validated targets are jumpable
# --------------------------------------------------------------------------- #


def test_a_wikilink_carries_the_target_the_index_resolved(tmp_path: Path) -> None:
    """The link's ``target_note_id`` is a real ID, usable as a route parameter."""
    vault, database_path = build_vault(tmp_path)
    deploy_id = note_id_of(database_path, DEPLOY)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    links = [
        segment
        for block in payload["blocks"]
        for segment in block["segments"]
        if segment["kind"] == "link"
    ]
    assert links, "测试数据没造出 WikiLink，下面两条断言会恒真"
    resolved = [link for link in links if link["target_note_id"] is not None]
    assert [link["target_note_id"] for link in resolved] == [deploy_id]


def test_an_aliased_link_shows_the_alias_and_points_at_the_target(tmp_path: Path) -> None:
    """``[[Guides/Deploy|部署指南]]`` renders as 部署指南, not as the path."""
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    paragraph = text_of(block_of(payload, "paragraph"))
    assert "部署指南" in paragraph
    assert "Guides/Deploy" not in paragraph, "有别名时不该把路径显示出来"


def test_a_link_outside_the_vault_is_unresolved_and_reported(tmp_path: Path) -> None:
    """``target_note_id: null`` is what makes the UI render inert text.

    The path is also listed in ``unresolved_links`` so the page can explain why
    some links do not work instead of leaving the user to guess.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, HOSTILE))

    links = [
        segment
        for block in payload["blocks"]
        for segment in block["segments"]
        if segment["kind"] == "link"
    ]
    assert links and all(link["target_note_id"] is None for link in links)
    assert payload["unresolved_links"] == ["Notes/Gone"]


def test_unresolved_links_are_deduplicated(tmp_path: Path) -> None:
    """A note that references a missing note five times says so once.

    The field drives a count in the UI; repeating the path five times would turn
    "1 个链接指向不存在的笔记" into "5 个".
    """
    vault, database_path = build_vault(tmp_path)
    write(vault, REDIS, REDIS_NOTE.replace("与 [[Notes/Gone]]", "与 [[Notes/Gone]] 和 [[Notes/Gone]]"))
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    assert payload["unresolved_links"] == ["Notes/Gone"]


def test_a_heading_link_to_the_same_note_resolves_to_itself(tmp_path: Path) -> None:
    """``[[#配置]]`` has no path, and the indexer answers it with the source note.

    Pinned because "no target path" is easy to mistake for "unresolved", and the
    two render very differently: one is a jump, the other is dead text.
    """
    vault, database_path = build_vault(tmp_path)
    write(vault, DEPLOY, "# Deploy\n\n回到 [[#Deploy]]。\n")
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    deploy_id = note_id_of(database_path, DEPLOY)
    payload = note_ok(settings_for(vault, database_path), deploy_id)

    links = [
        segment
        for block in payload["blocks"]
        for segment in block["segments"]
        if segment["kind"] == "link"
    ]
    assert [link["target_note_id"] for link in links] == [deploy_id]
    assert payload["unresolved_links"] == []


# --------------------------------------------------------------------------- #
# What the page needs
# --------------------------------------------------------------------------- #


def test_the_blocks_arrive_in_document_order_with_their_heading_levels(tmp_path: Path) -> None:
    """Headings need a level, and ``Block`` does not carry one — ``Heading`` does.

    Joining them here is the point: a renderer that counted ``#`` characters would
    be re-deriving something the parser already knows.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    assert kinds_of(payload) == ["heading", "paragraph", "heading", "code", "paragraph"]
    assert [block["level"] for block in payload["blocks"] if block["kind"] == "heading"] == [1, 2]
    assert [block["level"] for block in payload["blocks"] if block["kind"] != "heading"] == [
        None
    ] * 3, "非标题块不该带 level，否则渲染器会把它当成标题"


def test_the_text_is_preserved_exactly(tmp_path: Path) -> None:
    """Concatenating the segments must reproduce the parser's visible text.

    This is the property that makes the link search trustworthy: it can only move
    boundaries inside the text, never drop or duplicate any of it.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    first = block_of(payload, "paragraph")
    assert text_of(first) == "Redis 是内存缓存，见 部署指南 与 Notes/Gone。"
    assert [segment["kind"] for segment in first["segments"]] == ["text", "link", "text", "link", "text"]


def test_a_block_id_is_carried_so_a_citation_can_land_on_it(tmp_path: Path) -> None:
    """``^maxmemory`` becomes the block's ``block_id``, which is how a citation
    from the ask page can point at a paragraph rather than at the whole note.

    The marker itself is gone from the text: it is a reference target, not prose.
    The index keeps it in ``Block.content`` so the block stays findable by ID, so
    this is the one place the view deliberately differs — and the rule is the
    parser's, not a second copy of it.
    """
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    with_id = [block for block in payload["blocks"] if block["block_id"] == "maxmemory"]
    assert len(with_id) == 1
    assert with_id[0]["kind"] == "paragraph"
    assert text_of(with_id[0]) == "把上限写进 redis.conf 更省事。"
    assert "^maxmemory" not in json.dumps(payload, ensure_ascii=False)


def test_frontmatter_tags_and_path_come_through(tmp_path: Path) -> None:
    """The page shows them; without them a note's header strip would be empty."""
    vault, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, REDIS))

    assert payload["path"] == REDIS
    assert payload["title"] == "Redis"
    assert payload["tags"] == ["redis", "cache"]
    assert payload["frontmatter"] == {"tags": ["redis", "cache"]}
    assert payload["note_id"] == note_id_of(database_path, REDIS)


def test_a_note_with_nothing_but_frontmatter_has_no_blocks(tmp_path: Path) -> None:
    """Empty is a result, not an error: the page shows the title and nothing else."""
    vault, database_path = build_vault(tmp_path)
    write(vault, "Empty.md", "---\ntitle: 空\n---\n")
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    payload = note_ok(settings_for(vault, database_path), note_id_of(database_path, "Empty.md"))

    assert payload["blocks"] == []
    assert payload["title"] == "空"


# --------------------------------------------------------------------------- #
# What the endpoint needs
# --------------------------------------------------------------------------- #


def test_an_unknown_note_id_is_a_not_found(tmp_path: Path) -> None:
    """404, not 400: the request was fine and the configuration is fine.

    The UI can then say "这篇笔记已经不在了" instead of asking the user to check
    a configuration file that is correct.
    """
    vault, database_path = build_vault(tmp_path)
    status, payload = note_of(settings_for(vault, database_path), "0" * 32)

    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["type"] == "NotFoundError"


def test_a_missing_index_is_still_a_config_error(tmp_path: Path) -> None:
    """Without an index there are no note IDs at all, so "not built yet" is the
    only useful thing to say — and it is a different problem from "no such note"."""
    vault, _ = build_vault(tmp_path)
    status, payload = note_of(settings_for(vault, tmp_path / "never-built.db"), "0" * 32)

    assert status == 400
    assert payload["error"]["code"] == "config"
    assert payload["error"]["message"] == MISSING_INDEX_MESSAGE


def test_the_note_endpoint_does_not_need_a_vault(tmp_path: Path) -> None:
    """No ``vault.path`` at all, and the note still renders.

    Worth pinning because it is the same property the search page has and the
    overview page does not: a Vault that has moved since the last ``index update``
    is still readable from the index.
    """
    _, database_path = build_vault(tmp_path)
    payload = note_ok(settings_for(None, database_path), note_id_of(database_path, REDIS))

    assert payload["path"] == REDIS
    assert payload["blocks"], "没有块的话这条断言在空响应上也成立"


def test_an_edited_note_renders_as_indexed(tmp_path: Path) -> None:
    """A known trade-off, pinned as-is and recorded in the plan document.

    The note is read from the index, so editing the file without re-indexing shows
    the indexed version. Reading the file instead would mix two snapshots — the ID
    comes from the index, the bytes would not — and would turn a Vault that moved
    into a 500 on a page the user reached by clicking a working search result.
    ``obsai index update`` is the refresh, and ``/status`` is where a stale index
    becomes visible. Flipping this test means changing that decision, not fixing it.
    """
    vault, database_path = build_vault(tmp_path)
    redis_id = note_id_of(database_path, REDIS)
    write(vault, REDIS, "# Redis\n\n改过了，但还没重建索引。\n")

    payload = note_ok(settings_for(vault, database_path), redis_id)

    assert "改过了" not in json.dumps(payload, ensure_ascii=False)
    assert "内存缓存" in json.dumps(payload, ensure_ascii=False)

    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    refreshed = note_ok(settings_for(vault, database_path), redis_id)
    assert "改过了" in json.dumps(refreshed, ensure_ascii=False)


@pytest.mark.parametrize("note_id", ["not-hex", "0" * 200])
def test_a_malformed_note_id_is_a_not_found_not_a_crash(tmp_path: Path, note_id: str) -> None:
    """The lookup is a parameterised query, so junk in the path segment is just a
    miss. Pinned because a route that pattern-matched IDs would 422 instead, and
    the UI would have to render two different "gone" states."""
    vault, database_path = build_vault(tmp_path)
    status, payload = note_of(settings_for(vault, database_path), note_id)

    assert status == 404
    assert payload["error"]["code"] == "not_found"
