"""Search orchestration and the remote-embedding consent protocol.

The consent protocol exists because a semantic query leaves the machine. The
original CLI asked for approval with a bare ``typer.confirm`` inside the retriever
factory, which meant the approval was only ever bound to "the query I happened to
be looking at". Here a challenge is an explicit, serializable value
(:class:`~obsai.application.dto.RemoteConsent`) bound to the query hash and the
embedding generation, with an expiry. An approval that does not match all three
is rejected instead of silently applied.

Two callers are expected:

- the CLI probes, shows tokens and cost, asks, then searches;
- an HTTP client probes, shows the same numbers in a dialog, then searches with
  the approval it received.

Neither one is privileged; both go through :func:`probe_semantic`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from obsai.application.dto import (
    ConsentApproval,
    RemoteConsent,
    SearchOutcome,
    SearchRequest,
    SemanticFailure,
    SemanticProbe,
)
from obsai.application.embedding import build_embedding_pipeline
from obsai.config.models import Settings
from obsai.errors import ConfigError, ConsentExpiredError
from obsai.retrieval import FTSRetriever, HybridRetriever, VectorRetriever
from obsai.storage.database import Database

SEMANTIC_INDEX_MISSING = "Semantic index missing; run 'obsai index embeddings'"
SEMANTIC_NOT_APPROVED = "Semantic query was not approved"
SEMANTIC_BACKEND_UNAVAILABLE = "Semantic backend unavailable ({kind}: {detail})"

#: How long an approval prompt stays usable. Long enough to read the cost, short
#: enough that a forgotten dialog cannot authorize a query much later.
CONSENT_TTL = timedelta(minutes=15)


def hash_query(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def probe_semantic(
    query: str,
    *,
    database: Database,
    settings: Settings,
    strict: bool = False,
    now: datetime | None = None,
) -> SemanticProbe:
    """Decide whether a remote query would be sent, without sending one.

    Reads the index to check the embedding generation exists and that the query
    fits the configured budget; performs no network I/O.

    The two ways this can come back negative are reported separately: ``reason``
    is the sentence a caller shows, ``failure`` is the value it branches on. The
    HTTP adapter uses the latter to pick a status, because "no vectors in the
    index" and "the provider is unreachable" are not the same failure and do not
    have the same fix.

    ``strict`` re-raises instead of degrading, which is what ``--mode semantic``
    and ``--strict-semantic`` require.
    """
    failure: SemanticFailure = "index_missing"
    reason = SEMANTIC_INDEX_MISSING
    try:
        store, pipeline = build_embedding_pipeline(database, settings)
        if store.has_generation(pipeline.generation) and query.strip():
            tokens = pipeline.count_tokens(query)
            pipeline.check_budget(tokens, 1)
            moment = now or datetime.now(timezone.utc)
            return SemanticProbe(
                consent=RemoteConsent(
                    consent_id=hashlib.sha256(
                        f"{query}\x00{pipeline.generation.id}".encode("utf-8")
                    ).hexdigest()[:32],
                    query_hash=hash_query(query),
                    generation_id=pipeline.generation.id,
                    expires_at=moment + CONSENT_TTL,
                    query_tokens=tokens,
                    estimated_cost_usd=tokens * pipeline.price / 1_000_000,
                    request_count=1,
                ),
                reason="",
            )
    except Exception as exc:
        if strict:
            raise
        failure = "backend_unavailable"
        reason = SEMANTIC_BACKEND_UNAVAILABLE.format(kind=type(exc).__name__, detail=exc)
    return SemanticProbe(consent=None, reason=reason, failure=failure)


def approve_consent(
    consent: RemoteConsent,
    *,
    approved: bool = True,
    now: datetime | None = None,
) -> ConsentApproval:
    """Record a decision about one challenge.

    A declined challenge still produces an approval object, so callers have a
    single value to pass along and the reason for degrading stays explicit.

    Approving an expired challenge raises instead. That is the one case a caller
    cannot recover from by looking at the approval: the challenge it would have
    covered is gone, and a fresh search is the only way to get a new one.
    """
    moment = now or datetime.now(timezone.utc)
    if approved and consent.expires_at <= moment:
        raise ConsentExpiredError("Remote embedding consent expired; run the search again")
    return ConsentApproval(
        consent_id=consent.consent_id,
        query_hash=consent.query_hash,
        generation_id=consent.generation_id,
        nonce=hashlib.sha256(f"{consent.consent_id}\x00{moment.isoformat()}".encode("utf-8")).hexdigest()[:32],
        approved_at=moment,
        approved=approved,
    )


def approval_covers(
    approval: ConsentApproval,
    consent: RemoteConsent,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether an approval still authorizes exactly this challenge.

    Fails closed on any mismatch: a different query, a different embedding
    generation, a challenge that has since expired, or an approval older than the
    challenge TTL all invalidate it.

    The age check is on ``approval.approved_at`` rather than on
    ``consent.expires_at``, and that distinction is the whole point. A caller that
    re-probes on every request — which is what the HTTP route does — hands in a
    challenge minted moments ago, so its own deadline is always in the future and
    checking it would authorize a decision made last week. What has to expire is
    the *decision*, and a decision carries its own timestamp.
    """
    if not approval.approved:
        return False
    moment = now or datetime.now(timezone.utc)
    if consent.expires_at <= moment:
        return False
    if approval.approved_at + CONSENT_TTL <= moment:
        return False
    return (
        approval.query_hash == consent.query_hash
        and approval.generation_id == consent.generation_id
        and approval.consent_id == consent.consent_id
    )


