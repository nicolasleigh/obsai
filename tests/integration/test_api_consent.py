"""B-8 acceptance: the remote-embedding consent protocol, end to end over HTTP.

The plan's acceptance criterion is negative — *declining must not send a remote
request, and must degrade to keyword results visibly*. Pinning that needs both
halves of the protocol, a challenge from ``/search`` and an approval from
``/consent``, because a test that only checked "the dialog renders" would pass on a
build where approving did nothing at all.

The Vault fixture is the same two-note one ``test_api_search`` uses, plus one
addition: :func:`seed_generation` writes the embedding generation row so the probe
reaches the "a remote query *would* be sent" branch. No vectors and no provider call
are involved — ``has_generation`` is a row lookup, which is the whole reason this can
be tested without a network. If a semantic backend were ever reached for real, these
tests would fail rather than quietly pass, since there is no key and no price.

Two assertions are deliberately indirect:

* **"No remote request" is counted, not observed.** :func:`semantic_calls` records
  whether ``build_semantic_retriever`` ran, because that is the only door a query
  can leave through and ``VectorRetriever`` refuses to embed without ``approved=True``.
  Watching for a failed network call instead would pass for a dozen unrelated reasons.
* **"The approval was honoured" is read off the warning.** Without one the notice is
  the approval message; with one the code goes on to build the retriever and the
  notice becomes a backend failure. That difference is what proves the approval
  changed the path taken, rather than merely being accepted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import obsai.application.search as search_module
from obsai.api.app import create_app
from obsai.api.deps import get_settings
from obsai.application.search import SEMANTIC_INDEX_MISSING, SEMANTIC_NOT_APPROVED
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer
from obsai.storage import Database, IndexRepository

BASE_URL = "http://127.0.0.1:8000"

REDIS = "Backend/Redis.md"
DEPLOY = "Guides/Deploy.md"

#: How far past the challenge's TTL the "expired" tests reach. The TTL is 15
#: minutes; one extra minute is enough to be unambiguous without pretending to know
#: the exact value, which the application owns.
BEYOND_TTL = timedelta(minutes=16)


def write(vault: Path, path: str, content: str) -> Path:
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A two-note Vault with a keyword-only index, and the index path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    write(vault, REDIS, "---\ntags: [redis]\n---\n# Redis\n\nRedis cache strategy.\n")
    write(vault, DEPLOY, "# Deploy\n\nSee [[Backend/Redis]].\n")
    database_path = tmp_path / "index.db"
    with Database(database_path) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
    return vault, database_path


def settings_for(vault: Path | None, database_path: Path) -> Settings:
    vault_section = {} if vault is None else {"path": vault}
    return Settings(vault=vault_section, index={"database": database_path})


def client_for(settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, base_url=BASE_URL)


def search_of(settings: Settings, body: dict) -> tuple[int, dict]:
    with client_for(settings) as client:
        response = client.post("/api/v1/search", json=body)
    return response.status_code, response.json()


def search_ok(settings: Settings, body: dict) -> dict:
    status, payload = search_of(settings, body)
    assert status == 200, payload
    return payload


def seed_generation(database_path: Path, settings: Settings) -> None:
    """Register the configured embedding generation so ``has_generation`` is true.

    The probe only asks whether the generation exists in the store, so writing the
    row is enough to reach the "would send a remote query" branch.
    """
    from obsai.application.embedding import build_embedding_pipeline

    with Database(database_path) as database:
        store, pipeline = build_embedding_pipeline(database, settings)
        store.ensure_generation(pipeline.generation)


def challenge_for(settings: Settings, query: str = "redis") -> dict:
    """The consent challenge a search hands back for ``query``."""
    payload = search_ok(settings, {"query": query, "mode": "hybrid"})
    consent = payload["semantic"]["consent"]
    assert consent is not None, "fixture 没种 generation，探针走不到可批准的分支"
    return consent


def approve_of(settings: Settings, consent: dict, *, approved: bool = True) -> tuple[int, dict]:
    with client_for(settings) as client:
        response = client.post("/api/v1/consent", json={**consent, "approved": approved})
    return response.status_code, response.json()


def approve_ok(settings: Settings, consent: dict, *, approved: bool = True) -> dict:
    status, payload = approve_of(settings, consent, approved=approved)
    assert status == 200, payload
    return payload


@pytest.fixture
def semantic_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record whether the semantic retriever was assembled at all.

    This is the mechanism behind "declining sends no remote request". A query can
    only leave the machine through a retriever built here, and ``VectorRetriever``
    refuses to embed without ``approved=True``; counting the construction is
    therefore a stronger statement than watching for a failed network call, which
    could be absent for a dozen unrelated reasons.
    """
    calls: list[str] = []
    original = search_module.build_semantic_retriever

    def spy(database: Database, settings: Settings):  # type: ignore[no-untyped-def]
        calls.append("built")
        return original(database, settings)

    monkeypatch.setattr(search_module, "build_semantic_retriever", spy)
    return calls


