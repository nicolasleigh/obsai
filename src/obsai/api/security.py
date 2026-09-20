"""The boundary that keeps this server local.

This API reads a personal Vault and, in later phases, writes to it. It has no
authentication, by design: it is meant to be reachable only from the machine it
runs on. Two checks make that true even though the browser is the client:

**Host must be a loopback name.** A page on ``evil.com`` can point a hostname at
``127.0.0.1`` and then fetch this API; the browser will happily connect, because
from its point of view it is talking to ``evil.com``. What it cannot fake is the
``Host`` header, which stays ``evil.com``. Requiring a loopback hostname breaks
that attack (DNS rebinding) without needing a token.

Only the *hostname* is checked, not the port: the threat is the name, and the
port varies with ``OBSAI_UI_PORT`` and with the Vite dev server, which forwards
requests with the browser's original ``Host`` (``changeOrigin: false``).

**Origin must be loopback when present.** A request that carries an
``Origin`` was made by a browser on behalf of a document, so the document's
origin is checked. An absent ``Origin`` is allowed, because that is what a
same-origin ``GET`` and every non-browser client (``curl``, the tests) send.
``Origin: null`` is rejected: it comes from sandboxed iframes and ``data:`` URLs,
which is precisely the shape an attacker would use.

Both middlewares are written as raw ASGI rather than ``BaseHTTPMiddleware``. The
latter buffers responses, which would break the server-sent event stream that the
job UI needs in phase C.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from obsai.api.errors import envelope

#: Hostnames that mean "this machine". Anything else is refused.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: The IPv4 loopback the server binds to. Never ``0.0.0.0``: that would expose the
#: Vault to the local network.
BIND_HOST = "127.0.0.1"

#: Accepted as an incoming request ID. Restricting the charset matters: the value
#: ends up in log lines, so a newline would let a caller forge entries, and an
#: unbounded value would let it flood them.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

REQUEST_ID_HEADER = "X-Request-ID"


def hostname_of(host_header: str) -> str:
    """The hostname part of a ``Host`` header, lowercased, brackets stripped.

    ``"[::1]:8000"`` → ``"::1"``, ``"127.0.0.1:5173"`` → ``"127.0.0.1"``.
    """
    value = host_header.strip()
    if value.startswith("["):
        return value.partition("]")[0].lstrip("[").lower()
    return value.partition(":")[0].lower()


def is_loopback_host(host_header: str) -> bool:
    return hostname_of(host_header) in LOOPBACK_HOSTS


def is_allowed_origin(origin: str) -> bool:
    """Whether a browser document at ``origin`` may talk to this server."""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return parts.hostname.lower() in LOOPBACK_HOSTS


class LocalOnlyMiddleware:
    """Refuse any request that did not come from a loopback document."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if not is_loopback_host(host):
            # 400: for this server the request is malformed, not merely forbidden.
            await _reject(
                scope, receive, send,
                status_code=400,
                code="invalid_host",
                message=f"Host {host!r} is not a loopback address; this API is local-only",
            )
            return
        origin = headers.get("origin")
        if origin is not None and not is_allowed_origin(origin):
            await _reject(
                scope, receive, send,
                status_code=403,
                code="cross_origin",
                message=f"Origin {origin!r} is not allowed; this API is local-only",
            )
            return
        await self.app(scope, receive, send)


class RequestIdMiddleware:
    """Give every request an identifier, and echo it back.

    Incoming IDs are accepted so a caller can correlate its own retries, but only
    if they match :data:`_REQUEST_ID`; anything else is replaced rather than
    rejected, because a bad correlation ID is not worth failing a request over.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_id)


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
) -> None:
    """Answer without running the application, in the shared error envelope."""
    request_id = scope.get("state", {}).get("request_id")
    response = JSONResponse(
        status_code=status_code,
        content=envelope(code, "LocalOnly", message, request_id=request_id),
    )
    await response(scope, receive, send)