def build_semantic_retriever(database: Database, settings: Settings) -> VectorRetriever:
    """Assemble the approved semantic retriever.

    ``approved=True`` is not a formality: :class:`VectorRetriever` refuses to
    embed without it, so reaching this function already implies consent was given.
    """
    store, pipeline = build_embedding_pipeline(database, settings)
    return VectorRetriever(store, pipeline, approved=True)


def search(
    request: SearchRequest,
    *,
    database: Database,
    settings: Settings,
    probe: SemanticProbe | None = None,
    approval: ConsentApproval | None = None,
) -> SearchOutcome:
    """Run one search across the requested mode.

    ``probe`` lets a caller that already computed the challenge pass it back in,
    which avoids rebuilding the embedding pipeline. When omitted it is computed
    here, so an HTTP caller that never showed a dialog still gets correct
    degradation behaviour rather than an accidental remote query.
    """
    if request.mode == "keyword":
        results = FTSRetriever(database).search(
            request.query, limit=request.limit, filters=request.filters
        )
        return SearchOutcome(results=tuple(results))

    semantic: VectorRetriever | None = None
    reason = SEMANTIC_NOT_APPROVED
    if probe is None:
        probe = probe_semantic(
            request.query,
            database=database,
            settings=settings,
            strict=request.semantic_is_mandatory,
        )
    if probe.consent is not None:
        if approval is not None and approval_covers(approval, probe.consent):
            semantic = build_semantic_retriever(database, settings)
    else:
        reason = probe.reason

    if request.mode == "semantic":
        if semantic is None:
            raise ConfigError(reason)
        results = semantic.search(request.query, limit=request.limit, filters=request.filters)
        return SearchOutcome(results=tuple(results))

    hybrid = HybridRetriever(
        FTSRetriever(database),
        semantic,
        on_semantic_failure="strict" if request.strict_semantic else "warn",
        semantic_unavailable_reason=reason,
    )
    if request.mode == "hybrid":
        outcome = hybrid.search_with_status(
            request.query, limit=request.limit, filters=request.filters
        )
        return SearchOutcome(results=tuple(outcome.results), warnings=tuple(outcome.warnings))

    from obsai.graph import GraphRepository, GraphService
    from obsai.retrieval import GraphRetriever
    from obsai.storage import IndexRepository

    repository = IndexRepository(database)
    graph = GraphService(repository, GraphRepository(database))
    retriever = GraphRetriever(hybrid, graph, repository)
    results = retriever.search(request.query, limit=request.limit, filters=request.filters)
    return SearchOutcome(results=tuple(results), warnings=tuple(retriever.last_warnings))