# --------------------------------------------------------------------------- #
# The probe names its reason in a form a caller can branch on
# --------------------------------------------------------------------------- #


def test_the_probe_names_the_setup_problem(tmp_path: Path) -> None:
    """No vectors is ``index_missing``, and that is not the same as "not approved"."""
    vault, database_path = build_vault(tmp_path)
    payload = search_ok(settings_for(vault, database_path), {"query": "redis", "mode": "hybrid"})

    assert payload["semantic"]["failure"] == "index_missing"
    assert payload["semantic"]["reason"] == SEMANTIC_INDEX_MISSING
    assert payload["semantic"]["consent"] is None


def test_the_consent_and_failure_fields_are_exclusive(tmp_path: Path) -> None:
    """Both directions of the contract, because either one alone is satisfiable.

    ``consent`` set means a challenge exists; ``failure`` set means none can. A
    caller switching on one without the other would read the opposite state on the
    branch it forgot to test.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)

    without_vectors = search_ok(settings, {"query": "redis", "mode": "hybrid"})["semantic"]
    assert without_vectors["consent"] is None
    assert without_vectors["failure"] is not None
    assert without_vectors["reason"] != ""

    seed_generation(database_path, settings)
    with_vectors = search_ok(settings, {"query": "redis", "mode": "hybrid"})["semantic"]
    assert with_vectors["consent"] is not None
    assert with_vectors["failure"] is None
    assert with_vectors["reason"] == ""


# --------------------------------------------------------------------------- #
# Approving and declining
# --------------------------------------------------------------------------- #


def test_approving_a_challenge_returns_a_nonce(tmp_path: Path) -> None:
    """The nonce is the thing ``/search`` is handed back; without it the loop is open."""
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    consent = challenge_for(settings)
    approval = approve_ok(settings, consent)

    assert approval["approved"] is True
    assert len(approval["nonce"]) == 32
    int(approval["nonce"], 16)
    for field in ("consent_id", "query_hash", "generation_id"):
        assert approval[field] == consent[field], f"{field} 必须原样回显，否则批准覆盖不到挑战"


def test_declining_is_expressible(tmp_path: Path) -> None:
    """A refusal is a decision, not an error — ``search`` fails closed on it.

    The search page never takes this path (it simply does not ask again), but the
    protocol has two answers and a route that could only record one of them would
    not be implementing it.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    approval = approve_ok(settings, challenge_for(settings), approved=False)

    assert approval["approved"] is False


