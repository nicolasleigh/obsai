"""A-1 acceptance: the wire contract is JSON-clean and closed.

Every model crossing the adapter boundary must survive ``model_dump(mode="json")``
and must not carry a ``Path``, ``Console``, ``Database`` or any other handle. The
test enumerates the module rather than listing models by hand, so a new DTO that
forgets the rule fails here instead of at the first HTTP request.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from obsai.application import dto

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

BANNED_TYPE_NAMES = {
    "Path",
    "PurePath",
    "PosixPath",
    "WindowsPath",
    "Console",
    "Database",
    "Connection",
    "SafeWriteService",
    "TransactionService",
    "InboxOrganizer",
}

EXAMPLES: dict[str, dict] = {
    "SearchRequest": {"query": "redis"},
    "SearchOutcome": {},
    "CitationView": {
        "citation_id": "1",
        "note_id": "n1",
        "chunk_id": "c1",
        "path": "A.md",
        "title": "A",
        "location": "A.md",
    },
    "AskRequest": {"query": "what is redis?"},
    "AskOutcome": {"text": "Redis is a cache."},
    "RemoteConsent": {
        "consent_id": "c",
        "query_hash": "h",
        "generation_id": "g",
        "expires_at": NOW,
        "query_tokens": 5,
        "estimated_cost_usd": Decimal("0.0000001"),
    },
    "ConsentApproval": {
        "consent_id": "c",
        "query_hash": "h",
        "generation_id": "g",
        "nonce": "n",
        "approved_at": NOW,
    },
    "EmbeddingPlanView": {
        "plan_id": "p",
        "generation_id": "g",
        "chunks_requiring_embeddings": 10,
        "cache_hits": 2,
        "estimated_tokens": 1000,
        "request_count": 1,
        "estimated_cost_usd": Decimal("0.0001"),
        "expires_at": NOW,
        "nonce": "n",
    },
    "EmbeddingApproveRequest": {
        "nonce": "n",
    },
    "SemanticProbe": {},
    "NoteSegment": {"kind": "text", "text": "hello"},
    "NoteBlockView": {"kind": "paragraph"},
    "NoteView": {"note_id": "n1", "path": "A.md", "title": "A"},
    "DiffLine": {"text": "+one"},
    "PlannedChange": {"operation": "update", "path": "A.md"},
    "DiffSummaryItem": {"path": "A.md", "operation": "update", "added_lines": 1, "removed_lines": 0},
    "ChangePlanView": {
        "plan_id": "p",
        "revision": 1,
        "expires_at": NOW,
        "nonce": "n",
    },
    "ChangeOutcome": {"plan_id": "p", "committed": True},
    "ChangeOperationRequest": {"kind": "create", "path": "A.md", "new": "Hello"},
    "CreateChangePlanRequest": {"operations": ()},
    "ApproveChangePlanRequest": {"revision": 1, "nonce": "n", "approved": True},
    "OrganizeProposalView": {
        "number": 1,
        "path": "Inbox/a.md",
        "title": "A",
        "confidence": 0.8,
        "reason": "title match",
    },
    "OrganizePreview": {"inbox": "Inbox"},
    "OrganizePlanRequest": {"numbers": (1, 2)},
    "JobView": {"job_id": "j", "kind": "index-update", "status": "queued", "created_at": NOW},
    "JournalOriginal": {"path": "A.md"},
    "JournalView": {"transaction_id": "t", "status": "committed"},
    "RecoveryView": {"transaction_id": "t", "status": "applying", "journal_directory": "/tmp/t"},
    "RecoverTransactionRequest": {"approved": True},
    "DirtyNoteView": {"path": "A.md", "reason": "note changed", "marked_at": "2026-09-14T12:00:00Z"},
    "IndexStatusView": {"path": "/tmp/index.db"},
    "StatusView": {"version": "0.1.0", "index": {"path": "/tmp/index.db"}},
    "AgentTimelineItem": {
        "tool": "search_notes",
        "summary": "Found 1 note",
        "args": {"query": "test"},
    },
    "AgentApprovalView": {
        "kind": "write_approval",
        "preview": "--- a\n+++ b",
        "plan_ref": "ref-1",
    },
    "AgentRunView": {
        "run_id": "r1",
        "status": "completed",
        "query": "test query",
        "step_count": 1,
        "retrieval_step_count": 1,
        "selected_note_ids": ("n1",),
        "retrieved_chunk_ids": ("c1",),
        "timeline": (
            {
                "tool": "search_notes",
                "summary": "Found 1 note",
                "args": {"query": "test"},
            },
        ),
        "final_answer": "Answer",
    },
    "AgentRunRequest": {"query": "search something"},
    "AgentResumeRequest": {"approved": True},
}



def _models() -> list[type[BaseModel]]:
    """Every public model defined in the module; private bases are excluded."""
    return [
        obj
        for name, obj in inspect.getmembers(dto, inspect.isclass)
        if issubclass(obj, BaseModel)
        and obj is not BaseModel
        and not name.startswith("_")
        and obj.__module__ == dto.__name__
    ]


def test_every_dto_has_an_example() -> None:
    """A new model must be added to EXAMPLES, or the contract test skips it."""
    assert {model.__name__ for model in _models()} == set(EXAMPLES)


@pytest.mark.parametrize("model", _models(), ids=lambda model: model.__name__)
def test_dto_declares_no_io_types(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        rendered = str(field.annotation)
        for banned in BANNED_TYPE_NAMES:
            assert banned not in rendered, f"{model.__name__}.{name} leaks {banned}"


@pytest.mark.parametrize("model", _models(), ids=lambda model: model.__name__)
def test_dto_serialises_to_json(model: type[BaseModel]) -> None:
    instance = model(**EXAMPLES[model.__name__])
    payload = instance.model_dump(mode="json")
    assert json.loads(json.dumps(payload)) == payload
    assert model.model_validate(payload).model_dump() == instance.model_dump()


@pytest.mark.parametrize("model", _models(), ids=lambda model: model.__name__)
def test_dto_is_frozen(model: type[BaseModel]) -> None:
    instance = model(**EXAMPLES[model.__name__])
    first = next(iter(model.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, first, getattr(instance, first))


@pytest.mark.parametrize("model", _models(), ids=lambda model: model.__name__)
def test_dto_rejects_unknown_fields(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError):
        model(**EXAMPLES[model.__name__], unexpected_field=1)


def test_search_request_mode_drives_the_degradation_policy() -> None:
    assert not dto.SearchRequest(query="q", mode="keyword").requires_semantic
    assert dto.SearchRequest(query="q", mode="hybrid").requires_semantic
    assert dto.SearchRequest(query="q", mode="hybrid").semantic_is_mandatory is False
    assert dto.SearchRequest(query="q", mode="hybrid", strict_semantic=True).semantic_is_mandatory
    assert dto.SearchRequest(query="q", mode="semantic").semantic_is_mandatory


def test_search_outcome_reports_degradation() -> None:
    assert not dto.SearchOutcome().degraded
    assert dto.SearchOutcome(warnings=("Warning: keyword only",)).degraded


def test_change_plan_counts_unique_backlink_notes() -> None:
    view = dto.ChangePlanView(
        plan_id="p",
        revision=2,
        expires_at=NOW,
        nonce="n",
        changes=(
            dto.PlannedChange(operation="move", path="A.md", affected_backlinks=("R1.md", "R2.md")),
            dto.PlannedChange(operation="move", path="B.md", affected_backlinks=("R2.md",)),
        ),
    )
    assert view.affected_backlink_count == 2
    assert view.only_change is None


def test_job_view_terminal_states() -> None:
    def job(status: str) -> dto.JobView:
        return dto.JobView(job_id="j", kind="k", status=status, created_at=NOW)

    assert [job(status).terminal for status in ("queued", "running", "awaiting_approval")] == [
        False,
        False,
        False,
    ]
    assert all(job(status).terminal for status in ("succeeded", "failed", "cancelled"))


def test_journal_view_classifies_recovery_state() -> None:
    def journal(status: str) -> dto.JournalView:
        return dto.JournalView(transaction_id="t", status=status)

    assert journal("applying").unfinished
    assert not journal("committed").unfinished
    assert journal("index_dirty").needs_index_update
    assert not journal("prepared").needs_index_update


def test_organize_preview_counts_conflicts() -> None:
    preview = dto.OrganizePreview(
        inbox="Inbox",
        proposals=(
            dto.OrganizeProposalView(
                number=1, path="Inbox/a.md", title="A", confidence=0.9, reason="r"
            ),
            dto.OrganizeProposalView(
                number=2, path="Inbox/b.md", title="B", confidence=0.1, reason="r", issue="conflict"
            ),
        ),
    )
    assert preview.conflict_count == 1


def test_diff_line_defaults_match_rich_behaviour() -> None:
    """Rich highlights by default; only the diff body opts out."""
    assert dto.DiffLine(text="x").highlight is True
    assert dto.DiffLine(text="x", style="notice").highlight is True


def test_a_note_segment_defaults_to_plain_text() -> None:
    """The ``text`` branch is the common one, so it needs no link fields at all.

    ``target_note_id`` staying ``None`` is what marks a run as unresolved; a
    segment built without it must therefore be inert rather than accidentally
    linking somewhere.
    """
    segment = dto.NoteSegment(kind="text", text="hello")

    assert segment.target_note_id is None
    assert not segment.is_embed


def test_a_note_block_has_no_parallel_content_string() -> None:
    """``segments`` is the whole of the block: two renderings can disagree.

    Pinned because adding ``content`` back is the tempting way to save a caller a
    join, and it would immediately raise the question of which one a renderer is
    supposed to trust.
    """
    assert "content" not in dto.NoteBlockView.model_fields
    assert set(dto.NoteBlockView.model_fields) == {
        "kind",
        "level",
        "segments",
        "block_id",
        "language",
        "line",
    }


def test_the_note_view_carries_no_source_markdown() -> None:
    """B-7's safety rests on this field not existing. See application/notes.py."""
    assert "raw_content" not in dto.NoteView.model_fields
    assert "plain_text" not in dto.NoteView.model_fields


