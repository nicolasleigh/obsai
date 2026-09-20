"""B-6 acceptance: ``POST /api/v1/ask`` answers, abstains, or refuses — visibly.

The plan's acceptance criterion for the ask page is stated as three negatives:
abstention must be explained, a failed citation check must be explained, and a
missing API key must produce guidance rather than a stack trace. Each one is a
place where the obvious implementation shows the user something useless, so each
gets its own test rather than being folded into "it returns 200".

Three properties are pinned here:

1. **Abstention is a 200, not an error.** ``abstained: true`` is a truthful
   answer to a question the index cannot support. A 4xx would make the page
   render a failure state for a system that is working exactly as designed.
2. **The three abstentions are distinguishable.** An empty question, no evidence,
   and a failed citation check all arrive with the same ``abstained: true`` flag
   and the same JSON shape. Only ``text`` and ``warnings`` separate them, which
   is precisely what ``web/src/lib/ask.ts`` switches on.
3. **A missing API key has its own code.** ``missing_credential`` rather than
   ``config``: the fix is an environment variable, and the ``config`` message
   points at ``config.toml``, which in this case is already correct.

No test here may reach the network. Every test that would arrive at the provider
either deletes ``OPENAI_API_KEY`` (so the call fails before it is made) or
replaces the provider outright. A developer machine with a real key set must not
turn these into live API calls.
"""

from __future__ import annotations

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

REDIS = "Backend/Redis.md"
DEPLOY = "Guides/Deploy.md"

