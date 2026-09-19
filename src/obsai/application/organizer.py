"""Inbox organization: read-only proposal, then an explicit selection.

``organize inbox`` used to interleave scanning, numbering, interactive selection
and application in one 89-line command body. Only the middle part is interaction;
the scan and the apply are service work. Splitting them lets a UI render the same
proposals as a table and post back a selection, while the CLI keeps its prompt
loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from obsai.application.changes import approve, plan_transaction
from obsai.application.dto import ChangeOutcome, ChangePlanView, OrganizePreview, OrganizeProposalView
from obsai.config.models import Settings
from obsai.organizer.models import OrganizerProposal
from obsai.organizer.service import InboxOrganizer
from obsai.retrieval import FTSRetriever
from obsai.storage import Database, IndexRepository


@dataclass(frozen=True)
class OrganizeScan:
    """One Inbox scan: the wire-ready preview plus the domain proposals behind it.

    The proposals are kept because :meth:`InboxOrganizer.plan` needs the original
    dataclasses — the view is lossy on purpose (it drops the ``RelatedLink``
    objects down to their wikilink text).
    """

    organizer: InboxOrganizer
    preview: OrganizePreview
    proposals: tuple[OrganizerProposal, ...]


def build_organizer(
    vault: Path, database_path: Path, database: Database, settings: Settings
) -> InboxOrganizer:
    """Assemble the Inbox organizer against an open index.

    ``database_path`` is passed alongside the connection because the organizer
    reopens the index for its own reindexing step.
    """
    return InboxOrganizer(
        vault,
        database_path,
        IndexRepository(database),
        FTSRetriever(database),
        inbox=settings.organize.inbox,
    )


def to_view(number: int, proposal: OrganizerProposal) -> OrganizeProposalView:
    """Convert one domain proposal into its wire form, keeping the 1-based number."""
    return OrganizeProposalView(
        number=number,
        path=proposal.path,
        destination=proposal.destination,
        title=proposal.title,
        add_tags=tuple(proposal.add_tags),
        add_links=tuple(link.wikilink for link in proposal.add_links),
        affected_backlinks=tuple(proposal.affected_backlinks),
        confidence=proposal.confidence,
        reason=proposal.reason,
        issue=proposal.issue,
        actionable=proposal.actionable,
        selected_by_default=proposal.selected_by_default,
    )


def propose(vault: Path, database_path: Path, database: Database, settings: Settings) -> OrganizeScan:
    """Scan the Inbox and return every proposal. Never writes."""
    organizer = build_organizer(vault, database_path, database, settings)
    proposals = tuple(organizer.propose())
    preview = OrganizePreview(
        proposals=tuple(to_view(index, item) for index, item in enumerate(proposals, start=1)),
        inbox=settings.organize.inbox,
        default_numbers=tuple(
            index for index, item in enumerate(proposals, start=1) if item.selected_by_default
        ),
    )
    return OrganizeScan(organizer=organizer, preview=preview, proposals=proposals)


def plan_selection(scan: OrganizeScan, numbers: list[int]) -> ChangePlanView:
    """Turn a selection of proposal numbers into one approvable transaction."""
    plan = scan.organizer.plan(scan.proposals, numbers)
    return plan_transaction(scan.organizer.transaction, plan)


def apply(
    view: ChangePlanView, *, approved: bool = True, nonce: str | None = None
) -> ChangeOutcome:
    """Apply a planned selection through the shared approval protocol.

    ``nonce`` must echo the plan's nonce; an approval without one is refused.
    """
    return approve(view, approved=approved, nonce=nonce)