def test_status_view_is_total_for_a_degraded_vault() -> None:
    """B-1: every degraded state is representable as data, never as an exception."""
    view = dto.StatusView(version="0.1.0", index=dto.IndexStatusView(path="/tmp/index.db"))
    assert view.vault_path is None
    assert not view.vault_ready
    assert not view.index.exists
    assert not view.index.usable
    assert view.index.error is None
    assert not view.recovery_required
    assert not view.locked


def test_status_view_reports_recovery_and_staleness() -> None:
    view = dto.StatusView(
        version="0.1.0",
        vault_path="/vault",
        vault_ready=True,
        index=dto.IndexStatusView(
            path="/tmp/index.db",
            exists=True,
            usable=True,
            note_count=9,
            chunk_count=80,
            vector_count=0,
            semantic_ready=False,
            dirty_notes=(dto.DirtyNoteView(path="A.md", reason="r", marked_at="t"),),
        ),
        unfinished_transactions=(dto.JournalView(transaction_id="t", status="applying"),),
        index_dirty_transactions=(dto.JournalView(transaction_id="u", status="committed"),),
        recovery_required=True,
    )
    assert view.recovery_required
    assert view.unfinished_transactions[0].unfinished
    assert view.index_dirty_transactions[0].needs_index_update
    assert view.index.dirty_notes[0].path == "A.md"


