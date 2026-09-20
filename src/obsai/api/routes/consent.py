"""The other half of the remote-embedding consent protocol.

``/search`` probes but never approves, so a query that would leave the machine
comes back as a challenge instead of leaving. This route is where a caller answers
that challenge: it turns a :class:`~obsai.application.dto.RemoteConsent` into a
:class:`~obsai.application.dto.ConsentApproval` carrying the nonce that ``/search``
then expects alongside the query.

Three things about its shape are deliberate:

* **It is a POST although it changes nothing.** An approval is a decision, and the
  result is bound to the moment it was made — ``approved_at`` and the nonce both
  derive from the timestamp. A GET would claim the value is a property of the
  challenge, which it is not.
* **It needs neither the Vault nor the index.** Everything it touches is in the
  challenge it was handed, so the route has no ``get_settings`` and no
  ``index_handle`` dependency. That is also why it is not a way around the
  protocol: a challenge can only be produced by ``probe_semantic`` reading the
  index, and this route merely records a decision about one that already exists.
* **Declining is expressible here.** A declined challenge yields an approval with
  ``approved=False``, which ``search`` then fails closed on. The search page does
  not use that path — it simply does not ask again — but omitting it would leave
  the route unable to express the protocol it implements.
"""

from __future__ import annotations

from fastapi import APIRouter

from obsai.application.dto import ConsentApproval, RemoteConsent
from obsai.application.search import approve_consent

router = APIRouter(tags=["consent"])


class ConsentRequest(RemoteConsent):
    """A challenge plus the decision about it.

    Extending ``RemoteConsent`` rather than wrapping it keeps the JSON flat, the
    same way ``SearchResponse`` extends ``SearchOutcome``: the caller echoes back
    the challenge it received and adds one field.
    """

    approved: bool = True


@router.post("/consent", response_model=ConsentApproval)
def post_consent(request: ConsentRequest) -> ConsentApproval:
    """Record a decision about one challenge.

    Approving an expired challenge raises ``ConsentExpiredError`` (410). The
    challenge is what carried the expiry, so a caller that held on to a dialog for
    longer than the TTL is told to search again rather than being handed an
    approval that ``search`` would silently ignore.
    """
    return approve_consent(request, approved=request.approved)
