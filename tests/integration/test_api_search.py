"""B-5 acceptance: ``POST /api/v1/search`` answers, degrades visibly, or refuses.

The plan's acceptance criterion for the search page is a negative one — *keyword
mode has to work with no API key*. That is not a small thing to guarantee, because
it is easy to satisfy "by accident" on a developer machine where a key happens to
be in the environment. Every test below therefore builds a Vault and an index with
**no embedding configuration at all**: default ``EmbeddingConfig``, no price, no
key, no network. If a semantic backend were reached, these tests would fail rather
than quietly pass.

Three properties are pinned here, and the third is the one that rots silently:

1. **Keyword mode touches no embedding backend.** Not "it works anyway" — it never
   probes, so ``semantic`` comes back ``null``. That is the mechanism behind the
   acceptance criterion, and asserting only "it returned 200" would not pin it.
2. **Degradation is data, not an exception.** A query that could not use the
   semantic leg returns keyword results *plus* the reason. A frontend that renders
   the results without the reason is showing a worse answer than it thinks.
3. **The reason has to survive the trip.** ``warnings`` is prose; ``semantic`` is
   structured. The page needs the structured one to tell "this index has no
   vectors, go build them" apart from "nobody approved this query yet", and those
   two are the same sentence shape in ``warnings``.

The ``strict_semantic`` 500 that this module used to pin as-is is gone as of B-8:
the route now classifies why semantic retrieval is unavailable *before* retrieval
starts, so the same condition is a 400 here and under ``mode=semantic`` alike. See
``test_strict_semantic_reports_the_setup_problem_not_a_server_error``, which is the
test that used to record the discrepancy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.paths import MISSING_INDEX_MESSAGE
from obsai.application.search import SEMANTIC_INDEX_MISSING, SEMANTIC_NOT_APPROVED
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"

#: The two notes the index is built from. ``Redis.md`` carries a matching tag so
#: the filter tests have something to narrow; ``Deploy.md`` only links to it.
REDIS = "Backend/Redis.md"
DEPLOY = "Guides/Deploy.md"


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A two-note Vault with a keyword-only index, and the index path.

    ``IncrementalIndexer`` writes chunks and FTS rows but never vectors, so the
    embedding generation is absent from the store — which is exactly the state a
    Vault is in before ``obsai index embeddings`` has ever been run.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, REDIS, "---\ntags: [redis]\n---\n# Redis\n\nRedis cache strategy.\n")
    write(vault, DEPLOY, "# Deploy\n\nSee [[Backend/Redis]].\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def client_for(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def settings_for(vault: Path | None, database_path: Path) -> Settings:
    """Settings with the index configured and **no embedding configuration**.

    ``vault`` is optional because search reads the derived index, not the notes:
    the route never resolves ``vault.path``. Passing ``None`` is how the "search
    does not need a Vault" test states that.
    """
    vault_section = {} if vault is None else {"path": vault}
    return Settings(vault=vault_section, index={"database": database_path})


def search_of(settings: Settings, body: dict) -> tuple[int, dict]:
    with client_for(settings) as client:
        response = client.post("/api/v1/search", json=body)
    return response.status_code, response.json()


def search_ok(settings: Settings, body: dict) -> dict:
    status, payload = search_of(settings, body)
    assert status == 200, payload
    return payload


def paths_of(payload: dict) -> list[str]:
    return [result["path"] for result in payload["results"]]


# --------------------------------------------------------------------------- #
# Keyword mode: the acceptance criterion
# --------------------------------------------------------------------------- #


def test_keyword_mode_works_with_no_embedding_configuration(tmp_path: Path) -> None:
    """B-5's acceptance criterion, stated as the state it actually needs.

    The settings here have ``EmbeddingConfig()`` untouched — no key, no
    ``price_per_million_tokens_usd``, nothing. If keyword search reached for an
    embedding backend, the missing price alone would raise ``ConfigError``.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "keyword"})

    assert paths_of(payload) == [REDIS, DEPLOY]
    assert all(result["source"] == "keyword" for result in payload["results"])


