"""The ASGI application for the local UI.

Assembled by :func:`create_app` rather than at import time, so importing this
module has no side effects: no configuration is read, no index is opened and no
socket is bound. ``uvicorn obsai.api.app:app`` still works because the module ends
by calling the factory once.

The lifespan is not decoration. A Vault that needs recovery, a missing index or an
unusable configuration all produce failures that look like bugs from a browser, so
the server says them once, in its own log, using the same read the overview screen
will use. That is why the startup check calls :func:`read_status` instead of
re-deriving the conditions: the log and the UI cannot disagree.

The lifespan also owns the two things that are neither per-request nor module-level:
the process facts the overview screen reports, and the job runners. A runner is
created on demand and opens its journal later still, so a server that is only ever
asked questions leaves no file behind.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI

from obsai import __version__
from obsai.api.deps import AppState, JobRegistry
from obsai.api.errors import install_error_handlers
from obsai.api.routes import agent as agent_routes
from obsai.api.routes import ask as ask_routes
from obsai.api.routes import changes as changes_routes
from obsai.api.routes import consent as consent_routes
from obsai.api.routes import embedding as embedding_routes
from obsai.api.routes import index_jobs as index_jobs_routes
from obsai.api.routes import jobs as jobs_routes
from obsai.api.routes import notes as notes_routes
from obsai.api.routes import organize as organize_routes
from obsai.api.routes import search as search_routes
from obsai.api.routes import status as status_routes
from obsai.api.routes import transactions as transactions_routes
from obsai.api.security import LocalOnlyMiddleware, RequestIdMiddleware
from obsai.application.index import open_index
from obsai.application.jobs import recover_interrupted_jobs
from obsai.application.paths import database_path
from obsai.application.status import read_status
from obsai.config.loader import load_settings
from obsai.errors import ObsAIError

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def _announce_startup() -> None:
    """Repair what a previous process left open, then report the starting state.

    Neither half may fail the boot: a server that refuses to start cannot tell the
    browser *why* the configuration is unusable, which is the whole point of the
    overview screen.

    Recovery goes through :func:`recover_interrupted_jobs` rather than through a
    registered runner, because it is a repair rather than a job: it opens the
    journal, closes out what a dead process left open and lets go of it. The runner
    a request later asks for opens the same file again, and SQLite is happy to have
    two readers of one journal.
    """
    try:
        settings = load_settings()
    except ObsAIError as exc:
        # Not fatal on purpose: the UI must be able to show *why* the
        # configuration is unusable, and a server that refuses to start cannot.
        # Each request reports it as a 400 through the error handlers.
        logger.error("Configuration is unusable: %s", exc)
        return

    try:
        interrupted = recover_interrupted_jobs(database_path(settings))
    except (ObsAIError, sqlite3.Error) as exc:
        # A job record that was running when the process died has no result to
        # restore, only a status that must stop claiming to be running. Failing
        # here would take the whole server down over a repair nobody asked for.
        logger.error("Job journal cannot be used: %s", exc)
    else:
        if interrupted:
            logger.warning(
                "%d job(s) were still open when the previous process exited; "
                "marked interrupted",
                interrupted,
            )

    handle = open_index(settings)
    try:
        view = read_status(settings, handle)
        if view.vault_path is None:
            logger.warning("No vault configured; set vault.path in config.toml")
        elif not view.vault_ready:
            logger.warning("Configured vault does not exist: %s", view.vault_path)
        if not view.index.exists:
            logger.warning("Index has not been built; run 'obsai index update'")
        elif not view.index.usable:
            logger.error("Index cannot be opened: %s", view.index.error)
        if view.recovery_required:
            pending = ", ".join(
                item.transaction_id for item in view.unfinished_transactions
            )
            logger.warning(
                "Recovery required for transaction(s) %s; run "
                "'obsai transaction recover ID'",
                pending,
            )
    finally:
        handle.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.obsai = AppState(started_at=datetime.now(timezone.utc))
    # Empty, and deliberately so: the first request that needs a runner builds one,
    # and the journal is opened later than that. Nothing here touches the disk.
    app.state.jobs = JobRegistry()
    _announce_startup()
    try:
        yield
    finally:
        # Live jobs are cancelled cooperatively and waited for, so one that is
        # mid-file-transaction still gets to roll back instead of being abandoned.
        # uvicorn has already closed the event streams by this point — see
        # ``api/routes/jobs.py`` for why that ordering is what makes it safe.
        app.state.jobs.close()
        logger.info("ObsAgent UI stopped")


def create_app() -> FastAPI:
    """Build the application: middleware, error handlers, routes."""
    application = FastAPI(
        title="ObsAgent UI",
        description="Local-only HTTP adapter over the shared ObsAgent application services.",
        version=__version__,
        lifespan=lifespan,
    )
    # The last middleware added is the outermost, so the request ID is assigned
    # before the local-only check can reject a request — a rejection is exactly the
    # case where a correlatable ID matters most.
    application.add_middleware(LocalOnlyMiddleware)
    application.add_middleware(RequestIdMiddleware)
    install_error_handlers(application)
    application.include_router(status_routes.router, prefix=API_PREFIX)
    application.include_router(search_routes.router, prefix=API_PREFIX)
    application.include_router(consent_routes.router, prefix=API_PREFIX)
    application.include_router(ask_routes.router, prefix=API_PREFIX)
    application.include_router(notes_routes.router, prefix=API_PREFIX)
    application.include_router(jobs_routes.router, prefix=API_PREFIX)
    application.include_router(index_jobs_routes.router, prefix=API_PREFIX)
    application.include_router(embedding_routes.router, prefix=API_PREFIX)
    application.include_router(changes_routes.router, prefix=API_PREFIX)
    application.include_router(organize_routes.router, prefix=API_PREFIX)
    application.include_router(transactions_routes.router, prefix=API_PREFIX)
    application.include_router(agent_routes.router, prefix=API_PREFIX)
    return application


app = create_app()
