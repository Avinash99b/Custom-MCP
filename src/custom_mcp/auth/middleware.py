"""Pure ASGI authentication middleware.

The middleware is deliberately generic: it depends only on the
:class:`~custom_mcp.auth.base.Authenticator` ABC and the mechanism-agnostic
:class:`~custom_mcp.auth.base.AuthInfo` metadata an authenticator attaches to
``request.state``. Swapping authentication implementations therefore requires
no change here.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from custom_mcp.api.errors import error_response, security_headers
from custom_mcp.auth.base import Authenticator, get_auth_info
from custom_mcp.config import Settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC_EXACT = frozenset({"/healthz", "/login"})
_OAUTH_CALLBACK_RE = re.compile(r"^/mcp/[a-z0-9][a-z0-9-]{0,39}/oauth/callback$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_public_path(path: str) -> bool:
    """Return ``True`` for the exact public routes (no prefix surprises)."""
    if path in _PUBLIC_EXACT:
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    return _OAUTH_CALLBACK_RE.match(path) is not None


def normalize_origin(value: str) -> str | None:
    """Normalize an absolute origin for exact comparison.

    Returns ``scheme://host:port`` with a lowercased scheme/hostname and an
    explicit port (scheme default applied) so that ``https://host`` and
    ``https://host:443`` compare equal. Returns ``None`` for anything that is
    not a clean http(s) origin, including whitespace, control characters,
    userinfo, or malformed URLs.
    """
    if not value or any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if hostname is None:
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        return None
    return f"{scheme}://{hostname}:{port}"


def _scope_header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return bytes(value).decode("latin-1")
    return None


def _wants_html(scope: Scope) -> bool:
    accept = _scope_header(scope, b"accept") or ""
    return "text/html" in accept.lower()


def _is_api_or_mcp(path: str) -> bool:
    return path in {"/api", "/mcp"} or path.startswith(("/api/", "/mcp/"))


def _is_protected_mcp(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _should_no_store(path: str) -> bool:
    return path in {"/healthz", "/login", "/logout"} or _is_api_or_mcp(path)


def _message_is_json(message: Message) -> bool:
    for key, value in message.get("headers") or []:
        if key == b"content-type":
            return bytes(value).decode("latin-1").lower().startswith("application/json")
    return False


class AuthMiddleware:
    """Authenticate requests, enforce CSRF, and add security headers."""

    def __init__(self, app: ASGIApp, *, settings: Settings, authenticator: Authenticator) -> None:
        self.app = app
        self.settings = settings
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if is_public_path(path):
            await self._forward(scope, receive, send, no_store=_should_no_store(path))
            return

        request = Request(scope, receive=receive)
        principal = await self.authenticator.authenticate(request)
        if principal is None:
            await self._reject_unauthenticated(scope, receive, send)
            return

        auth_info = get_auth_info(request)
        method = auth_info.method if auth_info is not None else "session"

        if _is_protected_mcp(path) and method != "bearer":
            await self._send_error(
                scope,
                receive,
                send,
                401,
                "unauthorized",
                "Bearer authentication is required for MCP endpoints.",
                {"WWW-Authenticate": "Bearer"},
            )
            return

        if (
            scope.get("method") in UNSAFE_METHODS
            and method != "bearer"
            and not self._csrf_ok(scope)
        ):
            await self._send_error(
                scope,
                receive,
                send,
                403,
                "forbidden",
                "Cross-site request blocked.",
            )
            return

        scope.setdefault("state", {})["principal"] = principal
        await self._forward(scope, receive, send, no_store=_should_no_store(path))

    def _csrf_ok(self, scope: Scope) -> bool:
        expected = normalize_origin(self.settings.origin)
        if expected is None:
            return False
        origin = _scope_header(scope, b"origin")
        if origin is not None:
            # An explicit Origin is authoritative: a mismatching Origin is
            # rejected even when a Referer would otherwise match.
            return normalize_origin(origin) == expected
        referer = _scope_header(scope, b"referer")
        if referer is not None:
            return normalize_origin(referer) == expected
        return False

    async def _reject_unauthenticated(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("method") == "GET" and _wants_html(scope) and not _is_api_or_mcp(path):
            response = RedirectResponse("/login", status_code=302)
            await response(scope, receive, send)
            return
        await self._send_error(
            scope,
            receive,
            send,
            401,
            "unauthorized",
            "Authentication required.",
            {"WWW-Authenticate": "Bearer"},
        )

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = error_response(status_code, code, message, headers)
        await response(scope, receive, send)

    async def _forward(self, scope: Scope, receive: Receive, send: Send, *, no_store: bool) -> None:
        security = security_headers()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in security.items():
                    headers[key] = value
                if no_store or _message_is_json(message):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = ["UNSAFE_METHODS", "AuthMiddleware", "is_public_path", "normalize_origin"]
