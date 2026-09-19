"""A-9 acceptance: a job can be cancelled at a safe boundary, and stops cleanly.

Two properties, both about *where* cancellation lands.

**Between batches, not inside one.** ``CancellationToken`` implements the same
``check()`` interface ``ShutdownController`` does, and is installed into the
ContextVar that ``check_shutdown()`` already reads. The embedding pipeline, the
agent workflow and the indexer therefore became cancellable without being
edited — ``test_the_shared_shutdown_boundary_sees_the_job_token`` proves the
wiring, and ``test_cancellation_stops_between_batches`` proves the granularity.

**Without breaking an open file transaction.** A cancel that arrives mid-commit
must leave the Vault at either the old bytes or the new ones. The transaction
rolls back first and the job reports ``cancelled``, not ``failed`` — a user's
decision is not an error.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from obsai.application.jobs import (
    CancellationToken,
    JobCancelled,
    JobNotFoundError,
    JobRunner,
)
from obsai.shutdown import check_shutdown, current_controller, is_process_stop
from obsai.transactions import TransactionOperation, TransactionService


@pytest.fixture()
def runner():
    with JobRunner() as instance:
        yield instance


def wait_for(runner: JobRunner, job_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = runner.get(job_id)
        if view.terminal:
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished: {runner.get(job_id)}")


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("# A\n\nA body.\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\nB body.\n", encoding="utf-8")
    (root / "C.md").write_text("# C\n\nC body.\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# The shared cancellation boundary
# --------------------------------------------------------------------------- #


def test_the_shared_shutdown_boundary_sees_the_job_token(runner: JobRunner) -> None:
    """Domain code calls ``check_shutdown()``; it must find the job's token."""
    seen: list[object] = []

    def work(context):
        seen.append(current_controller())
        check_shutdown()  # must not raise while the job is live
        return {"ok": True}

    view = wait_for(runner, runner.submit("probe", work).job_id)
    assert view.status == "succeeded"
    assert view.detail["ok"] is True
    assert isinstance(seen[0], CancellationToken)


def test_the_token_still_forwards_to_the_process_controller() -> None:
    """Ctrl-C must keep working inside a job, so the ambient check is chained."""
    from obsai.shutdown import install_controller

    class Stop:
        def __init__(self) -> None:
            self.calls = 0

        def check(self) -> None:
            self.calls += 1
            raise KeyboardInterrupt

    ambient = Stop()
    token = CancellationToken(previous=ambient)
    with install_controller(ambient):
        token._previous = ambient
        with pytest.raises(KeyboardInterrupt):
            token.check()
    assert ambient.calls == 1


def test_cancellation_stops_between_batches(runner: JobRunner) -> None:
    """The boundary is a batch edge: the in-flight batch finishes, the next never starts."""
    batches: list[int] = []
    started = threading.Event()
    job_id: list[str] = []

    def work(context):
        job_id.append(context.job_id)
        for index in range(5):
            context.progress(f"batch {index}")
            batches.append(index)
            started.set()
            time.sleep(0.05)
        return {"batches": len(batches)}

    view = runner.submit("batches", work)
    assert started.wait(timeout=5)
    assert runner.cancel(view.job_id, "user cancelled")
    finished = wait_for(runner, view.job_id)

    assert finished.status == "cancelled"
    assert finished.message == "user cancelled"
    assert len(batches) < 5, "cancellation should have stopped the loop"
    assert finished.error is None


def test_a_job_that_ignores_the_boundary_still_reports_its_result(runner: JobRunner) -> None:
    """Cancellation is cooperative: a job with no checkpoint runs to completion."""
    view = wait_for(runner, runner.submit("no-checkpoint", lambda context: {"done": True}).job_id)
    assert view.status == "succeeded"
    assert view.detail["done"] is True


# --------------------------------------------------------------------------- #
# Job bookkeeping
# --------------------------------------------------------------------------- #


def test_submit_reports_queued_before_the_job_starts() -> None:
    with JobRunner() as runner:
        gate = threading.Event()
        view = runner.submit("blocked", lambda context: (gate.wait(timeout=5), {"ok": True})[1])
        assert view.status in ("queued", "running")
        gate.set()
        assert wait_for(runner, view.job_id).status == "succeeded"


def test_a_failing_job_captures_the_error_and_traceback(runner: JobRunner) -> None:
    def work(context):
        raise ValueError("nope")

    view = wait_for(runner, runner.submit("boom", work).job_id)
    assert view.status == "failed"
    assert view.error == "ValueError: nope"
    assert "ValueError: nope" in view.detail["traceback"]


def test_a_cancelled_job_is_not_reported_as_failed(runner: JobRunner) -> None:
    """``JobCancelled`` must not be swallowed by ``except Exception`` in domain code."""

    def work(context):
        try:
            raise JobCancelled("stop")
        except Exception:  # pragma: no cover - the point is that this does not match
            return {"swallowed": True}

    view = wait_for(runner, runner.submit("cancel", work).job_id)
    assert view.status == "cancelled"
    assert view.detail.get("swallowed") is not True


def test_unknown_jobs_are_rejected(runner: JobRunner) -> None:
    with pytest.raises(JobNotFoundError):
        runner.get("nope")
    assert runner.cancel("nope") is False
    assert runner.resolve_approval("nope", approved=True) is False


