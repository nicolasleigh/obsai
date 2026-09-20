"""B-1 acceptance: one error envelope, one status table.

The mapping is the contract the frontend switches on, so it is pinned here rather
than left to whichever ``isinstance`` chain a route happens to write. Two
properties matter and both are easy to break by accident:

* the *most specific* registered class wins — ``RecoveryRequiredError`` is a
  ``TransactionError`` is a ``SafeWriteError``, and a flat chain would have to be
  ordered correctly by hand at every call site;
* every failure, including a 404 and an unexpected crash, uses the same JSON shape,
  because a second shape means a second parser in the frontend.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from obsai.api.errors import (
    FALLBACK_STATUS,
    STATUS_BY_ERROR,
    envelope,
    error_code,
    error_payload,
    http_status,
    install_error_handlers,
)
from obsai.api.security import RequestIdMiddleware
from obsai.application.locks import LockBusyError, LockUnsafeError
from obsai.errors import (
    CollisionError,
    ConfigError,
    ConflictError,
    ConsentExpiredError,
    ConsentRequiredError,
    ContextError,
    EmbeddingBudgetError,
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingServiceError,
    InvalidEncodingError,
    LLMError,
    NotFoundError,
    ObsAIError,
    ParseError,
    RecoveryRequiredError,
    SafeWriteError,
    SchemaError,
    SemanticIndexMissingError,
    SemanticUnavailableError,
    TransactionError,
    VaultError,
)

UNREGISTERED_DOMAIN_ERRORS = [
    ObsAIError,
    VaultError,
    ParseError,
    SchemaError,
    ContextError,
    LLMError,
    SafeWriteError,
    TransactionError,
    InvalidEncodingError,
    # ``EmbeddingError`` stays unregistered on purpose: it is what a provider
    # raises when it fails *mid-query*, which is an outage rather than a bad
    # request. B-8 registered two of its subclasses for the cases that are not —
    # see ``test_the_embedding_family_did_not_become_a_client_error``.
    EmbeddingError,
    EmbeddingRateLimitError,
    EmbeddingServiceError,
    LockUnsafeError,
]


def client_raising(exc: BaseException, *, with_request_id: bool = False) -> TestClient:
    app = FastAPI()
    if with_request_id:
        app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    @app.get("/counted")
    def counted(limit: int = Query(1)) -> dict[str, int]:
        return {"limit": limit}

    # ``raise_server_exceptions=False`` is what lets the catch-all handler's
    # response be observed: Starlette re-raises after answering so uvicorn still
    # logs the traceback.
    return TestClient(app, raise_server_exceptions=False)


def body(response: Any) -> dict[str, Any]:
    # Asserting the content type first keeps the failure readable: when the
    # application crashes before answering, httpx returns an empty body and
    # ``response.json()`` would raise a bare JSONDecodeError instead.
    assert response.headers.get("content-type", "").startswith("application/json"), (
        f"expected a JSON envelope, got {response.status_code} {response.text!r}"
    )
    payload = response.json()
    assert set(payload) <= {"error", "request_id"}, payload
    assert set(payload["error"]) <= {"code", "type", "message", "details"}
    return payload["error"]


@pytest.mark.parametrize(
    "error_class", sorted(STATUS_BY_ERROR, key=lambda cls: cls.__name__), ids=lambda c: c.__name__
)
def test_every_registered_class_maps_to_its_status(error_class: type[Exception]) -> None:
    assert http_status(error_class("boom")) == STATUS_BY_ERROR[error_class]


@pytest.mark.parametrize("error_class", UNREGISTERED_DOMAIN_ERRORS, ids=lambda c: c.__name__)
def test_unregistered_domain_errors_are_server_failures(error_class: type[Exception]) -> None:
    assert http_status(error_class("boom")) == FALLBACK_STATUS


def test_the_most_specific_registered_ancestor_wins() -> None:
    """``RecoveryRequiredError`` must not inherit a broader mapping by accident."""
    assert http_status(RecoveryRequiredError("boom")) == 423
    assert http_status(CollisionError("boom")) == 409
    assert http_status(EmbeddingBudgetError("boom")) == 429
    # And a subclass added later inherits its parent rather than falling to 500.
    class LateConflict(ConflictError):
        """A future, more specific conflict."""

    assert http_status(LateConflict("boom")) == 409


def test_a_newly_registered_subclass_overrides_its_parent() -> None:
    class Specific(ConfigError):
        """Registered below to prove the lookup honours the MRO, not the dict order."""

    STATUS_BY_ERROR[Specific] = 418
    try:
        assert http_status(Specific("boom")) == 418
    finally:
        del STATUS_BY_ERROR[Specific]


@pytest.mark.parametrize(
    ("error_class", "code"),
    [
        (ConfigError, "config"),
        (ConflictError, "conflict"),
        (CollisionError, "collision"),
        (RecoveryRequiredError, "recovery_required"),
        (EmbeddingBudgetError, "embedding_budget"),
        (LockBusyError, "lock_busy"),
        (NotFoundError, "not_found"),
        (ConsentRequiredError, "consent_required"),
        (ConsentExpiredError, "consent_expired"),
        (SemanticIndexMissingError, "semantic_index_missing"),
        (SemanticUnavailableError, "semantic_unavailable"),
        (SchemaError, "schema"),
        (ObsAIError, "obs_ai"),
    ],
)
def test_error_codes_are_stable_identifiers(error_class: type[Exception], code: str) -> None:
    assert error_code(error_class("boom")) == code


def test_the_envelope_is_the_same_shape_for_every_failure() -> None:
    assert envelope("x", "T", "m") == {"error": {"code": "x", "type": "T", "message": "m"}, "request_id": None}
    assert envelope("x", "T", "m", details=[1], request_id="r") == {
        "error": {"code": "x", "type": "T", "message": "m", "details": [1]},
        "request_id": "r",
    }
    assert error_payload(ConfigError("nope")) == {
        "error": {"code": "config", "type": "ConfigError", "message": "nope"},
        "request_id": None,
    }


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (ConfigError("no vault"), 400, "config"),
        (ConflictError("changed"), 409, "conflict"),
        (CollisionError("exists"), 409, "collision"),
        (RecoveryRequiredError("recover first"), 423, "recovery_required"),
        (EmbeddingBudgetError("too expensive"), 429, "embedding_budget"),
        (LockBusyError("busy"), 423, "lock_busy"),
        (NotFoundError("gone"), 404, "not_found"),
        (ConsentRequiredError("not approved"), 409, "consent_required"),
        (ConsentExpiredError("expired"), 410, "consent_expired"),
        (SemanticIndexMissingError("no vectors"), 400, "semantic_index_missing"),
        (SemanticUnavailableError("provider down"), 503, "semantic_unavailable"),
        (SchemaError("corrupt"), 500, "schema"),
        (RuntimeError("bug"), 500, "internal"),
    ],
    ids=[
        "config",
        "conflict",
        "collision",
        "recovery",
        "budget",
        "lock",
        "not-found",
        "consent-required",
        "consent-expired",
        "semantic-index",
        "semantic-unavailable",
        "schema",
        "crash",
    ],
)
def test_a_domain_failure_becomes_the_envelope_over_http(
    exc: Exception, status: int, code: str
) -> None:
    response = client_raising(exc).get("/boom")
    assert response.status_code == status
    assert body(response) == {"code": code, "type": type(exc).__name__, "message": str(exc)}


def test_details_travel_with_the_envelope() -> None:
    """``ObsAIError.details`` reaches the wire, which is how the consent dialog works.

    A UI that only ever saw a 409 has to be able to put up the dialog from that
    response alone. Without the challenge travelling along, the caller learns that a
    decision is needed but not what it costs, and needs a second round trip to find
    out — the round trip the challenge exists to avoid.
    """
    exc = ConsentRequiredError("approve first", details={"consent": {"consent_id": "abc"}})
    response = client_raising(exc).get("/boom")

    assert response.status_code == 409
    assert body(response)["details"] == {"consent": {"consent_id": "abc"}}


def test_details_are_omitted_when_there_are_none() -> None:
    """The key must be absent rather than ``null``, since callers test for presence."""
    error = body(client_raising(ConfigError("plain")).get("/boom"))

    assert "details" not in error


def test_the_embedding_family_did_not_become_a_client_error() -> None:
    """B-8 registered two ``EmbeddingError`` subclasses without touching the parent.

    Mapping the whole family to a 4xx would have been the easy way to fix the
    ``strict_semantic`` 500, and it would have been wrong: a provider that dies
    mid-query is an outage, and telling a user to check their configuration during
    one is worse than a wrong status class. Only the two conditions that are *not*
    outages got a status.
    """
    assert http_status(EmbeddingError("provider died")) == FALLBACK_STATUS
    assert http_status(SemanticUnavailableError("no backend")) == 503
    assert http_status(SemanticIndexMissingError("no vectors")) == 400


def test_a_missing_route_uses_the_same_envelope() -> None:
    """An unknown *route* is ``http_error``; an unknown *note* is ``not_found``.

    Both are 404s with the same shape, which is the point — the frontend needs one
    parser. They carry different codes because the next step differs: a bad URL is
    the user's mistake, a missing note is a stale bookmark.
    """
    response = client_raising(ConfigError("x")).get("/nowhere")
    assert response.status_code == 404
    assert body(response)["code"] == "http_error"
    assert body(response)["message"] == "Not Found"


def test_a_bad_request_parameter_uses_the_same_envelope() -> None:
    response = client_raising(ConfigError("x")).get("/counted", params={"limit": "many"})
    assert response.status_code == 422
    error = body(response)
    assert error["code"] == "invalid_request"
    assert error["type"] == "RequestValidationError"
    # The raw pydantic errors are kept: the frontend needs the field name.
    assert error["details"][0]["loc"] == ["query", "limit"]


def test_the_request_id_lands_in_the_error_body() -> None:
    response = client_raising(ConfigError("x"), with_request_id=True).get(
        "/boom", headers={"X-Request-ID": "trace-42"}
    )
    assert response.json()["request_id"] == "trace-42"
    assert response.headers["X-Request-ID"] == "trace-42"


def test_a_rejection_before_the_app_still_carries_a_request_id() -> None:
    """The local-only middleware answers without reaching a route, so it has to
    build the envelope itself — and must still be correlatable in the log."""
    from obsai.api.security import LocalOnlyMiddleware

    app = FastAPI()
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/anything")
    def anything() -> dict[str, bool]:
        return {"reached": True}

    client = TestClient(app, base_url="http://127.0.0.1:8000")
    response = client.get("/anything", headers={"Host": "evil.com", "X-Request-ID": "trace-7"})
    assert response.status_code == 400
    assert response.json()["request_id"] == "trace-7"
