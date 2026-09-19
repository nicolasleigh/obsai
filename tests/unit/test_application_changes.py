"""A-6 acceptance: structured diffs stay faithful, and approvals cannot drift.

Two properties are checked here.

**Fidelity.** ``ChangePlanView.diff`` is the only diff a non-terminal adapter ever
sees. If it cannot be rendered back into exactly what ``SafeWriteService.preview``
prints, the CLI and the UI would disagree about what the user approved. The test
compares the two renderings byte for byte, styles included.

**Non-transferability.** A plan is applied only when the caller echoes back the
right ``plan_id``, ``revision`` and ``nonce`` before ``expires_at``. Each of those
is a separate rejection path below.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from obsai.application import changes
from obsai.application.changes import approve, discard, plan_single, plan_transaction, require_batch
from obsai.cli.app import _render_diff
from obsai.errors import ConflictError, TransactionError
from obsai.safe_write import SafeWriteService
from obsai.transactions import TransactionOperation, TransactionService


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("# Alpha\n\nAlpha body.\n", encoding="utf-8")
    (root / "R1.md").write_text("# R1\n\nSee [[A]].\n", encoding="utf-8")
    (root / "R2.md").write_text("# R2\n\nAlso [[A]].\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def clean_store():
    changes.reset()
    yield
    changes.reset()


def capture(render, *, terminal: bool) -> str:
    output = io.StringIO()
    console = Console(file=output, width=100, force_terminal=terminal, color_system="truecolor")
    render(console)
    return output.getvalue()


def both_renderings(service, change):
    """The domain preview and the wire-format diff, rendered the same way."""
    view = plan_single(service, change)
    plain = (
        capture(lambda console: service.preview(change, console), terminal=False),
        capture(lambda console: _render_diff(console, view.diff), terminal=False),
    )
    styled = (
        capture(lambda console: service.preview(change, console), terminal=True),
        capture(lambda console: _render_diff(console, view.diff), terminal=True),
    )
    return view, plain, styled


def test_update_diff_renders_identically(vault: Path) -> None:
    service = SafeWriteService(vault)
    _, plain, styled = both_renderings(service, service.update_note("A.md", "Alpha body", "Edited body"))
    assert plain[0] == plain[1]
    assert styled[0] == styled[1]


def test_create_diff_renders_identically(vault: Path) -> None:
    service = SafeWriteService(vault)
    _, plain, styled = both_renderings(service, service.create_note("New.md", "# New\n"))
    assert plain[0] == plain[1]
    assert styled[0] == styled[1]


def test_trash_diff_renders_identically(vault: Path) -> None:
    service = SafeWriteService(vault)
    _, plain, styled = both_renderings(service, service.trash_note("A.md"))
    assert plain[0] == plain[1]
    assert styled[0] == styled[1]


def test_move_diff_carries_the_highlight_of_the_backlink_notice(vault: Path) -> None:
    """Rich bolds the count in "Affected backlinks (2)"; the wire format must say so.

    Without the carried ``highlight`` flag the structured rendering would be
    visually identical in plain text and subtly different in a terminal — exactly
    the kind of drift this test exists to catch.
    """
    service = SafeWriteService(vault)
    view, plain, styled = both_renderings(service, service.move_note("A.md", "Moved/A.md"))
    assert plain[0] == plain[1]
    assert styled[0] == styled[1]

    notice = next(line for line in view.diff if line.style == "notice")
    assert notice.text == "Affected backlinks (2); they will not be rewritten:"
    assert notice.highlight is True
    body = [line for line in view.diff if line.style in ("added", "removed", "hunk")]
    assert body and all(line.highlight is False for line in body)


def test_transaction_diff_renders_identically(vault: Path) -> None:
    service = TransactionService(vault)
    plan = service.plan([TransactionOperation.append("A.md", "\nMore.\n")])
    view = plan_transaction(service, plan)
    expected = capture(lambda console: service.preview(plan, console), terminal=True)
    actual = capture(lambda console: _render_diff(console, view.diff), terminal=True)
    assert expected == actual
    assert view.batch is True


def test_diff_lines_use_semantic_tags_not_colours(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.update_note("A.md", "Alpha body", "Edited body"))
    assert {line.style for line in view.diff} <= {"added", "removed", "hunk", "notice", None}
    assert "added" in {line.style for line in view.diff}


def test_plan_describes_what_it_touches(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.move_note("A.md", "Moved/A.md"))
    assert view.affected_paths == ("A.md",)
    assert view.only_change.operation == "move"
    assert view.only_change.destination == "Moved/A.md"
    assert view.revision == 1


def test_approval_requires_the_matching_nonce(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    with pytest.raises(ConflictError, match="does not match"):
        approve(view, nonce="not-the-nonce")
    assert not (vault / "New.md").exists()


def test_approval_without_a_nonce_is_refused(vault: Path) -> None:
    """A plan id alone is not an approval; the nonce has no default."""
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    with pytest.raises(ConflictError, match="nonce"):
        approve(view, approved=True)
    assert not (vault / "New.md").exists()


def test_approval_cannot_be_replayed(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    assert approve(view, nonce=view.nonce).committed
    assert (vault / "New.md").exists()
    with pytest.raises(ConflictError, match="unknown or already resolved"):
        approve(view, nonce=view.nonce)


def test_an_expired_plan_is_refused(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "_store", changes.PlanStore(ttl=timedelta(seconds=-1)))
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    with pytest.raises(ConflictError, match="expired"):
        approve(view, nonce=view.nonce)
    assert not (vault / "New.md").exists()


def test_a_declined_plan_touches_nothing(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    outcome = approve(view, approved=False, nonce=view.nonce)
    assert outcome.cancelled and not outcome.committed
    assert not (vault / "New.md").exists()
    with pytest.raises(ConflictError):
        approve(view, nonce=view.nonce)


def test_a_discarded_plan_cannot_be_approved(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    discard(view)
    with pytest.raises(ConflictError):
        approve(view, nonce=view.nonce)
    assert not (vault / "New.md").exists()


def test_require_batch_rejects_a_single_note_plan(vault: Path) -> None:
    service = SafeWriteService(vault)
    view = plan_single(service, service.create_note("New.md", "# New\n"))
    with pytest.raises(TransactionError):
        require_batch(view)


def test_plan_ids_and_nonces_are_unique(vault: Path) -> None:
    service = SafeWriteService(vault)
    first = plan_single(service, service.create_note("One.md", "# One\n"))
    second = plan_single(service, service.create_note("Two.md", "# Two\n"))
    assert first.plan_id != second.plan_id
    assert first.nonce != second.nonce


def test_approving_a_transaction_reports_index_state(vault: Path) -> None:
    service = TransactionService(vault)
    view = plan_transaction(service, service.plan([TransactionOperation.create("New.md", "# New\n")]))
    outcome = approve(view, nonce=view.nonce)
    assert outcome.committed
    assert outcome.plan_id == view.plan_id
    assert (vault / "New.md").exists()
