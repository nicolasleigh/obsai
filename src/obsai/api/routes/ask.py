"""Grounded answers over the index, with the degradation notices kept visible.

``/ask`` is ``/search`` plus a model: the same hybrid retriever, the same consent
protocol, the same degradation vocabulary. Three things differ, and each is why
this is not just a second verb on the search route:

* **It always probes.** ``/search`` probes only when the requested mode needs
  semantics, because keyword search provably touches no embedding backend. Asking
  is hybrid by definition, so there is no mode that can skip the probe.
* **It can fail for a reason that is not the user's fault.** No vectors, no
  network, no API key. The first two are ordinary ``ConfigError``/``LLMError``
  values the adapter maps; "no API key" gets its own code
  (:class:`~obsai.errors.MissingCredentialError`) because its fix is an
  environment variable rather than a file.
* **Abstention is a success.** A 200 with ``abstained: true`` is the correct
  response to a question the index cannot support. The request was well-formed
  and was answered truthfully with "I don't know" — it is not an error, and the
  page must not render it as one.

The probe is returned for the same reason as in ``/search``: ``warnings`` cannot
tell "this index has no vectors" apart from "nobody approved this query", and the
two need different words.

One consequence of probing unconditionally is worth stating, because it is
visible in the payload and easy to misread: **a blank question probes too**, and
``probe_semantic`` reports the setup problem for it, not the emptiness. That is
not the reason the answer abstained — the empty question is. The rule the page
follows is therefore "when ``abstained`` is true the text is the message and the
probe is diagnostic only", which ``tests/integration/test_api_ask.py`` pins.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import get_settings, index_handle
from obsai.application.answering import ask
from obsai.application.dto import AskOutcome, AskRequest, SemanticProbe
from obsai.application.index import IndexHandle
from obsai.application.search import probe_semantic
from obsai.config.models import Settings

router = APIRouter(tags=["ask"])


class AskResponse(AskOutcome):
    """The answer, its citations, and the probe that produced them.

    Extending the application DTO rather than wrapping it keeps the JSON flat,
    the same way ``SearchResponse`` and ``StatusResponse`` do.
    """

    #: Always set for a real question, since asking is hybrid. Optional so the
    #: field has one shape and a future keyword-only ask needs no schema change.
    semantic: SemanticProbe | None = None


@router.post("/ask", response_model=AskResponse)
def post_ask(
    request: AskRequest,
    settings: Settings = Depends(get_settings),
    index: IndexHandle = Depends(index_handle),
) -> AskResponse:
    """Answer one question from bounded retrieved evidence.

    POST for the same reasons as ``/search``: a question is prose that would end
    up in server logs as a query string, and nothing here mutates anything.

    A missing index is a 400 carrying the CLI's own wording; an index that exists
    but cannot be read re-raises its original failure, so a corrupt schema stays a
    server-side error instead of being flattened into a 400.
    """
    database = index.require()

    # Probed, never approved. The route has no dialog, so an unapproved semantic
    # leg degrades to keyword retrieval rather than leaving the machine. Closing
    # that loop is B-8; until it exists the degraded answer is the honest one.
    probe = probe_semantic(request.query, database=database, settings=settings)

    outcome = ask(request, database=database, settings=settings, probe=probe)
    return AskResponse(**outcome.model_dump(), semantic=probe)
