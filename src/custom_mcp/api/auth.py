"""Login, logout and session introspection endpoints.

These handlers delegate all credential handling to the ``auth`` package: they
never touch the shared secret directly, only the authenticator/session services
exposed on ``app.state``.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from custom_mcp.api.errors import error_response
from custom_mcp.auth.base import Authenticator, get_auth_info
from custom_mcp.auth.ratelimit import LoginRateLimiter
from custom_mcp.auth.sessions import SessionService
from custom_mcp.logging import get_logger

logger = get_logger("auth.api")


def _client_ip(request: Request) -> str:
    """Peer IP for rate limiting, ignoring spoofable forwarding headers."""
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


async def _read_secret(request: Request) -> str | None:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        value = payload.get("secret")
        if isinstance(value, str):
            return value
    return None


async def login(request: Request) -> Response:
    """Authenticate with the owner secret and start a session."""
    sessions: SessionService = request.app.state.sessions
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    authenticator: Authenticator = request.app.state.authenticator

    ip = _client_ip(request)
    retry_after = await limiter.retry_after(ip)
    if retry_after is not None:
        return error_response(
            429,
            "rate_limited",
            "Too many login attempts. Try again later.",
            {"Retry-After": str(retry_after)},
        )

    secret = await _read_secret(request)
    if secret is None or not authenticator.verify_login_secret(secret):
        await limiter.record_failure(ip)
        logger.info("login_failed", extra={"extra_fields": {"ip": ip}})
        return error_response(
            401,
            "unauthorized",
            "Invalid credentials.",
            {"WWW-Authenticate": "Bearer"},
        )

    await limiter.reset(ip)
    session_id = await sessions.create()
    response = JSONResponse({"authenticated": True})
    response.headers.append("Set-Cookie", sessions.build_cookie(session_id))
    logger.info("login_succeeded", extra={"extra_fields": {"ip": ip}})
    return response


async def logout(request: Request) -> Response:
    """Revoke the current session and clear the cookie."""
    sessions: SessionService = request.app.state.sessions
    auth_info = get_auth_info(request)
    if auth_info is not None and auth_info.session_id:
        await sessions.delete(auth_info.session_id)
    response = JSONResponse({"authenticated": False})
    response.headers.append("Set-Cookie", sessions.clear_cookie())
    return response


async def session_info(request: Request) -> Response:
    """Report the authentication status of the current request."""
    return JSONResponse({"authenticated": True})


auth_routes = [
    Route("/login", login, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/api/session", session_info, methods=["GET"]),
]


__all__ = ["auth_routes", "login", "logout", "session_info"]