def test_an_expired_challenge_is_gone_not_broken(tmp_path: Path) -> None:
    """410, and the message tells the user to search again rather than edit a file.

    A dialog left open past the TTL is the ordinary way to arrive here, so the
    recovery has to be obvious. Pointing at ``config.toml`` — which is what the
    generic config message does — would send the user to a file that is fine.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    consent = challenge_for(settings)
    stale = dict(consent)
    stale["expires_at"] = (datetime.now(timezone.utc) - BEYOND_TTL).isoformat()

    status, payload = approve_of(settings, stale)

    assert status == 410
    assert payload["error"]["code"] == "consent_expired"
    assert "search again" in payload["error"]["message"]


def test_approving_needs_neither_vault_nor_index(tmp_path: Path) -> None:
    """Everything an approval needs is in the challenge it was handed.

    The route has no ``get_settings`` dependency for this reason, and the test says
    so by pointing the settings at an index that does not exist: if the route ever
    grew a dependency on the index, this would fail rather than pass by accident.
    """
    vault, database_path = build_vault(tmp_path)
    seeded = settings_for(vault, database_path)
    seed_generation(database_path, seeded)
    consent = challenge_for(seeded)

    nowhere = settings_for(None, tmp_path / "never-built.db")
    status, payload = approve_of(nowhere, consent)

    assert status == 200
    assert payload["approved"] is True


# --------------------------------------------------------------------------- #
# Refusing to degrade asks for a decision instead
# --------------------------------------------------------------------------- #


def test_semantic_mode_without_an_approval_asks_for_one(tmp_path: Path) -> None:
    """``mode=semantic`` cannot degrade, so an unanswered challenge is a 409.

    Not a 400: nothing about the request is wrong. The caller is one decision short
    of a servable request, and the status says so.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    status, payload = search_of(settings, {"query": "redis", "mode": "semantic"})

    assert status == 409
    assert payload["error"]["code"] == "consent_required"


def test_strict_semantic_without_an_approval_asks_for_one(tmp_path: Path) -> None:
    """The same condition reached through the other switch.

    ``strict_semantic`` and ``mode=semantic`` differ in what they do *after*
    retrieval runs; before it runs, both are promises that keyword results will not
    be substituted, and both need the same answer.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    status, payload = search_of(
        settings, {"query": "redis", "mode": "hybrid", "strict_semantic": True}
    )

    assert status == 409
    assert payload["error"]["code"] == "consent_required"


def test_the_refusal_carries_the_challenge(tmp_path: Path) -> None:
    """The UI has to be able to show the dialog from the refusal alone.

    Without this the caller learns that a decision is needed but not what it costs,
    and would need a second request to find out — which is the round trip the
    challenge was invented to avoid.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    status, payload = search_of(settings, {"query": "redis", "mode": "semantic"})

    assert status == 409
    consent = payload["error"]["details"]["consent"]
    fresh = challenge_for(settings)

    # ``expires_at`` differs because it is minted per probe. Everything the dialog
    # displays, and everything the approval will be bound to, must not — an id that
    # changed per probe is an approval that can never cover the next request.
    for field in ("consent_id", "query_hash", "generation_id", "query_tokens"):
        assert consent[field] == fresh[field], f"{field} 必须跨探测稳定"
    assert consent["estimated_cost_usd"] == fresh["estimated_cost_usd"]
    assert consent["expires_at"] != ""


def test_an_approval_for_a_different_query_asks_again(tmp_path: Path) -> None:
    """Approving one query must not authorize another.

    ``approval_covers`` fails closed on a changed query, so the honest report is
    "this still needs a decision" rather than a degraded answer the user did not
    ask for.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    approval = approve_ok(settings, challenge_for(settings, "redis"))
    status, payload = search_of(
        settings, {"query": "deploy", "mode": "semantic", "approval": approval}
    )

    assert status == 409
    assert payload["error"]["code"] == "consent_required"


# --------------------------------------------------------------------------- #
# What an approval actually changes
# --------------------------------------------------------------------------- #


def test_declining_never_assembles_the_semantic_retriever(
    tmp_path: Path, semantic_calls: list[str]
) -> None:
    """The acceptance criterion, stated as a mechanism rather than an outcome.

    A declined approval is passed along rather than omitted, so this exercises the
    path where an approval object *is* present and must still not authorize
    anything.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    approval = approve_ok(settings, challenge_for(settings), approved=False)
    payload = search_ok(settings, {"query": "redis", "mode": "hybrid", "approval": approval})

    assert semantic_calls == [], "拒绝之后仍然建了语义检索器 —— 查询会离开本机"
    assert payload["warnings"] == [SEMANTIC_NOT_APPROVED]
    assert payload["results"], "降级结果必须仍然返回，否则用户看到的是空白页"


