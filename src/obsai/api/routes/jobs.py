"""Job progress: live, after the fact, and across a reload.

Four ways to ask about the same records, and the difference between them is what the
caller is doing: a list for a page that has just loaded, one job for a page that kept
its id, the stream for a page that is watching, and a cancel for a page whose user
changed their mind.

Three decisions are worth stating.

* **The stream carries state, not a log.** Every connection opens with a snapshot of
  every known job and then sends each change. A client that reconnects is therefore
  correct by re-reading rather than by replaying, which is why the events carry no
  ``id``: ``Last-Event-ID`` exists to resume a *log*, and this journal is upserted
  state — one row per job — so there is nothing to replay. The snapshot is also what
  keeps "no jobs" distinguishable from "the stream has not said anything yet", the
  same distinction the overview screen had to make in B-4.
* **Disconnecting is not cancelling.** The stream only reads. It holds no job handle,
  calls nothing on the runner, and has no way to stop anything; cancelling is a
  separate ``POST`` that a human has to make. A tab that goes to sleep, a proxy that
  drops an idle connection and a user who hits reload must never stop work that was
  asked for.
* **The runner is not request-scoped.** A job is meant to outlive the request that
  started it and a stream is meant to keep reading after its handler has returned,
  so the runner comes from ``app.state`` through
  :func:`obsai.api.deps.get_job_runner` and only the lifespan closes it.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import anyio
from fastapi import APIRouter, Depends
from sse_starlette import EventSourceResponse, ServerSentEvent

from obsai.api.deps import get_job_runner
from obsai.application.dto import JobView
from obsai.application.jobs import JobRunner

router = APIRouter(tags=["jobs"])

#: How often the stream re-reads the runner. The journal throttles its own writes to
#: one per second, but a running job's live record is updated on every progress
#: report, so this bounds how stale the *view* can be rather than how often a job may
#: report. Half a second is under the point where a progress bar reads as stuck.
POLL_SECONDS = 0.5

#: Keep-alive interval. sse-starlette sends a comment line, which ``EventSource``
#: ignores, so this costs the client nothing and stops the Vite proxy from closing a
#: stream that is merely idle. Passed explicitly rather than left to the library
#: default: a proxy dropping a connection the user is watching is not a bug worth
#: discovering from a version bump.
PING_SECONDS = 15

#: The two event names. Both carry a list of ``JobView``, so the client has one
#: payload shape to parse and one function to merge; only the name says whether the
#: list is the whole world or just what changed.
SNAPSHOT_EVENT = "snapshot"
JOB_EVENT = "job"


def _event(name: str, views: list[JobView]) -> ServerSentEvent:
    """One event, serialised the way every other endpoint serialises a model.

    ``mode="json"`` so datetimes leave as ISO strings here instead of being left to
    the library's ``str()``, which happens to agree only because the values are
    UTC-aware.
    """
    payload = [view.model_dump(mode="json") for view in views]
    return ServerSentEvent(event=name, data=json.dumps(payload, ensure_ascii=False))


async def job_events(
    runner: JobRunner, *, poll_seconds: float = POLL_SECONDS
) -> AsyncIterator[ServerSentEvent]:
    """A snapshot of every job, then one event per change, until the client leaves.

    The read runs on a worker thread: ``JobStore`` is synchronous SQLite and this
    generator is not, so querying inline would stall every other connection on the
    server — including the requests the user is making while they watch.

    Every pass re-reads the whole list rather than waiting for a notification, so a
    change landing between two passes is reported by the next one instead of being
    lost. That is what makes the feed self-healing, and it is also why disconnecting
    cannot lose anything: there is no queue to miss.
    """
    sent: dict[str, JobView] = {}
    first = True
    while True:
        views = await anyio.to_thread.run_sync(runner.list)
        if first:
            first = False
            sent = {view.job_id: view for view in views}
            yield _event(SNAPSHOT_EVENT, views)
        else:
            changed = [view for view in views if sent.get(view.job_id) != view]
            for view in changed:
                sent[view.job_id] = view
            if changed:
                yield _event(JOB_EVENT, changed)
        await anyio.sleep(poll_seconds)


@router.get("/events")
async def get_events(runner: JobRunner = Depends(get_job_runner)) -> EventSourceResponse:
    """Stream job changes until the client goes away.

    An ``async def`` rather than a ``def``: nothing here blocks, and a synchronous
    signature would suggest the generator runs in the handler's thread when it does
    not. Cancelling the client's connection cancels this generator, which is the only
    cleanup there is — and it touches no job.
    """
    return EventSourceResponse(job_events(runner), ping=PING_SECONDS)


@router.get("/jobs", response_model=list[JobView])
def list_jobs(runner: JobRunner = Depends(get_job_runner)) -> list[JobView]:
    """Every job this index has run, oldest first — live records preferred."""
    return runner.list()


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, runner: JobRunner = Depends(get_job_runner)) -> JobView:
    """One job, live if this process is running it and from the journal if not.

    This is the call that makes a reload safe: the id a page was given before it was
    refreshed still resolves afterwards, in this process or the next one.
    """
    return runner.get(job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str, runner: JobRunner = Depends(get_job_runner)) -> JobView:
    """Ask a job to stop, and report where it actually is.

    The response is the job's state *after* the request rather than a boolean,
    because ``cancel`` is cooperative: "accepted" and "already over" are the same
    answer here, and the view is the one that says which happened. A job that had
    already finished comes back terminal and unchanged — which is the honest reply,
    and the reason this is not a 409.

    The first read is the 404 gate for an id nobody knows, live or journaled. The
    last one re-reads instead of returning that first view, because the job can
    finish between the two calls and the stale answer would say ``running``.
    """
    runner.get(job_id)
    runner.cancel(job_id)
    return runner.get(job_id)