#: The three abstention messages, pinned as literals rather than imported.
#: They are the user-visible contract — the page renders them verbatim — and the
#: domain stores them as inline strings with no constant to import. Pinning them
#: here means a reworded message fails a test instead of silently changing the UI.
NO_QUESTION = "请提供问题。"
NO_EVIDENCE = "未找到可用于回答的笔记证据。"
BAD_CITATIONS = "无法从检索到的证据生成带有效引用的回答。"
CITATION_WARNING = "Model answer had missing or invalid citations"


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A two-note Vault with a keyword-only index, and the index path.

    ``IncrementalIndexer`` writes chunks and FTS rows but never vectors, so the
    embedding generation is absent — the state a Vault is in before
    ``obsai index embeddings`` has ever run. Both notes match ``redis``; only the
    first is about it, which is what makes ``S1`` and ``S2`` deterministic.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, REDIS, "---\ntags: [redis]\n---\n# Redis\n\nRedis cache strategy.\n")
    write(vault, DEPLOY, "# Deploy\n\nSee [[Backend/Redis]].\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def settings_for(vault: Path | None, database_path: Path) -> Settings:
    """Settings with the index configured and **no embedding configuration**.

    ``vault`` is optional because answering reads the derived index and the
    evidence repository, never the notes.
    """
    vault_section = {} if vault is None else {"path": vault}
    return Settings(vault=vault_section, index={"database": database_path})


def client_for(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def ask_of(settings: Settings, body: dict) -> tuple[int, dict]:
    with client_for(settings) as client:
        response = client.post("/api/v1/ask", json=body)
    return response.status_code, response.json()


def ask_ok(settings: Settings, body: dict) -> dict:
    status, payload = ask_of(settings, body)
    assert status == 200, payload
    return payload


def seed_generation(database_path: Path, settings: Settings) -> None:
    """Register the configured embedding generation so the probe finds it.

    No vectors and no provider call: the probe only asks whether the generation
    exists, so writing the generation row is enough to reach the "a remote query
    would be sent" branch.
    """
    from obsai.application.embedding import build_embedding_pipeline

    with Database(database_path) as database:
        store, pipeline = build_embedding_pipeline(database, settings)
        store.ensure_generation(pipeline.generation)


# --------------------------------------------------------------------------- #
# Abstention is a success
# --------------------------------------------------------------------------- #


def test_a_question_with_no_evidence_abstains_rather_than_failing(tmp_path: Path) -> None:
    """The index has nothing to say, so the answer is "I don't know" — a 200.

    The page has to render this as a normal state. A 4xx would send it down the
    error path, where the only thing left to show is a generic failure message
    that says less than the abstention text does.
    """
    vault, database_path = build_vault(tmp_path)
    payload = ask_ok(settings_for(vault, database_path), {"query": "zzzznotfound"})

    assert payload["abstained"] is True
    assert payload["text"] == NO_EVIDENCE
    assert payload["citations"] == []


def test_the_no_evidence_abstention_keeps_the_degradation_notice(tmp_path: Path) -> None:
    """Why the abstention is not just ``text``: it also carries the reason.

    The retriever degraded to keyword because there are no vectors, and that is
    part of the honest answer. Dropping ``warnings`` on the abstention path would
    tell the user "no evidence" when the real message is "no evidence *in the
    keyword half*, because the semantic half is not built".
    """
    vault, database_path = build_vault(tmp_path)
    payload = ask_ok(settings_for(vault, database_path), {"query": "zzzznotfound"})

    assert payload["warnings"] == [SEMANTIC_INDEX_MISSING]


@pytest.mark.parametrize("query", ["", "   ", "\n"])
def test_a_blank_question_abstains_without_retrieving(tmp_path: Path, query: str) -> None:
    """An empty question is answered before the retriever runs, so no warnings.

    The empty ``warnings`` list is the observable proof: a degradation notice
    only appears once the retriever has been consulted, and it never is.
    """
    vault, database_path = build_vault(tmp_path)
    payload = ask_ok(settings_for(vault, database_path), {"query": query})

    assert payload["abstained"] is True
    assert payload["text"] == NO_QUESTION
    assert payload["citations"] == []
    assert payload["warnings"] == []


def test_a_blank_question_still_carries_a_probe(tmp_path: Path) -> None:
    """The response shape does not change with the abstention reason.

    Asserted as "the field is present", not as "the reason is X": the reason here
    describes the *setup* (no vectors), not the emptiness, because the probe only
    knows the query string. That mismatch is exactly why the page's rule is
    "when ``abstained`` is true the text is the message and the probe is
    diagnostic only" — see ``web/src/lib/ask.test.ts``. Pinning the current
    reason string would freeze the wart instead of the rule.
    """
    vault, database_path = build_vault(tmp_path)
    payload = ask_ok(settings_for(vault, database_path), {"query": ""})

    assert payload["semantic"] is not None
    assert payload["semantic"]["consent"] is None


# --------------------------------------------------------------------------- #
# The citation check
# --------------------------------------------------------------------------- #


def test_a_valid_citation_comes_back_resolved(tmp_path: Path, stub_llm) -> None:
    """The happy path, and the shape the page's ``[S1]`` badge is built from."""
    vault, database_path = build_vault(tmp_path)
    stub_llm("Redis 是内存缓存。[S1]")
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert payload["abstained"] is False
    assert payload["text"] == "Redis 是内存缓存。[S1]"
    assert len(payload["citations"]) == 1

    citation = payload["citations"][0]
    assert citation["citation_id"] == "S1"
    assert citation["path"] == REDIS
    assert citation["title"]
    assert citation["location"].startswith(REDIS)
    assert citation["location"].endswith("> Redis"), "location 是 path > heading 形式"


def test_citations_follow_the_text_not_the_numbering(tmp_path: Path, stub_llm) -> None:
    """Order is first appearance in the answer, which is the order the page shows.

    ``validate_citations`` de-duplicates with ``dict.fromkeys``, so a repeated
    ``[S1]`` appears once and ``[S2]`` before ``[S1]`` stays that way. Sorting by
    citation number would silently reorder the reference list away from the prose.
    """
    vault, database_path = build_vault(tmp_path)
    stub_llm("先 [S2]，再 [S1]，又 [S2]。")
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert [item["citation_id"] for item in payload["citations"]] == ["S2", "S1"]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("Redis 是内存缓存。", id="no-citation-at-all"),
        pytest.param("Redis 是内存缓存。[S9]", id="citation-that-does-not-exist"),
        pytest.param("   ", id="empty-answer"),
    ],
)
def test_an_answer_that_fails_the_citation_check_abstains(
    tmp_path: Path, stub_llm, answer: str
) -> None:
    """The model said something the evidence cannot back, so nothing is shown.

    Pinned as an abstention rather than as an error because that is the design:
    the alternative — displaying an uncited answer — is the failure mode the
    citation check exists to prevent. The answer text is discarded entirely, not
    shown with a warning attached.
    """
    vault, database_path = build_vault(tmp_path)
    stub_llm(answer)
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert payload["abstained"] is True
    assert payload["text"] == BAD_CITATIONS
    assert payload["citations"] == []
    assert answer not in payload["text"], "被丢弃的回答正文不能出现在 abstention 文案里"


def test_the_citation_failure_is_appended_to_the_degradation_notices(
    tmp_path: Path, stub_llm
) -> None:
    """Both reasons survive, degradation first, and the page shows both.

    Appended rather than replacing: the user needs to know the answer was
    discarded *and* that the retrieval behind it was degraded.
    """
    vault, database_path = build_vault(tmp_path)
    stub_llm("Redis 是内存缓存。")
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert payload["warnings"] == [SEMANTIC_INDEX_MISSING, CITATION_WARNING]


# --------------------------------------------------------------------------- #
# Missing credentials get their own code
# --------------------------------------------------------------------------- #


def test_a_missing_api_key_is_a_missing_credential_not_a_config_error(
    tmp_path: Path
) -> None:
    """B-6's third acceptance criterion, stated as the code the page switches on.

    ``config`` would be wrong advice: its message points the user at
    ``config.toml``, which is fine — what is missing is an environment variable.
    The 400 status is unchanged, so this is a refinement, not a behaviour change.
    """
    vault, database_path = build_vault(tmp_path)
    status, payload = ask_of(settings_for(vault, database_path), {"query": "redis"})

    assert status == 400
    assert payload["error"]["code"] == "missing_credential"
    assert payload["error"]["type"] == "MissingCredentialError"
    assert "OPENAI_API_KEY" in payload["error"]["message"]


