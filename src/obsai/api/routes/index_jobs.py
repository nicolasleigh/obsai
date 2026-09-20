"""Starting an index operation, and the two ways it can be refused.

Two endpoints with one shape: queue the work, hand back the job that will run it.
The whole ``JobView`` is the response — not a bare id — because the page needs the
status and the timestamps immediately and gets every later change from the stream.
There is no ``Location`` header for the same reason: the only client already has the
id in its hand.

Four decisions are worth stating.

* **202, not 200.** The work has been accepted, not done. A 200 would be a claim
  about an outcome nobody has yet, and a page that has to guess whether a response
  means "started" or "finished" guesses wrong the first time a rebuild takes a
  minute.
* **The refusals happen before the queue.** Each job factory resolves the Vault and
  checks that it is not frozen for recovery, so a missing Vault is a 400 and an
  unfinished transaction is a 423 — both answers about *the request*, delivered
  while the user is still looking at the button. Queueing work that will fail for a
  reason knowable at submit time is a worse contract and a worse experience.
* **Neither route holds the write lock.** It has to be held for as long as the work
  runs, and the work outlives the request that started it, so the job takes it. A
  route that acquired it would have to keep it across the 202, which is precisely
  what a request-scoped dependency cannot do.
* **No index is required.** Building the index for the first time and synchronising
  one are the same operation, and a rebuild with nothing to replace simply produces
  the first index. Both routes therefore depend on configuration rather than on
  :func:`obsai.api.deps.index_handle`, and the button works on the fresh machine the
  overview screen is currently telling to run ``obsai index update``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import check_write_lock, get_job_runner, get_settings
from obsai.application.dto import JobView
from obsai.application.index_jobs import (
    INDEX_REBUILD_KIND,
    INDEX_UPDATE_KIND,
    index_rebuild_job,
    index_update_job,
)
from obsai.application.jobs import JobRunner
from obsai.config.models import Settings

router = APIRouter(tags=["jobs"])

ACCEPTED = 202
"""Accepted for processing — true the moment ``submit`` returns, which is once the
job is journaled and queued. It says nothing about the outcome, and that is the point.
"""


@router.post("/jobs/index-update", response_model=JobView, status_code=ACCEPTED)
def start_index_update(
    settings: Settings = Depends(get_settings),
    runner: JobRunner = Depends(get_job_runner),
    _lock: None = Depends(check_write_lock),
) -> JobView:
    """Queue an incremental update: changed notes in, derived rows out.

    Building and submitting are separate statements rather than one nested call, so
    that "the factory runs first and may refuse" is visible instead of resting on
    argument evaluation order.
    """
    work = index_update_job(settings)
    return runner.submit(INDEX_UPDATE_KIND, work)


@router.post("/jobs/index-rebuild", response_model=JobView, status_code=ACCEPTED)
def start_index_rebuild(
    settings: Settings = Depends(get_settings),
    runner: JobRunner = Depends(get_job_runner),
    _lock: None = Depends(check_write_lock),
) -> JobView:
    """Queue a full rebuild: a fresh index, validated, then swapped in."""
    work = index_rebuild_job(settings)
    return runner.submit(INDEX_REBUILD_KIND, work)
