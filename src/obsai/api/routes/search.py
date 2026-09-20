"""Search over the derived index, with the degradation notices kept visible.

``/status`` answers "what state is this Vault in"; ``/search`` answers "what does it
say about this". Both are read-only, and both are *total* in the same sense: a query
that could not use the semantic backend still returns keyword results plus the reason
it degraded, rather than an error the caller has to interpret.

Four things are worth stating because they are easy to get wrong:

* **Keyword mode touches no embedding backend at all.** That is what makes the search
  page usable on a machine with no API key, no vectors and no network — the plan's
  B-5 acceptance criterion. The route does not special-case it; it simply never probes.
* **Probing is not approving.** ``probe_semantic`` decides whether a remote query
  *would* be sent and prices it, without sending anything. This route probes, so the
  UI can be told what is missing, but never approves: an approval arrives with the
  request, and ``POST /consent`` is where a caller obtains one. A query with no
  approval degrades to keyword results instead of leaving the machine.
* **The probe is returned alongside the results** because the two reasons a semantic
  query did not run need different words: "this index has no vectors" is a setup
  problem, "nobody approved this query" is a question the user has not been asked yet.
  The ``warnings`` list cannot tell them apart — both arrive as prose.
* **Refusing to degrade belongs to this layer.** See :func:`_refuse_to_degrade` for
  why the branch lives here and not in ``search``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import get_settings, index_handle
from obsai.application.dto import (
    ConsentApproval,
    SearchOutcome,
    SearchRequest,
    SemanticProbe,
)
from obsai.application.index import IndexHandle
from obsai.application.search import approval_covers, probe_semantic, search
from obsai.config.models import Settings
from obsai.errors import (
    ConsentRequiredError,
    SemanticIndexMissingError,
    SemanticUnavailableError,
)

router = APIRouter(tags=["search"])


class SearchRequestBody(SearchRequest):
    """A search plus the approval for the remote query it may need.

    ``SearchRequest`` stays free of consent state because it describes *what* is
    being asked, not what the user has agreed to — the CLI hands an approval to
    ``search`` as a separate argument for exactly the same reason. Extending it here
    keeps the JSON flat, the way ``SearchResponse`` extends ``SearchOutcome``.
    """

    #: Omitted by a caller that has not been asked yet, which is the ordinary first
    #: request. Present when a previous response carried a challenge and the caller
    #: answered it through ``POST /consent``.
    approval: ConsentApproval | None = None


class SearchResponse(SearchOutcome):
    """Results, degradation notices, and the probe that produced them.

    Extending the application DTO rather than wrapping it keeps the JSON flat, the
    same way ``StatusResponse`` extends ``StatusView``.
    """

    #: ``None`` for keyword mode (nothing was probed). A remote probe sets one of
    #: ``consent`` / ``reason``; a local Ollama probe may intentionally leave both
    #: empty — see :class:`SemanticProbe`.
    semantic: SemanticProbe | None = None


def _refuse_to_degrade(request: SearchRequestBody, probe: SemanticProbe) -> None:
    """Fail with the right status when a query that cannot degrade would have to.

    ``mode=semantic`` and ``strict_semantic`` both promise the caller that keyword
    results are not an acceptable answer. Two things can make that promise
    unkeepable, and they are not the same failure:

    * **The challenge was never answered.** The caller needs a decision, not a fix,
      so the challenge rides along in ``details`` and the UI can show what the
      decision costs. An approval that no longer covers this challenge — the query
      or the embedding generation changed since it was given — lands here too,
      because as far as this request is concerned it has not been approved either.
    * **Semantic retrieval is unavailable.** Either the index holds no vectors (a
      setup problem: 400) or the backend is down (a downstream failure: 503).

    This runs in the adapter rather than inside ``search`` for one reason: ``search``
    has to keep raising the classes the CLI snapshot pins, and those two conditions
    reach the command line through its own ``ConfigError`` and ``EmbeddingError``.
    Deciding here costs one branch and leaves the command line's wording untouched.
    """
    if probe.consent is not None:
        if request.approval is None or not approval_covers(request.approval, probe.consent):
            raise ConsentRequiredError(
                "Semantic retrieval was not approved for this query",
                details={"consent": probe.consent.model_dump(mode="json")},
            )
        return
    if probe.failure == "index_missing":
        raise SemanticIndexMissingError(probe.reason)
    raise SemanticUnavailableError(probe.reason)


@router.post("/search", response_model=SearchResponse)
def post_search(
    request: SearchRequestBody,
    settings: Settings = Depends(get_settings),
    index: IndexHandle = Depends(index_handle),
) -> SearchResponse:
    """Run one search across the requested mode.

    POST rather than GET because the filters are a nested object (tags, folder,
    frontmatter key/values, date bounds) that would turn into a dozen query
    parameters, and because a query string ends up in shell history and server logs
    in a way a body does not. Nothing here mutates anything.
    """
    # A missing index is a 400 carrying the same message the CLI prints; an index
    # that exists but cannot be read re-raises its original failure, so a corrupt
    # schema stays a server-side error instead of being flattened into a 400.
    database = index.require()

    probe: SemanticProbe | None = None
    if request.requires_semantic:
        # Probed without ``strict`` on purpose. Strictness is about what to do once
        # semantic retrieval turns out to be unavailable, and the ways that can
        # happen need different statuses — which is a decision made below, after the
        # probe has said which one it is. Letting ``probe_semantic`` re-raise would
        # collapse them all into whatever the provider happened to throw.
        #
        # Probed once and passed in: ``search`` would otherwise rebuild the whole
        # embedding pipeline a second time to reach the same conclusion.
        probe = probe_semantic(request.query, database=database, settings=settings)
        if request.semantic_is_mandatory:
            _refuse_to_degrade(request, probe)

    outcome = search(
        request,
        database=database,
        settings=settings,
        probe=probe,
        approval=request.approval,
    )
    return SearchResponse(**outcome.model_dump(), semantic=probe)