def test_a_missing_index_still_reports_the_plain_config_code(tmp_path: Path) -> None:
    """The contrast that justifies the new class.

    Both conditions are 400s, and before B-6 both were ``config``. The page shows
    a different instruction for each, so the codes have to differ — and this test
    is what stops a future change from collapsing them again.
    """
    vault, _ = build_vault(tmp_path)
    status, payload = ask_of(
        settings_for(vault, tmp_path / "never-built.db"), {"query": "redis"}
    )

    assert status == 400
    assert payload["error"]["code"] == "config"
    assert payload["error"]["message"] == MISSING_INDEX_MESSAGE


def test_asking_reaches_the_provider_without_a_vault(tmp_path: Path) -> None:
    """No ``vault.path``, and the question still gets as far as the model.

    Reaching the provider *is* the assertion: it means retrieval and evidence
    lookup both succeeded from the derived index alone. The request then stops at
    the missing key, which is the proof it was not stopped earlier.
    """
    _, database_path = build_vault(tmp_path)
    status, payload = ask_of(settings_for(None, database_path), {"query": "redis"})

    assert status == 400
    assert payload["error"]["code"] == "missing_credential"


# --------------------------------------------------------------------------- #
# The route probes but never approves
# --------------------------------------------------------------------------- #


def test_a_probe_that_could_send_a_remote_query_still_degrades(
    tmp_path: Path, stub_llm
) -> None:
    """The consent protocol, from the ask side.

    With vectors in place the probe returns a challenge instead of a reason — and
    the route still does not approve it, so the semantic leg stays out and the
    answer is built from keyword evidence. ``reason`` is empty and ``consent`` is
    set, which is the "exactly one field" contract ``SemanticProbe`` documents.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)
    stub_llm("Redis 是内存缓存。[S1]")

    payload = ask_ok(settings, {"query": "redis"})

    assert payload["semantic"]["consent"] is not None, "语义后端可用，应该给出挑战而不是降级"
    assert payload["semantic"]["reason"] == "", "consent 非空时 reason 必须为空"
    assert payload["warnings"] == [SEMANTIC_NOT_APPROVED]
    assert payload["abstained"] is False, "未批准只应降级，不应让回答失败"


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-query"),
        pytest.param({"query": "redis", "bogus": 1}, id="unknown-field"),
        pytest.param({"query": 5}, id="query-not-a-string"),
        pytest.param({"query": None}, id="query-null"),
    ],
)
def test_a_malformed_body_is_an_invalid_request(tmp_path: Path, body: dict) -> None:
    """``AskRequest`` inherits ``extra="forbid"``, so a typo fails loudly.

    ``{"query": 5}`` is included because Pydantic v2 does *not* coerce an integer
    to a string: a question that arrives as a number is a client bug, and
    silently stringifying it would hide it.
    """
    vault, database_path = build_vault(tmp_path)
    status, payload = ask_of(settings_for(vault, database_path), body)

    assert status == 422
    assert payload["error"]["code"] == "invalid_request"
    assert payload["error"]["details"], "details 里才有出错的是哪个字段"


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #


def test_the_top_level_shape_is_text_citations_warnings_and_probe(
    tmp_path: Path, stub_llm
) -> None:
    """The exact key set, because a renamed field is ``undefined`` at runtime."""
    vault, database_path = build_vault(tmp_path)
    stub_llm("Redis 是内存缓存。[S1]")
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert set(payload) == {"text", "citations", "warnings", "abstained", "semantic"}


def test_every_citation_carries_the_fields_the_page_renders(
    tmp_path: Path, stub_llm
) -> None:
    """``location`` is pre-composed so neither adapter re-derives ``path > heading``."""
    vault, database_path = build_vault(tmp_path)
    stub_llm("先 [S1]，再 [S2]。")
    payload = ask_ok(settings_for(vault, database_path), {"query": "redis"})

    assert payload["citations"], "没有引用的话下面几条断言会在空列表上恒真"
    for citation in payload["citations"]:
        assert set(citation) == {
            "citation_id",
            "note_id",
            "chunk_id",
            "path",
            "title",
            "heading_path",
            "block_id",
            "location",
            "truncated",
        }
        assert isinstance(citation["heading_path"], list)
        assert isinstance(citation["truncated"], bool)
        assert citation["title"], "标题不能是空串，页面拿它当链接文字"
        assert citation["path"] in {REDIS, DEPLOY}


def test_the_question_is_not_echoed_back(tmp_path: Path) -> None:
    """One source of truth for what was asked, and it is the request.

    The same reasoning as ``test_filters_are_echoed_nowhere_but_applied``: echoing
    invites the page to render the server's copy while the input box shows
    another, and the two drift as soon as the user types again.
    """
    vault, database_path = build_vault(tmp_path)
    payload = ask_ok(settings_for(vault, database_path), {"query": "zzzznotfound"})

    assert "query" not in payload
