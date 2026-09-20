"""Liveness and the overview read.

``/health`` is a probe: it answers without touching configuration, the filesystem
or the index, so a supervisor can tell "the process is up" from "the process can
do useful work".

``/status`` is the opposite: it is the one call the overview screen makes, and it
is deliberately *total*. A missing Vault, an unbuilt index and a pending recovery
are all ordinary states that the screen must render, so none of them is allowed to
become an error response. See :mod:`obsai.application.status`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from obsai.api.deps import AppState, get_app_state, get_settings, index_handle
from obsai.application.dto import StatusView
from obsai.application.index import IndexHandle
from obsai.application.status import read_status
from obsai.config.models import Settings

router = APIRouter(tags=["status"])


class StatusResponse(StatusView):
    """The application's status plus the process facts only the adapter knows.

    Extending the contract rather than wrapping it keeps the JSON flat: the
    overview screen reads ``vault_path`` and ``index.dirty_notes`` at the same
    depth whether it is talking to HTTP or to anything else.
    """

    started_at: datetime
    uptime_seconds: float


def to_response(view: StatusView, state: AppState) -> StatusResponse:
    return StatusResponse(
        **view.model_dump(),
        started_at=state.started_at,
        uptime_seconds=state.uptime_seconds,
    )


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the development script and smoke tests."""
    return {"status": "ok"}


@router.get("/status", response_model=StatusResponse)
def get_status(
    settings: Settings = Depends(get_settings),
    index: IndexHandle = Depends(index_handle),
    state: AppState = Depends(get_app_state),
) -> StatusResponse:
    """Vault path, index state, stale notes, unfinished transactions — in one read."""
    return to_response(read_status(settings, index), state)
