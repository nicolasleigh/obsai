"""Domain errors translated into HTTP responses.

The mapping is a policy decision, not a mechanical one, so it lives in one table:

========================  ====  ==============================================
domain error              HTTP  why
========================  ====  ==============================================
``ConfigError``           400   the request cannot be served as configured
``SemanticIndexMissing``  400   the index holds no vectors for this generation
``NotFoundError``         404   the entity is not in the derived index
``ConsentRequired``       409   a query that cannot degrade needs a decision
``LockBusy``              409   another writer holds the Vault lock — retry later
``Conflict``              409   the source changed since the change was prepared
``Collision``             409   the destination already exists
``ConsentExpired``        410   the challenge is gone; search again
``Recovery``              423   writes are frozen until a transaction is recovered
``Budget``                429   the request would exceed the embedding ceiling
``SemanticUnavailable``   503   the semantic backend could not be reached
everything else           500   an unexpected failure
========================  ====  ==============================================

Four details are deliberate:

* **Resolution walks the MRO.** ``RecoveryRequiredError`` is a ``TransactionError``
  is a ``SafeWriteError``; a flat ``isinstance`` chain would answer in whatever
  order it happened to be written. Looking up each ancestor in turn means the
  *most specific* registered class always wins, and adding a subclass later cannot
  silently steal its parent's status.
* **The payload is one stable shape.** ``{"error": {"code", "type", "message"}}``
  is what the frontend switches on to choose a Chinese message; the raw domain
  string is carried alongside for the case where no translation exists yet.
* **Errors may carry structured details.** ``error.details`` holds whatever the
  domain put on ``ObsAIError.details``. The consent challenge travels there so a
  UI can show what a decision costs without a round trip to ask for it — the
  alternative is a UI that knows a decision is needed but not what it is about.
* **Unknown exceptions are still enveloped.** A bare ``Internal Server Error``
  body tells the UI nothing. The traceback is not lost: Starlette re-raises after
  the handler, so uvicorn still logs it exactly as before.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from obsai.application.locks import LockBusyError
from obsai.errors import (
    CollisionError,
    ConfigError,
    ConflictError,
    ConsentExpiredError,
    ConsentRequiredError,
    EmbeddingBudgetError,
    NotFoundError,
    ObsAIError,
    PlanDriftError,
    PlanExpiredError,
    PlanNotFoundError,
    RecoveryRequiredError,
    SemanticIndexMissingError,
    SemanticUnavailableError,
)

#: Domain class → HTTP status. Consulted through the exception's MRO.
STATUS_BY_ERROR: dict[type[Exception], int] = {
    ConfigError: 400,
    SemanticIndexMissingError: 400,
    NotFoundError: 404,
    PlanNotFoundError: 404,
    PlanExpiredError: 404,
    ConsentRequiredError: 409,
    LockBusyError: 423,
    ConflictError: 409,
    CollisionError: 409,
    PlanDriftError: 409,
    ConsentExpiredError: 410,
    RecoveryRequiredError: 423,
    EmbeddingBudgetError: 429,
    SemanticUnavailableError: 503,
}

FALLBACK_STATUS = 500


def http_status(exc: Exception) -> int:
    """The status for ``exc``, preferring the most specific registered ancestor."""
    for klass in type(exc).__mro__:
        status = STATUS_BY_ERROR.get(klass)
        if status is not None:
            return status
    return FALLBACK_STATUS


def error_code(exc: Exception) -> str:
    """A stable, machine-readable identifier derived from the class name.

    ``RecoveryRequiredError`` becomes ``recovery_required``: the ``Error`` suffix
    carries no information the ``type`` field does not already have, and a shorter
    code is easier for the frontend to match on. Runs of capitals stay together, so
    ``ObsAIError`` is ``obs_ai`` rather than ``obs_a_i``.
    """
    name = type(exc).__name__
    if name.endswith("Error"):
        name = name[: -len("Error")]
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return name.lower()


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def envelope(
    code: str,
    type_name: str,
    message: str,
    *,
    details: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """The single error envelope every failure shares.

    Kept separate from :func:`error_payload` because the security middleware
    rejects a request before any exception exists — it still has to produce the
    same shape, or the frontend needs two parsers.
    """
    error: dict[str, Any] = {"code": code, "type": type_name, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error, "request_id": request_id}


def error_payload(
    exc: Exception,
    *,
    code: str | None = None,
    details: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return envelope(
        code or error_code(exc),
        type(exc).__name__,
        str(exc),
        details=details,
        request_id=request_id,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Attach the handlers that turn every failure into the envelope.

    The envelope has to be *universal* to be useful: a frontend that has to parse
    FastAPI's ``{"detail": ...}`` for a 404 and ours for a 409 has two parsers and
    one of them will rot. Hence the handler for plain HTTP errors as well.
    """

    @app.exception_handler(ObsAIError)
    async def handle_domain_error(request: Request, exc: ObsAIError) -> JSONResponse:
        return JSONResponse(
            status_code=http_status(exc),
            content=error_payload(
                exc, details=exc.details, request_id=_request_id(request)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(
                "http_error",
                type(exc).__name__,
                detail,
                request_id=_request_id(request),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's default 422 body has a different shape; normalise it."""
        return JSONResponse(
            status_code=422,
            content=error_payload(
                exc,
                code="invalid_request",
                details=exc.errors(),
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=FALLBACK_STATUS,
            content=error_payload(exc, code="internal", request_id=_request_id(request)),
        )
