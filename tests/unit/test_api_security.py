"""B-1 acceptance: the API answers only to a browser on this machine.

Two attacks are being closed here and it is worth naming them, because the tests
below are only meaningful in their light:

* **DNS rebinding.** A page on ``evil.com`` points a hostname at ``127.0.0.1``.
  The browser connects happily — but it sends ``Host: evil.com``, and that is the
  one header the page cannot forge. Requiring a loopback hostname stops it.
* **Cross-site reads.** A page on ``evil.com`` fetches ``http://127.0.0.1:8000``
  directly. The browser sends ``Origin: http://evil.com``, which is refused.

The hostname checks are tested as pure functions first, then through the real
middleware, because a check that is correct in isolation but wired to the wrong
header is still no protection.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from obsai.api.security import (
    LOOPBACK_HOSTS,
    LocalOnlyMiddleware,
    RequestIdMiddleware,
    hostname_of,
    is_allowed_origin,
    is_loopback_host,
)

BASE_URL = "http://127.0.0.1:8000"


def build(*, with_request_id: bool = False) -> TestClient:
    app = FastAPI()
    app.add_middleware(LocalOnlyMiddleware)
    if with_request_id:
        app.add_middleware(RequestIdMiddleware)

    @app.get("/reached")
    def reached() -> dict[str, bool]:
        return {"reached": True}

    return TestClient(app, base_url=BASE_URL)


@pytest.mark.parametrize(
    ("header", "hostname"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("localhost", "localhost"),
        ("localhost:5173", "localhost"),
        ("LOCALHOST:8000", "localhost"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("evil.com:8000", "evil.com"),
        ("127.0.0.1.evil.com", "127.0.0.1.evil.com"),
        ("", ""),
    ],
)
def test_hostname_of_splits_the_port_off(header: str, hostname: str) -> None:
    assert hostname_of(header) == hostname


@pytest.mark.parametrize(
    "header",
    ["127.0.0.1", "127.0.0.1:8000", "127.0.0.1:5173", "localhost:8000", "[::1]:8000"],
)
def test_loopback_hosts_are_accepted(header: str) -> None:
    assert is_loopback_host(header)


@pytest.mark.parametrize(
    "header",
    [
        "evil.com",
        "evil.com:8000",
        "127.0.0.1.evil.com",       # a suffix is a different host
        "evil.com:127.0.0.1",       # the port cannot smuggle a loopback name
        "0.0.0.0:8000",             # a bind address, never a client's Host
        "testserver",               # the naive TestClient default
        "",
    ],
)
def test_non_loopback_hosts_are_refused(header: str) -> None:
    assert not is_loopback_host(header)


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "https://127.0.0.1",
        "http://[::1]:5173",
    ],
)
def test_loopback_origins_are_allowed(origin: str) -> None:
    assert is_allowed_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.com",
        "https://evil.com:8000",
        "null",                       # sandboxed iframe or data: URL
        "file:///etc/passwd",
        "http://127.0.0.1.evil.com",  # lookalike
        "",
    ],
)
def test_other_origins_are_refused(origin: str) -> None:
    assert not is_allowed_origin(origin)


def test_the_allowed_hostnames_are_exactly_the_loopback_names() -> None:
    assert LOOPBACK_HOSTS == {"127.0.0.1", "localhost", "::1"}


def test_a_loopback_request_reaches_the_route() -> None:
    assert build().get("/reached").json() == {"reached": True}


def test_a_foreign_host_is_rejected_before_the_route() -> None:
    response = build().get("/reached", headers={"Host": "evil.com"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"
    assert "evil.com" in response.json()["error"]["message"]


def test_a_cross_site_origin_is_rejected() -> None:
    response = build().get("/reached", headers={"Origin": "http://evil.com"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_origin"


def test_a_null_origin_is_rejected() -> None:
    """``Origin: null`` comes from sandboxed frames; it must not be a bypass."""
    response = build().get("/reached", headers={"Origin": "null"})
    assert response.status_code == 403


def test_an_absent_origin_is_allowed() -> None:
    """A same-origin GET sends no Origin, and neither does curl. Refusing those
    would break the application itself."""
    assert build().get("/reached").status_code == 200


def test_the_vite_dev_server_origin_is_allowed() -> None:
    """The dev proxy forwards the browser's own Origin, so this must pass or the
    documented development workflow would not work."""
    assert build().get("/reached", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200


def test_the_request_id_is_generated_when_absent() -> None:
    response = build(with_request_id=True).get("/reached")
    assert len(response.headers["X-Request-ID"]) == 32


def test_a_well_formed_request_id_is_echoed() -> None:
    response = build(with_request_id=True).get(
        "/reached", headers={"X-Request-ID": "trace-abc_1.2"}
    )
    assert response.headers["X-Request-ID"] == "trace-abc_1.2"


@pytest.mark.parametrize(
    "supplied",
    [
        "bad value with spaces",
        "newline\ninjected",
        "x" * 65,
        "",
    ],
)
def test_a_malformed_request_id_is_replaced_not_rejected(supplied: str) -> None:
    """A bad correlation ID is not worth failing a request over — but it must not
    reach the log either, or a caller could forge log lines."""
    response = build(with_request_id=True).get("/reached", headers={"X-Request-ID": supplied})
    assert response.status_code == 200
    echoed = response.headers["X-Request-ID"]
    assert echoed != supplied
    assert len(echoed) == 32


def test_websocket_scopes_are_passed_through_untouched() -> None:
    """The middleware only inspects HTTP; anything else must not be dropped."""
    seen: list[str] = []

    async def inner(scope, receive, send):  # pragma: no cover - never invoked
        seen.append(scope["type"])

    import anyio

    middleware = LocalOnlyMiddleware(inner)
    anyio.run(middleware, {"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
