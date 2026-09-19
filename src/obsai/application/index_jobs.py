"""The two index maintenance jobs, as work the runner can execute.

``obsai index update`` and ``obsai index rebuild`` used to be command bodies. A
page has to be able to start the same two operations and watch them, so the
operations move here and both adapters submit them. Nothing in this module
renders, prompts, or knows that HTTP exists.

Four decisions are load-bearing.

* **Configuration is resolved when the job is built, not when it runs.** Each
  factory takes :class:`~obsai.config.models.Settings` and returns a callable, so
  a missing Vault and a Vault frozen for recovery are both raised while the POST
  is still open — a 400 and a 423 respectively. Validating inside the callable
  instead would answer 202 and then hand the user a failure they could have been
  told about before anything was queued. The job re-checks anyway; the state can
  change between submitting and running, and *that* check is the one that guards
  the work.
* **The write lock is taken by the job, not by the request.** It has to be held
  for as long as the work runs, and the work outlives the request that started
  it. The operation name travels with it, which is what makes a collision say
  "another ObsAgent *index rebuild* is already writing".
* **``message`` is a step name, not a sentence.** A job record is written once and
  read by adapters that render Chinese, so this layer publishes the same kind of
  stable identifier it publishes everywhere else and the adapter supplies the
  words. ``web/src/lib/jobs.ts`` owns the translation, and a contract test keeps
  the two vocabularies equal.
* **A step is only reported if it can be observed.** ``ShadowIndexRebuilder``
  builds a shadow database, checks its integrity and counts, compares the live
  file's fingerprint and swaps it in — and reports none of that. The rebuild job
  therefore reports the Vault scan, the rebuild as one step, and the end. Naming
  phases it cannot see would be a progress bar describing a program the user is
  not running.
"""

from __future__ import annotations

from typing import Any

from obsai.application.jobs import JobCallable, JobContext
from obsai.application.locks import vault_lock
from obsai.application.paths import database_path, require_existing_vault
from obsai.config.models import Settings
from obsai.indexing import IncrementalIndexer, ShadowIndexRebuilder, UpdateResult
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionService
from obsai.vault import scan_markdown_files

INDEX_UPDATE_KIND = "index-update"
INDEX_REBUILD_KIND = "index-rebuild"

STEP_SCANNING = "scanning"
STEP_SYNCHRONIZING = "synchronizing"
STEP_BUILDING = "building"
STEP_DONE = "done"

EMBEDDINGS_STALE = "embeddings_stale"
"""Detail key set by a finished rebuild: every vector in the replaced index is gone.

A rebuild writes a fresh database, so the vector table starts empty and the
semantic half of hybrid search quietly stops contributing. The job cannot fix
that — generating embeddings needs its own cost preflight and its own consent —
so it reports the fact and the page turns it into the one instruction that
resolves it. Carrying it as a field rather than as prose in ``message`` is what
lets the page show a link instead of asking the user to read English.
"""


def _counts(result: UpdateResult, *, notes: int) -> dict[str, Any]:
    """The outcome a page renders: how big the job was and what it changed.

    ``notes`` is the Vault as it was scanned before the work started rather than
    ``len(result.changes)``, which also counts the rows of notes that were deleted
    and would therefore report a Vault larger than the one on disk.
    """
    return {
        "notes": notes,
        "created": result.count("created"),
        "modified": result.count("modified"),
        "renamed": result.count("renamed"),
        "moved": result.count("moved"),
        "deleted": result.count("deleted"),
        "unchanged": result.count("unchanged"),
        "affected_links": result.affected_link_count,
    }


def _sized(context: JobContext, vault) -> int:
    """Report the scan, then the size of the job, and return that size.

    The Vault is walked here and again by the indexer. That is deliberate: a
    progress line with no number in it does not tell a user whether to wait two
    seconds or two minutes, and a directory walk costs a fraction of hashing,
    parsing and writing the same files. The first ``progress`` call also lands
    before any of the work, so it is the checkpoint that makes a cancel during the
    walk take effect.
    """
    context.progress(STEP_SCANNING)
    notes = len(scan_markdown_files(vault))
    return notes


def index_update_job(settings: Settings) -> JobCallable:
    """Build the incremental update job for the configured Vault and index.

    Raises before anything is queued — ``ConfigError`` when the Vault is unset,
    missing, or not a directory, and ``RecoveryRequiredError`` when an unfinished
    transaction means the Vault cannot be read consistently yet.
    """
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    # The same guard the job runs, applied early so a frozen Vault refuses the
    # request instead of queueing work that cannot start.
    TransactionService(vault).ensure_ready()

    def work(context: JobContext) -> dict[str, Any]:
        notes = _sized(context, vault)
        context.progress(STEP_SYNCHRONIZING, notes=notes)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with vault_lock(index_path, operation="index update"):
            # Inside the lock, unlike the check above: holding it means no writer
            # can be mid-transaction, so anything unfinished found here is a
            # leftover rather than a race.
            TransactionService(vault).ensure_ready()
            with Database(index_path) as database:
                result = IncrementalIndexer(IndexRepository(database)).update(vault)
            # Reopens the index, so it has to run after the update's connection is
            # closed and before the lock is dropped.
            TransactionService(vault, database_path=index_path).clear_index_dirty()
        context.progress(STEP_DONE)
        return _counts(result, notes=notes)

    return work


def index_rebuild_job(settings: Settings) -> JobCallable:
    """Build the full rebuild job: a fresh index, validated, then swapped in.

    The job adds nothing to the rebuild's safety. ``ShadowIndexRebuilder`` already
    refuses to run beside a WAL sidecar, refuses to start twice, validates the
    shadow database before touching the live one, and does the replace inside a
    deferred-signal region — so an interrupted rebuild leaves the previous index
    in place. What this adds is a job record, a step to show and a cancel button.
    """
    vault = require_existing_vault(settings)
    index_path = database_path(settings)
    TransactionService(vault).ensure_ready()

    def work(context: JobContext) -> dict[str, Any]:
        notes = _sized(context, vault)
        context.progress(STEP_BUILDING, notes=notes)
        with vault_lock(index_path, operation="index rebuild"):
            # No second ``ensure_ready`` here, unlike the update job: ``rebuild``
            # opens with exactly that check, and it is reached under this lock.
            result = ShadowIndexRebuilder(vault, index_path).rebuild()
        context.progress(STEP_DONE)
        detail = _counts(result, notes=notes)
        detail[EMBEDDINGS_STALE] = True
        return detail

    return work