def test_keyword_mode_never_probes_the_semantic_backend(tmp_path: Path) -> None:
    """``semantic: null`` is the mechanism, not an omission.

    The route only probes when ``requires_semantic`` is set. Asserting the null
    keeps a future refactor from probing unconditionally "so the response shape is
    uniform" — which would make keyword mode depend on the embedding configuration.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "keyword"})

    assert payload["semantic"] is None
    assert payload["warnings"] == []


def test_keyword_results_are_ranked_not_merely_returned(tmp_path: Path) -> None:
    """Both notes match ``redis``; only one is about it. Order has to reflect that."""
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "keyword"})

    scores = [result["score"] for result in payload["results"]]
    assert scores == sorted(scores, reverse=True)
    assert paths_of(payload)[0] == REDIS


# --------------------------------------------------------------------------- #
# Degradation is visible
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["hybrid", "graph"])
def test_a_mode_that_needs_semantics_still_answers(tmp_path: Path, mode: str) -> None:
    """Hybrid and graph degrade to keyword results instead of erroring.

    ``graph`` is included because it wraps the hybrid retriever, so a regression
    there would be reported as "graph is broken" while the cause sits one layer
    down.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": mode})

    assert paths_of(payload) == [REDIS, DEPLOY]
    assert payload["warnings"], "降级发生了，但 warnings 是空的 —— 页面会当作一切正常"


def test_the_notice_reuses_the_cli_wording(tmp_path: Path) -> None:
    """One sentence for the same condition, or the user gets two names for it."""
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "hybrid"})

    assert payload["warnings"] == [SEMANTIC_INDEX_MISSING]


