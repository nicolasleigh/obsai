"""A-3 acceptance: an approval authorises one exact remote query, once.

The three invalidation scenarios the plan calls out — expiry, a changed query and
a changed embedding generation — are the reason consent is a value here instead of
a ``typer.confirm`` boolean. Each is checked against :func:`approval_covers`, the
single place that decides whether a query may leave the machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from obsai.application.dto import ConsentApproval, RemoteConsent, SemanticProbe
from obsai.application.search import (
    CONSENT_TTL,
    SEMANTIC_INDEX_MISSING,
    SEMANTIC_NOT_APPROVED,
    approval_covers,
    approve_consent,
    hash_query,
    probe_semantic,
)
from obsai.config.models import Settings
from obsai.errors import ConsentExpiredError

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
GENERATION = "generation-a"
OTHER_GENERATION = "generation-b"


def consent(
    *,
    query: str = "redis cache strategy",
    generation: str = GENERATION,
    issued_at: datetime = NOW,
    consent_id: str = "consent-1",
) -> RemoteConsent:
    return RemoteConsent(
        consent_id=consent_id,
        query_hash=hash_query(query),
        generation_id=generation,
        expires_at=issued_at + CONSENT_TTL,
        query_tokens=12,
        estimated_cost_usd=Decimal("0.00000024"),
    )


def approval_for(challenge: RemoteConsent, *, at: datetime = NOW, approved: bool = True) -> ConsentApproval:
    return approve_consent(challenge, approved=approved, now=at)


def test_a_fresh_approval_covers_its_challenge() -> None:
    challenge = consent()
    assert approval_covers(approval_for(challenge), challenge, now=NOW)


def test_expiry_invalidates_the_approval() -> None:
    challenge = consent()
    approval = approval_for(challenge)
    just_inside = challenge.expires_at - timedelta(seconds=1)
    just_outside = challenge.expires_at + timedelta(seconds=1)

    assert approval_covers(approval, challenge, now=just_inside)
    assert not approval_covers(approval, challenge, now=just_outside)


def test_expiry_is_refused_at_approval_time() -> None:
    """``ConsentExpiredError`` rather than ``ConfigError``, though the words match.

    The class is what decides the HTTP status — a 410 rather than the 400 a
    ``ConfigError`` would get — and the frontend picks its Chinese hint off the code,
    so this is the difference between "search again" and "go edit ``config.toml``".
    """
    challenge = consent()
    with pytest.raises(ConsentExpiredError, match="expired"):
        approve_consent(challenge, now=challenge.expires_at + timedelta(seconds=1))


def test_declining_an_expired_challenge_is_still_allowed() -> None:
    """A no is always safe, so it must not need a valid challenge."""
    challenge = consent()
    late = challenge.expires_at + timedelta(days=1)
    approval = approve_consent(challenge, approved=False, now=late)
    assert approval.approved is False
    assert not approval_covers(approval, challenge, now=late)


def test_a_changed_query_invalidates_the_approval() -> None:
    approved = approval_for(consent(query="redis cache strategy"))
    assert not approval_covers(approved, consent(query="postgres tuning"), now=NOW)


def test_a_changed_generation_invalidates_the_approval() -> None:
    approved = approval_for(consent(generation=GENERATION))
    assert not approval_covers(approved, consent(generation=OTHER_GENERATION), now=NOW)


def test_a_different_challenge_for_the_same_query_invalidates_the_approval() -> None:
    """Same text, but a re-issued challenge: the approval is not transferable."""
    approved = approval_for(consent(consent_id="first"))
    assert not approval_covers(approved, consent(consent_id="second"), now=NOW)


def test_a_declined_approval_never_covers_anything() -> None:
    challenge = consent()
    assert not approval_covers(approval_for(challenge, approved=False), challenge, now=NOW)


def test_query_hash_is_stable_and_query_specific() -> None:
    assert hash_query("redis") == hash_query("redis")
    assert hash_query("redis") != hash_query("Redis")
    assert len(hash_query("redis")) == 64


def test_approval_nonce_differs_between_decisions() -> None:
    """A nonce is per decision, so one approval cannot be replayed as another."""
    challenge = consent()
    first = approve_consent(challenge, now=NOW)
    second = approve_consent(challenge, now=NOW + timedelta(seconds=1))
    assert first.nonce != second.nonce
    assert first.consent_id == second.consent_id


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #


def test_probe_with_no_generation_returns_index_missing_reason(tmp_path) -> None:
    """The store has no vectors, so a remote query would be pointless."""
    from obsai.indexing import IncrementalIndexer
    from obsai.storage import Database, IndexRepository

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n", encoding="utf-8")
    db = tmp_path / "index.db"
    with Database(db) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)

    with Database(db) as database:
        probe = probe_semantic(
            "redis",
            database=database,
            settings=Settings(vault={"path": vault}, index={"database": db}),
        )

    assert probe.consent is None
    assert probe.reason == SEMANTIC_INDEX_MISSING


def test_probe_with_a_generation_returns_consent_and_empty_reason(tmp_path) -> None:
    """Exactly one field carries the answer — see :class:`SemanticProbe`.

    An earlier implementation left the initial ``reason`` value alongside a
    non-null ``consent``, which contradicted the docstring and produced stale
    degradation notices on the wire.
    """
    from pathlib import Path

    from obsai.application.embedding import build_embedding_pipeline
    from obsai.config.models import Settings
    from obsai.indexing import IncrementalIndexer
    from obsai.storage import Database, IndexRepository

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n", encoding="utf-8")
    db = tmp_path / "index.db"
    with Database(db) as database:
        IncrementalIndexer(IndexRepository(database)).update(vault)
        store, pipeline = build_embedding_pipeline(database, Settings(vault={"path": vault}, index={"database": db}))
        store.ensure_generation(pipeline.generation)

    with Database(db) as database:
        probe = probe_semantic(
            "redis",
            database=database,
            settings=Settings(vault={"path": vault}, index={"database": db}),
        )

    assert probe.consent is not None, "generation 已注册，应该给出挑战"
    assert probe.reason == "", "consent 非空时 reason 必须为空，否则是矛盾数据"
    assert bool(probe.consent) != bool(probe.reason), "恰好一个字段承载答案"