def test_list_is_ordered_by_creation(runner: JobRunner) -> None:
    first = runner.submit("one", lambda context: None).job_id
    second = runner.submit("two", lambda context: None).job_id
    wait_for(runner, second)
    assert [view.job_id for view in runner.list()][:2] == [first, second]


# --------------------------------------------------------------------------- #
# Approval round trip
# --------------------------------------------------------------------------- #


def test_a_job_can_await_and_receive_approval(runner: JobRunner) -> None:
    requested = threading.Event()
    decisions: list[bool] = []

    def work(context):
        requested.set()
        decisions.append(context.await_approval({"kind": "write_approval", "summary": "edit A.md"}))
        return {"decided": decisions[-1]}

    view = runner.submit("approval", work)
    assert requested.wait(timeout=5)
    deadline = time.monotonic() + 5
    while runner.get(view.job_id).status != "awaiting_approval" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.get(view.job_id).status == "awaiting_approval"
    assert runner.resolve_approval(view.job_id, approved=True) is True

    finished = wait_for(runner, view.job_id)
    assert finished.status == "succeeded"
    assert finished.detail["decided"] is True


def test_cancelling_while_awaiting_approval_wakes_the_job(runner: JobRunner) -> None:
    requested = threading.Event()

    def work(context):
        requested.set()
        context.await_approval({"kind": "write_approval"})
        return {"reached": True}

    view = runner.submit("approval", work)
    assert requested.wait(timeout=5)
    deadline = time.monotonic() + 5
    while runner.get(view.job_id).status != "awaiting_approval" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert runner.cancel(view.job_id, "changed my mind") is True
    finished = wait_for(runner, view.job_id)
    assert finished.status == "cancelled"
    assert finished.detail.get("reached") is not True


def test_close_cancels_live_jobs_and_is_idempotent() -> None:
    """Shutdown cancels at the next checkpoint; a job without one still finishes."""
    runner = JobRunner()
    ticks: list[int] = []

    def work(context):
        for index in range(100):
            context.progress(f"tick {index}")
            ticks.append(index)
            time.sleep(0.02)
        return {"ticks": len(ticks)}

    view = runner.submit("looping", work)
    deadline = time.monotonic() + 5
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.01)

    runner.close(wait=True)
    assert runner.get(view.job_id).status == "cancelled"
    assert len(ticks) < 100
    runner.close(wait=True)  # idempotent
    with pytest.raises(RuntimeError):
        runner.submit("after-close", lambda context: None)


# --------------------------------------------------------------------------- #
# Cancellation must not damage a file transaction
# --------------------------------------------------------------------------- #


def test_a_cancelled_transaction_rolls_back_and_reports_cancelled(
    runner: JobRunner, vault: Path
) -> None:
    """The acceptance property: cancel mid-commit, Vault intact, job not "failed"."""
    originals = {path.name: path.read_bytes() for path in sorted(vault.glob("*.md"))}
    service = TransactionService(vault, indexer=lambda root: None)
    applied: list[str] = []
    apply_change = service.safe.apply

    def cancelling_apply(change, *, approved: bool) -> bool:
        result = apply_change(change, approved=approved)
        applied.append(change.file.path)
        if len(applied) == 1:
            # Cancel from inside the commit; the loop's next checkpoint raises.
            controller = current_controller()
            assert isinstance(controller, CancellationToken)
            controller.cancel("user cancelled")
        return result

    def work(context):
        service.safe.apply = cancelling_apply  # type: ignore[method-assign]
        try:
            plan = service.plan([
                TransactionOperation.append("A.md", "\nA extra.\n"),
                TransactionOperation.append("B.md", "\nB extra.\n"),
                TransactionOperation.append("C.md", "\nC extra.\n"),
            ])
            service.execute(plan, approved=True)
        finally:
            service.safe.apply = apply_change  # type: ignore[method-assign]
        return {"reached": True}

    view = wait_for(runner, runner.submit("transaction", work).job_id)

    assert view.status == "cancelled", view.error
    assert view.error is None
    assert applied == ["A.md"], "the in-flight change finishes, the next must not start"
    assert {path.name: path.read_bytes() for path in sorted(vault.glob("*.md"))} == originals
    assert not list((vault / ".obsai-transactions").glob("*/journal.json")), "journal must be cleaned up"


def test_a_completed_transaction_job_reports_success(runner: JobRunner, vault: Path) -> None:
    """The control case: without a cancel, the same job commits everything."""
    service = TransactionService(vault, indexer=lambda root: None)

    def work(context):
        plan = service.plan([
            TransactionOperation.append("A.md", "\nA extra.\n"),
            TransactionOperation.append("B.md", "\nB extra.\n"),
        ])
        result = service.execute(plan, approved=True)
        return {"committed": result.committed}

    view = wait_for(runner, runner.submit("transaction", work).job_id)
    assert view.status == "succeeded"
    assert view.detail["committed"] is True
    assert "A extra." in (vault / "A.md").read_text()
    assert "B extra." in (vault / "B.md").read_text()


def test_is_process_stop_separates_stops_from_failures() -> None:
    from obsai.shutdown import ShutdownRequested

    assert is_process_stop(ShutdownRequested(2))
    assert is_process_stop(JobCancelled("stop"))
    assert is_process_stop(KeyboardInterrupt())
    assert is_process_stop(SystemExit(1))
    assert not is_process_stop(ValueError("boom"))
