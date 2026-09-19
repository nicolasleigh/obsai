"""Application-layer data transfer objects.

These models are the contract between the CLI, the HTTP adapter and the domain
services. They exist so that neither adapter has to reach into a service object
or re-derive presentation text, and so that every value crossing the boundary is
JSON-serializable.

Two rules hold for everything in this module:

1. **No I/O types.** No ``Path``, no ``Console``, no ``Database``. Paths travel as
   POSIX strings relative to the Vault; anything that needs a filesystem handle
   is a service's business, not the wire format's.
2. **No rendering.** Diffs and citations are structured (``DiffLine``,
   ``CitationView.location``). Adapters decide how to draw them.

The ``*View`` suffix marks models that describe something already prepared by a
service — an approval prompt, a job, a recovery. They are snapshots, never live
handles.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from obsai.retrieval.models import SearchFilters, SearchResult
from obsai.vault.models import BlockKind

SearchMode = Literal["hybrid", "keyword", "semantic", "graph"]

# ``interrupted`` is terminal, but it is not ``cancelled``: nothing asked the job
# to stop and it never got the chance to roll back. A process that starts and
# finds a job still in a non-terminal state writes it, so that "we do not know how
# this ended" is never reported as "it succeeded".
JobStatus = Literal[
    "queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled", "interrupted"
]

DiffStyle = Literal["added", "removed", "hunk", "notice"]


class _Frozen(BaseModel):
    """Shared configuration: immutable and strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Search and answering
# --------------------------------------------------------------------------- #