def test_no_approval_never_assembles_the_semantic_retriever(
    tmp_path: Path, semantic_calls: list[str]
) -> None:
    """The first request, which is the ordinary one: nobody has been asked yet."""
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    payload = search_ok(settings, {"query": "redis", "mode": "hybrid"})

    assert semantic_calls == []
    assert payload["semantic"]["consent"] is not None
    assert payload["warnings"] == [SEMANTIC_NOT_APPROVED]


def test_approving_gets_past_the_approval_gate(
    tmp_path: Path, semantic_calls: list[str]
) -> None:
    """With an approval the code goes on to build the retriever — and fails on the
    provider, because this fixture has no key.

    The failure is the evidence. Without the approval the notice is the approval
    message and the retriever is never built; with it the notice becomes a backend
    failure, which can only happen if retrieval was actually attempted.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    approval = approve_ok(settings, challenge_for(settings))
    payload = search_ok(settings, {"query": "redis", "mode": "hybrid", "approval": approval})

    assert semantic_calls == ["built"], "批准之后没有建语义检索器 —— 批准被忽略了"
    assert payload["warnings"], "语义腿失败了却没有降级提示"
    assert payload["warnings"] != [SEMANTIC_NOT_APPROVED]
    assert "Semantic backend failed" in payload["warnings"][0]


def test_a_mid_query_failure_is_still_a_server_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason ``EmbeddingError`` was not mapped to 400 wholesale.

    With an approval the request gets all the way into retrieval, where a provider
    that fails mid-query raises ``EmbeddingError``. That is a real outage, not a bad
    request, so it has to stay a 500 — and this test is what stops a future change
    from "simplifying" the status table by giving the whole family a 4xx.

    The retriever is stubbed rather than left to fail on the missing key: without a
    key the failure is ``MissingCredentialError``, which is already a 400 for a
    different and correct reason (B-6), and would not exercise the unmapped class
    this test is about.
    """
    from obsai.errors import EmbeddingError
    from obsai.retrieval import VectorRetriever

    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    def explode(*args: object, **kwargs: object) -> None:
        raise EmbeddingError("provider went away mid-query")

    monkeypatch.setattr(VectorRetriever, "search", explode)

    approval = approve_ok(settings, challenge_for(settings))
    status, payload = search_of(
        settings, {"query": "redis", "mode": "semantic", "approval": approval}
    )

    assert status == 500
    assert payload["error"]["code"] == "embedding"


def test_a_missing_key_mid_query_is_reported_as_such(tmp_path: Path) -> None:
    """The un-stubbed version of the same path, and evidence the approval worked.

    Without the stub the provider fails on the absent key, and the code that comes
    back is ``missing_credential`` — which can only be reached from inside
    retrieval. Reaching it is proof that the approval got past the gate rather than
    being quietly ignored.
    """
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    approval = approve_ok(settings, challenge_for(settings))
    status, payload = search_of(
        settings, {"query": "redis", "mode": "semantic", "approval": approval}
    )

    assert status == 400
    assert payload["error"]["code"] == "missing_credential"


# --------------------------------------------------------------------------- #
# The paths B-5 established are unchanged
# --------------------------------------------------------------------------- #


def test_hybrid_without_an_approval_still_answers(tmp_path: Path) -> None:
    """B-5's behaviour, re-asserted here because B-8 changed the route's shape."""
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    payload = search_ok(settings, {"query": "redis", "mode": "hybrid"})

    assert payload["results"]
    assert payload["warnings"] == [SEMANTIC_NOT_APPROVED]


def test_keyword_mode_still_never_probes(tmp_path: Path) -> None:
    """No challenge, no failure, no probe — the field is ``null``, not empty."""
    vault, database_path = build_vault(tmp_path)
    settings = settings_for(vault, database_path)
    seed_generation(database_path, settings)

    payload = search_ok(settings, {"query": "redis", "mode": "keyword"})

    assert payload["semantic"] is None
    assert payload["warnings"] == []
