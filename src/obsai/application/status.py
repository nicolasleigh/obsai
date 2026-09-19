"""One read that answers "what state is this Vault in?".

``obsai status`` prints two lines and stops: the version and the Vault path. That
is enough for a terminal greeting and useless for an overview screen, which needs
the index state, the stale notes and any unfinished transaction *before* it can
decide what to show.

The interesting property here is that this function is **total**. Every degraded
state is a value, not an exception:

- no Vault configured          → ``vault_path is None``
- Vault configured but gone    → ``vault_ready is False``
- index never built            → ``index.exists is False``
- index file unreadable        → ``index.error`` set, ``index.usable is False``
- a transaction is unfinished  → ``recovery_required is True`` plus the journals
- another process is writing   → ``locked is True``

A caller can therefore render a complete "here is what is wrong and what to do
about it" screen from one successful response. The CLI cannot do that today: it
fails on the first problem and never reports the rest.
"""

from __future__ import annotations

from pathlib import Path

from obsai import __version__
from obsai.application.changes import journal_views
from obsai.application.dto import DirtyNoteView, IndexStatusView, StatusView
from obsai.application.embedding import build_generation
from obsai.application.index import IndexHandle
from obsai.application.locks import is_locked
from obsai.config.models import Settings
from obsai.errors import ObsAIError
from obsai.storage.repositories import IndexRepository
from obsai.transactions.journal import UNFINISHED, list_journals


def _journals(root: Path) -> list[dict]:
    """Every journal on disk, or none when the Vault cannot be inspected.

    ``list_journals`` raises :class:`RecoveryRequiredError` for a journal directory
    that exists but is malformed. That is a real problem, but it must not stop the
    status read from reporting the *rest* of the state, so it degrades to "no
    journals known" and the caller still gets the Vault and index sections.
    """
    try:
        return list_journals(root)
    except ObsAIError:
        return []


def _index_status(handle: IndexHandle, settings: Settings) -> IndexStatusView:
    if not handle.usable:
        return IndexStatusView(
            path=str(handle.path),
            exists=handle.exists,
            usable=False,
            error=handle.error,
        )

    database = handle.database
    assert database is not None
    connection = database.connection
    generation = build_generation(settings.embedding)
    note_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    known = connection.execute(
        "SELECT 1 FROM embedding_generations WHERE id = ?", (generation.id,)
    ).fetchone()
    vector_count = (
        connection.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE generation_id = ?",
            (generation.id,),
        ).fetchone()[0]
        if known is not None
        else 0
    )
    return IndexStatusView(
        path=str(handle.path),
        exists=True,
        usable=True,
        note_count=note_count,
        chunk_count=chunk_count,
        vector_count=vector_count,
        # The configured generation is only reported once the index actually stores
        # it: an ID the index has never heard of would read as "semantic search is
        # ready" on a fresh index that has no vectors at all.
        generation=generation.id if known is not None else None,
        semantic_ready=vector_count > 0,
        dirty_notes=tuple(
            DirtyNoteView(path=item.path, reason=item.reason, marked_at=item.marked_at)
            for item in IndexRepository(database).list_dirty()
        ),
    )


def read_status(settings: Settings, index: IndexHandle) -> StatusView:
    """Collect Vault, index and transaction state in one pass.

    ``index`` comes from :func:`obsai.application.index.open_index`, so the
    endpoint never decides whether the index should be opened — and never fails
    because it could not be.
    """
    vault_path: str | None = None
    vault_ready = False
    root: Path | None = None
    if settings.vault.path is not None:
        root = settings.vault.path.expanduser()
        vault_path = str(root)
        vault_ready = root.is_dir()

    journals = _journals(root) if vault_ready and root is not None else []
    unfinished = journal_views([item for item in journals if item["status"] in UNFINISHED])
    dirty_journals = journal_views(
        [item for item in journals if item["status"] in ("committed", "index_dirty")]
    )

    return StatusView(
        version=__version__,
        vault_path=vault_path,
        vault_ready=vault_ready,
        index=_index_status(index, settings),
        unfinished_transactions=unfinished,
        index_dirty_transactions=dirty_journals,
        recovery_required=bool(unfinished),
        locked=is_locked(index.path),
    )