def test_the_semantic_failure_reason_is_a_closed_set() -> None:
    """``Literal`` rather than ``str``, so a typo fails at construction.

    The two values have different fixes — one is ``obsai index embeddings``, the
    other is looking at the provider — and the HTTP adapter picks a status off
    them, with the last branch acting as ``else``. A third value spelled correctly
    at the call site would silently land in that ``else``; a misspelling would not
    even get that far.
    """
    with pytest.raises(ValidationError):
        dto.SemanticProbe(reason="x", failure="backend_unavailble")


def test_a_semantic_probe_defaults_to_having_no_failure() -> None:
    """``failure`` is only meaningful when ``consent`` is unset.

    Both fields are legitimately optional, so neither can enforce the invariant on
    its own — the two-sided contract is stated in the docstring and pinned over
    HTTP in ``test_api_consent.test_the_consent_and_failure_fields_are_exclusive``.
    What this asserts is the default, which is what every construction site that
    has a challenge relies on.
    """
    probe = dto.SemanticProbe(
        consent=dto.RemoteConsent(
            consent_id="c",
            query_hash="q",
            generation_id="g",
            expires_at=NOW,
            query_tokens=3,
            estimated_cost_usd=Decimal("0.000001"),
        )
    )
    assert probe.consent is not None
    assert probe.failure is None
    assert probe.reason == ""