def test_the_probe_reports_a_setup_problem_not_an_approval_problem(tmp_path: Path) -> None:
    """The distinction ``warnings`` cannot carry.

    With no vectors in the store the honest reason is "go build the index", not
    "this query was not approved". They produce the same shape of sentence, and
    the page shows a different next step for each, so the structured field is what
    it has to switch on.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "hybrid"})

    assert payload["semantic"]["reason"] == SEMANTIC_INDEX_MISSING
    assert payload["semantic"]["reason"] != SEMANTIC_NOT_APPROVED
    assert payload["semantic"]["consent"] is None


def test_an_unapproved_query_is_reported_as_such(tmp_path: Path) -> None:
    """The other branch: the backend *could* answer, but nobody has approved it.

    The route deliberately never approves, so a Vault with vectors in place still
    degrades — and the reason flips to the approval one. Without this test the
    previous test would pass even if the reason were hardcoded.
    """
    vault, database_path = build_vault(tmp_path)
    with Database(database_path) as database:
        _seed_generation(database, settings_for(vault, database_path))

    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "hybrid"})

    assert payload["semantic"]["consent"] is not None, "语义后端可用，应该给出挑战而不是降级"
    assert payload["semantic"]["reason"] == "", "consent 非空时 reason 必须为空"
    assert payload["warnings"] == [SEMANTIC_NOT_APPROVED]
    assert paths_of(payload) == [REDIS, DEPLOY]


def _seed_generation(database: Database, settings: Settings) -> None:
    """Register the configured embedding generation so ``has_generation`` is true.

    No vectors and no provider call: the probe only asks whether the generation
    exists in the store, so writing the generation row is enough to reach the
    "would send a remote query" branch.
    """
    from obsai.application.embedding import build_embedding_pipeline

    store, pipeline = build_embedding_pipeline(database, settings)
    store.ensure_generation(pipeline.generation)


# --------------------------------------------------------------------------- #
# Mandatory semantics refuse instead of degrading
# --------------------------------------------------------------------------- #


def test_semantic_mode_refuses_rather_than_degrading(tmp_path: Path) -> None:
    """``--mode semantic`` is a promise the results are semantic. Keep it or fail."""
    vault, database_path = build_vault(tmp_path)
    status, payload = search_of(settings_for(vault, database_path), {"query": "redis", "mode": "semantic"})

    assert status == 400
    assert payload["error"]["code"] == "semantic_index_missing"
    assert payload["error"]["message"] == SEMANTIC_INDEX_MISSING


def test_strict_semantic_reports_the_setup_problem_not_a_server_error(tmp_path: Path) -> None:
    """The rough edge §21.4 recorded, closed by B-8.

    Before B-8 this arrived as a 500: ``strict_semantic`` did not raise inside the
    probe, ``HybridRetriever`` raised ``EmbeddingError``, and that class has no
    entry in ``STATUS_BY_ERROR``. The very same condition was a 400 under
    ``mode=semantic`` — one problem described with two statuses, which is what a UI
    cannot render honestly.

    The fix was not to map ``EmbeddingError`` to 400 wholesale: that class also
    covers a provider that failed mid-query, and telling a user their configuration
    is wrong during a real outage is worse than a wrong status class. The route now
    classifies the two ways semantic retrieval can be unavailable *before* retrieval
    starts, using the probe's structured ``failure``. ``EmbeddingError`` keeps its
    500 for the mid-query case, where 500 is right.
    """
    vault, database_path = build_vault(tmp_path)
    status, payload = search_of(
        settings_for(vault, database_path),
        {"query": "redis", "mode": "hybrid", "strict_semantic": True},
    )

    assert status == 400
    assert payload["error"]["code"] == "semantic_index_missing"
    assert payload["error"]["message"] == SEMANTIC_INDEX_MISSING


# --------------------------------------------------------------------------- #
# What the endpoint needs
# --------------------------------------------------------------------------- #


def test_a_missing_index_is_a_config_error_not_a_crash(tmp_path: Path) -> None:
    """The index is a rebuildable artefact; not having one is a normal state.

    Same message as the CLI, so the page can tell the user the exact command
    rather than paraphrasing it.
    """
    vault, _ = build_vault(tmp_path)
    status, payload = search_of(
        settings_for(vault, tmp_path / "never-built.db"), {"query": "redis", "mode": "keyword"}
    )

    assert status == 400
    assert payload["error"]["code"] == "config"
    assert payload["error"]["message"] == MISSING_INDEX_MESSAGE


def test_search_reads_the_index_and_not_the_vault(tmp_path: Path) -> None:
    """No ``vault.path`` at all, and the query still works.

    Worth pinning because the overview page has a "no Vault configured" branch and
    it would be easy to assume search shares it. It does not: results come out of
    the derived index, and a Vault that has moved since the last ``index update``
    is still queryable.
    """
    _, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(None, database_path), {"query": "redis", "mode": "keyword"})

    assert paths_of(payload) == [REDIS, DEPLOY]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"query": "redis", "mode": "fuzzy"}, id="unknown-mode"),
        pytest.param({"query": "redis", "limit": 0}, id="limit-below-one"),
        pytest.param({"query": "redis", "limit": -3}, id="negative-limit"),
        pytest.param({}, id="no-query"),
        pytest.param({"query": "redis", "bogus": 1}, id="unknown-field"),
    ],
)
def test_a_malformed_body_is_an_invalid_request(tmp_path: Path, body: dict) -> None:
    """Every rejection uses the shared envelope, so the page needs one parser.

    ``SearchRequest`` inherits ``extra="forbid"`` from the application DTO, so a
    misspelled ``filters`` key fails loudly here instead of being dropped and
    returning unfiltered results that look correct.
    """
    vault, database_path = build_vault(tmp_path)
    status, payload = search_of(settings_for(vault, database_path), body)

    assert status == 422
    assert payload["error"]["code"] == "invalid_request"
    assert payload["error"]["details"], "details 里才有出错的是哪个字段"


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #


def test_the_top_level_shape_is_results_warnings_and_probe(tmp_path: Path) -> None:
    """``degraded`` is a property, not a field — the page derives it from ``warnings``."""
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "hybrid"})

    assert set(payload) == {"results", "warnings", "semantic"}
    assert payload["warnings"], "这条断言依赖降级真的发生，否则 degraded 无从验证"


def test_every_result_carries_the_fields_the_page_renders(tmp_path: Path) -> None:
    """The exact key set, because a renamed field is ``undefined`` at runtime.

    ``score`` is deliberately *not* asserted to be a percentage or to sit in
    ``[0, 1]``: its scale depends on the mode. See the next test.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "keyword"})

    assert payload["results"], "没有结果的话下面几条断言会在空列表上恒真"
    for result in payload["results"]:
        assert set(result) == {
            "chunk_id",
            "note_id",
            "path",
            "title",
            "heading_path",
            "snippet",
            "score",
            "source",
            "sources",
        }
        assert isinstance(result["heading_path"], list)
        assert isinstance(result["sources"], list)
        assert isinstance(result["score"], float)
        assert result["title"], "标题不能是空串，页面拿它当链接文字"