class SearchRequest(_Frozen):
    """One search invocation, already parsed out of argv or a request body."""

    query: str
    mode: SearchMode = "hybrid"
    limit: int = Field(default=10, ge=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    strict_semantic: bool = False

    @property
    def requires_semantic(self) -> bool:
        return self.mode != "keyword"

    @property
    def semantic_is_mandatory(self) -> bool:
        """``--mode semantic`` cannot degrade; ``--strict-semantic`` only forbids it."""
        return self.mode == "semantic" or self.strict_semantic


class SearchOutcome(_Frozen):
    """Search results plus the degradation notices the caller must surface."""

    results: tuple[SearchResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


class CitationView(_Frozen):
    """A resolved evidence reference, pre-composed into a citable location."""

    citation_id: str
    note_id: str
    chunk_id: str
    path: str
    title: str
    heading_path: tuple[str, ...] = ()
    block_id: str | None = None
    location: str
    truncated: bool = False


class AskRequest(_Frozen):
    query: str


class AskOutcome(_Frozen):
    """One grounded answer with its citations and degradation notices."""

    text: str
    citations: tuple[CitationView, ...] = ()
    warnings: tuple[str, ...] = ()
    abstained: bool = False


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


class NoteSegment(_Frozen):
    """One run inside a block: text, or a WikiLink with its target already resolved.

    A tag rather than two model classes so the wire shape stays flat — the
    frontend's contract test compares a flat key set, and a nested discriminator
    would make "which keys does this object have" depend on ``kind``.

    For a ``link`` run, ``target_note_id`` is the *index's* answer, not the
    frontend's guess: ``None`` means the target does not exist in the Vault, and
    the UI must render the run as inert text. That is the B-7 acceptance criterion
    ("WikiLink 只跳转服务端校验过的 Vault 内目标") as a single nullable field.
    """

    kind: Literal["text", "link"]
    text: str
    target_note_id: str | None = None
    target_path: str | None = None
    target_heading: str | None = None
    target_block_id: str | None = None
    is_embed: bool = False


class NoteBlockView(_Frozen):
    """One block of a note, with its links already in place.

    ``segments`` is the whole of the block's content: there is deliberately no
    parallel ``content`` string, because two renderings of the same text are two
    things that can disagree. A caller that wants the plain text joins the ``text``
    of every segment.
    """

    kind: BlockKind
    #: Headings only. ``Block`` does not carry the level — ``ParsedNote.headings``
    #: does — so it is joined in here rather than left for the renderer to guess
    #: from the number of ``#`` characters.
    level: int | None = None
    segments: tuple[NoteSegment, ...] = ()
    block_id: str | None = None
    language: str | None = None
    line: int = 0


class NoteView(_Frozen):
    """One note as a reader sees it.

    ``raw_content`` is deliberately absent. Nothing downstream ever receives the
    note's Markdown source, so no renderer can be handed a string that might be
    interpreted as markup; the note arrives already reduced to text runs and
    resolved links. See :mod:`obsai.application.notes`.
    """

    note_id: str
    path: str
    title: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    blocks: tuple[NoteBlockView, ...] = ()
    #: ``target_path`` of every WikiLink that points outside the Vault, in document
    #: order and deduplicated. Carried so the page can say *why* some links are not
    #: clickable instead of leaving the user to wonder.
    unresolved_links: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Remote consent
# --------------------------------------------------------------------------- #


class RemoteConsent(_Frozen):
    """The challenge shown before any query leaves the machine.

    Bound to the exact query and embedding generation so that approving one
    prompt cannot silently authorize a different one: see
    :func:`obsai.application.search.approve_consent`.

    ``consent_id`` is derived from those two things and nothing else, which makes
    it *stable*: probing the same query twice yields the same id. That is what lets
    a caller probe, show a dialog, and search in a later request — the server
    re-probes on every request, and an id that changed per probe could never be
    matched by the approval that came back from the dialog. Uniqueness comes from
    the pair, which is precisely the set of things an approval is about; the
    deadline lives in ``expires_at``, not in the id.
    """

    consent_id: str
    query_hash: str
    generation_id: str
    expires_at: datetime
    query_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal
    request_count: int = Field(default=1, ge=0)

    def matches(self, other: "RemoteConsent") -> bool:
        return (
            self.query_hash == other.query_hash
            and self.generation_id == other.generation_id
        )


class ConsentApproval(_Frozen):
    """A user's decision about one :class:`RemoteConsent`."""

    consent_id: str
    query_hash: str
    generation_id: str
    nonce: str
    approved_at: datetime
    approved: bool = True


#: Why a semantic query could not even be prepared. ``None`` means it could — see
#: :class:`SemanticProbe`. Two values rather than one because they have different
#: next steps: a missing generation is fixed by ``obsai index embeddings``, a
#: backend failure by looking at the provider.
SemanticFailure = Literal["index_missing", "backend_unavailable"]


class SemanticProbe(_Frozen):
    """Whether semantic retrieval can run for a query, and why not if it cannot.

    Exactly one of the two fields carries the answer:

    - ``consent`` is set when a remote query *would* be sent, so the caller must
      obtain approval before calling :func:`obsai.application.search.search`.
    - ``reason`` is the degradation notice to surface when it would not. It is
      also the message ``--mode semantic`` fails with.

    ``failure`` names the second case in a form a caller can branch on. ``reason``
    is prose, and prose reads much the same whether the index has no vectors or the
    provider is down — a caller that has to compare sentences to tell them apart
    will get it wrong the first time the wording changes. The invariant is
    therefore two-sided: ``consent`` is set exactly when ``reason`` is empty and
    ``failure`` is ``None``.
    """

    consent: RemoteConsent | None = None
    reason: str = ""
    failure: SemanticFailure | None = None


# --------------------------------------------------------------------------- #
# Embedding plans and approval
# --------------------------------------------------------------------------- #


class EmbeddingPlanView(_Frozen):
    """An approval prompt for generating remote embeddings."""

    plan_id: str
    generation_id: str
    chunks_requiring_embeddings: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    request_count: int = Field(ge=0)
    estimated_cost_usd: Decimal
    expires_at: datetime
    nonce: str


class EmbeddingApproveRequest(_Frozen):
    """The caller's confirmation to proceed with remote embeddings."""

    nonce: str


# --------------------------------------------------------------------------- #
# Structured diffs
# --------------------------------------------------------------------------- #


class DiffLine(_Frozen):
    """One rendered diff line, with its styling already decided.

    ``style`` is a semantic tag, not a colour: the CLI maps ``added`` to green,
    the web adapter maps it to a CSS class. Deciding it here keeps both adapters
    from re-parsing the ``+``/``-``/``@@`` prefixes.

    ``highlight`` mirrors the ``highlight`` keyword of Rich's ``Console.print``.
    It is carried because Rich's default highlighter bolds the count in
    "Affected backlinks (2)"; a renderer that ignores it would produce visibly
    different output than the terminal.
    """

    text: str
    style: DiffStyle | None = None
    highlight: bool = True


class PlannedChange(_Frozen):
    """One file change inside a plan."""

    operation: str
    path: str
    destination: str | None = None
    affected_backlinks: tuple[str, ...] = ()


class DiffSummaryItem(_Frozen):
    """File-level diff statistics for a proposed change."""

    path: str
    operation: str
    destination: str | None = None
    added_lines: int = 0
    removed_lines: int = 0


class ChangePlanView(_Frozen):
    """An approval prompt for a single-note change or a multi-file transaction.

    ``plan_id`` + ``revision`` + ``nonce`` are the three values an approver must
    echo back. ``expires_at`` bounds how long the prompt stays valid; a stale or
    mismatched approval is rejected rather than silently applied.
    """

    plan_id: str
    revision: int = Field(ge=1)
    expires_at: datetime
    nonce: str
    changes: tuple[PlannedChange, ...] = ()
    affected_paths: tuple[str, ...] = ()
    diff: tuple[DiffLine, ...] = ()
    diff_summary: tuple[DiffSummaryItem, ...] = ()
    ambiguous_backlinks: tuple[str, ...] = ()
    batch: bool = False

    @property
    def affected_backlink_count(self) -> int:
        return len({p for change in self.changes for p in change.affected_backlinks})

    @property
    def only_change(self) -> PlannedChange | None:
        return self.changes[0] if len(self.changes) == 1 else None


class ChangeOutcome(_Frozen):
    """The result of applying (or declining) a :class:`ChangePlanView`."""

    plan_id: str
    committed: bool
    cancelled: bool = False
    index_dirty: bool = False
    index_error: str | None = None
    transaction_id: str | None = None


class ChangeOperationRequest(_Frozen):
    """One operation submitted to generate a write plan."""

    kind: Literal[
        "create", "replace", "append", "frontmatter", "move", "trash", "rewrite_backlinks"
    ]
    path: str
    destination: str | None = None
    old: str | None = None
    new: str | None = None
    updates: dict[str, Any] = Field(default_factory=dict)


class CreateChangePlanRequest(_Frozen):
    """Request payload to generate a write plan."""

    operations: tuple[ChangeOperationRequest, ...] = ()


class ApproveChangePlanRequest(_Frozen):
    """Echoed credentials required to execute an approved change plan."""

    revision: int = Field(ge=1)
    nonce: str
    approved: bool = True


# --------------------------------------------------------------------------- #
# Organizer
# --------------------------------------------------------------------------- #


class OrganizeProposalView(_Frozen):
    """A reviewable Inbox proposal.

    Named with the ``View`` suffix rather than the shorter ``OrganizeProposal``
    so it cannot be confused with :class:`obsai.organizer.models.OrganizerProposal`,
    the domain dataclass it is derived from.
    """

    number: int = Field(ge=1)
    path: str
    destination: str | None = None
    title: str
    add_tags: tuple[str, ...] = ()
    add_links: tuple[str, ...] = ()
    affected_backlinks: tuple[str, ...] = ()
    confidence: float
    reason: str
    issue: str | None = None
    actionable: bool = False
    selected_by_default: bool = False


class OrganizePreview(_Frozen):
    """The full proposal listing for one Inbox scan."""

    proposals: tuple[OrganizeProposalView, ...] = ()
    inbox: str
    default_numbers: tuple[int, ...] = ()

    @property
    def conflict_count(self) -> int:
        return sum(1 for item in self.proposals if item.issue)


class OrganizePlanRequest(_Frozen):
    """Numbers of proposals to turn into a change plan."""

    numbers: tuple[int, ...] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


class JobView(_Frozen):
    """A snapshot of one background job.

    ``error_code`` is the exception type name, kept apart from ``error`` so that a
    renderer can map it to a message the way the HTTP error envelope maps its
    codes, instead of pattern-matching on prose.
    """

    job_id: str
    kind: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "interrupted")


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


class JournalOriginal(_Frozen):
    """One entry of a journal's ``originals`` list: where the bytes came from."""

    path: str
    snapshot: str | None = None


class JournalView(_Frozen):
    """One transaction journal as reported by ``transaction status``."""

    transaction_id: str
    status: str
    originals: tuple[JournalOriginal, ...] = ()

    @property
    def unfinished(self) -> bool:
        return self.status in ("prepared", "applying", "rolling_back", "recovery_required")

    @property
    def needs_index_update(self) -> bool:
        return self.status in ("committed", "index_dirty")


class RecoveryView(_Frozen):
    """The recovery prompt for an unfinished transaction."""

    transaction_id: str
    status: str
    journal_directory: str
    originals: tuple[JournalOriginal, ...] = ()
    diff: tuple[DiffLine, ...] = ()
    recoverable: bool = False


class RecoverTransactionRequest(_Frozen):
    approved: bool = True


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


class DirtyNoteView(_Frozen):
    """One note whose derived rows are stale after a Vault write.

    ``reason`` is the domain's own wording, not a display string: the adapters
    translate it if they want to.
    """

    path: str
    reason: str
    marked_at: str


class IndexStatusView(_Frozen):
    """The derived index as an outside observer sees it.

    ``usable`` is the field that matters. The index is a rebuildable artefact, so
    "missing", "unreadable" and "empty" are all ordinary states a caller must be
    able to render — none of them is an error for the *status* endpoint, which is
    why ``error`` carries the reason instead of an exception being raised.
    """

    path: str
    exists: bool = False
    usable: bool = False
    error: str | None = None
    note_count: int = 0
    chunk_count: int = 0
    vector_count: int = 0
    generation: str | None = None
    semantic_ready: bool = False
    dirty_notes: tuple[DirtyNoteView, ...] = ()


class StatusView(_Frozen):
    """Everything an overview screen can show without a remote call.

    Deliberately total: no field requires the network, and every degraded state
    (no Vault configured, no index built, recovery pending, another writer active)
    is representable as data rather than as a failure.
    """

    version: str
    vault_path: str | None = None
    vault_ready: bool = False
    index: IndexStatusView
    unfinished_transactions: tuple[JournalView, ...] = ()
    index_dirty_transactions: tuple[JournalView, ...] = ()
    recovery_required: bool = False
    locked: bool = False


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class AgentTimelineItem(_Frozen):
    """One completed tool call or execution event in an agent workflow."""

    tool: str
    summary: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentApprovalView(_Frozen):
    """A human-in-the-loop approval checkpoint for pending mutations."""

    kind: str
    preview: str
    plan_ref: str | None = None


class AgentRunView(_Frozen):
    """The full execution state of an agent workflow run."""

    run_id: str
    status: str
    query: str
    step_count: int = 0
    retrieval_step_count: int = 0
    selected_note_ids: tuple[str, ...] = ()
    retrieved_chunk_ids: tuple[str, ...] = ()
    timeline: tuple[AgentTimelineItem, ...] = ()
    final_answer: str | None = None
    stop_reason: str | None = None
    pending_approval: AgentApprovalView | None = None


class AgentRunRequest(_Frozen):
    """Request payload to initiate or resume an agent run."""

    query: str
    thread_id: str | None = None


class AgentResumeRequest(_Frozen):
    """Echoed user approval decision to resume an interrupted write."""

    approved: bool = True