def test_the_score_scale_depends_on_the_mode(tmp_path: Path) -> None:
    """Why the page must not render a score as a percentage.

    Keyword mode reports raw BM25 (tiny, unbounded), hybrid reports RRF fusion
    weights (roughly ``1 / (60 + rank)``, so around 0.016 for the top hit). The
    same note therefore scores ~1e-6 in one mode and ~1e-2 in the other. A single
    "relevance %" widget would read as "0%" for every keyword result.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    keyword = search_ok(settings, {"query": "redis", "mode": "keyword"})
    hybrid = search_ok(settings, {"query": "redis", "mode": "hybrid"})

    keyword_top = keyword["results"][0]
    hybrid_top = hybrid["results"][0]
    assert keyword_top["path"] == hybrid_top["path"] == REDIS

    assert hybrid_top["score"] / keyword_top["score"] > 100, "两个模式的分数量纲应当差得很远"
    assert keyword_top["sources"] == [], "keyword 是单一来源，没有融合来源"
    assert hybrid_top["sources"] == ["keyword"], "hybrid 降级后只有 keyword 一路参与融合"


def test_a_query_that_matches_nothing_is_an_empty_list(tmp_path: Path) -> None:
    """Empty is a result, not an error: the page shows "没有匹配" instead of a failure."""
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "zzzznotfound", "mode": "keyword"})

    assert payload["results"] == []
    assert payload["warnings"] == []


@pytest.mark.parametrize("query", ["", "   "])
def test_a_blank_query_returns_nothing_rather_than_failing(tmp_path: Path, query: str) -> None:
    """The input box is empty on first paint and every keystroke is a request.

    Returning an empty list keeps that path free of error states to render.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": query, "mode": "keyword"})

    assert payload["results"] == []


def test_limit_caps_the_number_of_results(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "keyword", "limit": 1})

    assert paths_of(payload) == [REDIS]


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_a_tag_filter_narrows_the_results(tmp_path: Path) -> None:
    """``Deploy.md`` mentions Redis but is not tagged ``redis``."""
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)

    matching = search_ok(settings, {"query": "redis", "mode": "keyword", "filters": {"tags": ["redis"]}})
    assert paths_of(matching) == [REDIS]

    empty = search_ok(settings, {"query": "redis", "mode": "keyword", "filters": {"tags": ["nope"]}})
    assert empty["results"] == []


def test_a_folder_filter_narrows_the_results(tmp_path: Path) -> None:
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(
        settings_for(vault, database_path),
        {"query": "redis", "mode": "keyword", "filters": {"folder": "Guides"}},
    )

    assert paths_of(payload) == [DEPLOY]


def test_filters_are_echoed_nowhere_but_applied(tmp_path: Path) -> None:
    """The response does not repeat the request back.

    Stated as a test because "echo the filters so the UI can show active chips" is
    a tempting addition — and it would mean two sources of truth for what the user
    asked for, one of which can drift from what was actually applied.
    """
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(
        settings_for(vault, database_path),
        {"query": "redis", "mode": "keyword", "filters": {"tags": ["redis"]}},
    )

    assert "filters" not in payload
    assert "query" not in payload
    assert json.dumps(payload, ensure_ascii=False).count('"tags"') == 0
