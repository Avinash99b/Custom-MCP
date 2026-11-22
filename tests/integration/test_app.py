"""Integration tests for routing, authentication, sessions and CSRF."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from custom_mcp.auth.sessions import SESSION_KEY_PREFIX
from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.helpers import TEST_BASE_URL, TEST_SECRET, JsonLogCapture

JSON_HEADERS = {"Accept": "application/json"}
HTML_HEADERS = {"Accept": "text/html"}


class _BrokenRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis is down")


class _SyncKV(Protocol):
    def get(self, name: str) -> str | None: ...


class _OwnedAsyncRedis:
    """App-owned async client double that records whether it was closed."""

    def __init__(
        self,
        *,
        ping_result: bool = True,
        ping_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.ping_result = ping_result
        self.ping_error = ping_error
        self.close_error = close_error
        self.closed = False

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return self.ping_result

    async def smembers(self, name: str) -> set[str]:
        return set()

    async def sadd(self, name: str, *values: str) -> int:
        return 0

    async def srem(self, name: str, *values: str) -> int:
        return 0

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _OwnedSyncRedis:
    """App-owned sync client double that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_client_factories(
    monkeypatch: pytest.MonkeyPatch,
    async_client: _OwnedAsyncRedis,
    sync_client: _OwnedSyncRedis,
) -> None:
    monkeypatch.setattr("custom_mcp.main.create_async_redis", lambda url, **kw: async_client)
    monkeypatch.setattr("custom_mcp.main.create_sync_redis", lambda url, **kw: sync_client)


async def _login(client: httpx.AsyncClient, secret: str = TEST_SECRET) -> httpx.Response:
    return await client.post("/login", json={"secret": secret}, headers=JSON_HEADERS)


# --- health -----------------------------------------------------------------


async def test_healthz_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


async def test_healthz_reports_redis_failure(
    settings: Settings, redis_sync: SyncRedis
) -> None:
    app = create_app(
        settings=settings,
        redis_async=cast("AsyncRedis", _BrokenRedis()),
        redis_sync=redis_sync,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as http:
        response = await http.get("/healthz")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


async def test_lifespan_fails_fast_when_redis_down(
    settings: Settings, redis_sync: SyncRedis
) -> None:
    app = create_app(
        settings=settings,
        redis_async=cast("AsyncRedis", _BrokenRedis()),
        redis_sync=redis_sync,
    )
    with pytest.raises(ConnectionError):
        async with app.router.lifespan_context(app):
            pass


async def test_lifespan_does_not_close_injected_clients(
    app: Starlette, redis_async: AsyncRedis
) -> None:
    async with app.router.lifespan_context(app):
        pass
    assert await redis_async.ping() is True


async def test_lifespan_closes_owned_clients_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_client = _OwnedAsyncRedis(ping_error=ConnectionError("down"))
    sync_client = _OwnedSyncRedis()
    _patch_client_factories(monkeypatch, async_client, sync_client)
    app = create_app(settings=settings)
    with pytest.raises(ConnectionError):
        async with app.router.lifespan_context(app):
            pass
    assert async_client.closed is True
    assert sync_client.closed is True


async def test_lifespan_closes_owned_clients_on_falsy_ping(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_client = _OwnedAsyncRedis(ping_result=False)
    sync_client = _OwnedSyncRedis()
    _patch_client_factories(monkeypatch, async_client, sync_client)
    app = create_app(settings=settings)
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert async_client.closed is True
    assert sync_client.closed is True


async def test_lifespan_closes_owned_clients_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_client = _OwnedAsyncRedis()
    sync_client = _OwnedSyncRedis()
    _patch_client_factories(monkeypatch, async_client, sync_client)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        assert async_client.closed is False
    assert async_client.closed is True
    assert sync_client.closed is True


async def test_sync_close_runs_even_if_async_close_raises(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_client = _OwnedAsyncRedis(close_error=RuntimeError("close boom"))
    sync_client = _OwnedSyncRedis()
    _patch_client_factories(monkeypatch, async_client, sync_client)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        pass
    assert sync_client.closed is True


# --- login ------------------------------------------------------------------


async def test_login_success_sets_hardened_cookie(client: httpx.AsyncClient) -> None:
    response = await _login(client)
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert response.headers["cache-control"] == "no-store"
    assert TEST_SECRET not in response.text

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("session=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "secure" in cookie
    assert "max-age=604800" in cookie


async def test_login_wrong_secret_is_unauthorized(client: httpx.AsyncClient) -> None:
    response = await _login(client, secret="wrong-secret")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    assert "wrong-secret" not in response.text


async def test_login_malformed_body_is_unauthorized(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login", content=b"not-json", headers={**JSON_HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 401


async def test_login_rate_limit_after_five_failures(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        response = await _login(client, secret="wrong")
        assert response.status_code == 401
    blocked = await _login(client, secret="wrong")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) > 0


async def test_login_rate_limit_ignores_spoofed_forwarding_headers(
    client: httpx.AsyncClient,
) -> None:
    for index in range(5):
        response = await client.post(
            "/login",
            json={"secret": "wrong"},
            headers={**JSON_HEADERS, "X-Forwarded-For": f"9.9.9.{index}"},
        )
        assert response.status_code == 401
    blocked = await client.post(
        "/login",
        json={"secret": "wrong"},
        headers={**JSON_HEADERS, "X-Forwarded-For": "8.8.8.8"},
    )
    assert blocked.status_code == 429


async def test_login_success_clears_failure_counter(client: httpx.AsyncClient) -> None:
    for _ in range(4):
        await _login(client, secret="wrong")
    assert (await _login(client)).status_code == 200
    # Counter cleared: five more failures are needed to trip the limit.
    for _ in range(4):
        assert (await _login(client, secret="wrong")).status_code == 401
    assert (await _login(client, secret="wrong")).status_code == 401


async def test_login_secret_not_logged(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await _login(client, secret="super-secret-value")
    assert "super-secret-value" not in caplog.text


# --- sessions ---------------------------------------------------------------


async def test_session_endpoint_reports_authenticated(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.get("/api/session", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


async def test_expired_session_is_rejected(
    client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    await _login(client)
    session_id = client.cookies.get("session")
    assert session_id
    await redis_async.delete(f"{SESSION_KEY_PREFIX}{session_id}")
    response = await client.get("/api/session", headers=JSON_HEADERS)
    assert response.status_code == 401


async def test_logout_revokes_session_and_clears_cookie(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Origin": TEST_BASE_URL})
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    after = await client.get("/api/session", headers=JSON_HEADERS)
    assert after.status_code == 401


# --- unauthenticated behaviour ---------------------------------------------


async def test_unauthenticated_api_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/session", headers=JSON_HEADERS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"


async def test_browser_get_redirects_to_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/", headers=HTML_HEADERS)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_non_browser_root_returns_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/", headers=JSON_HEADERS)
    assert response.status_code == 401


async def test_login_page_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# --- MCP bearer-only --------------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp/demo", "/mcp/demo/"])
async def test_mcp_rejects_cookie_only(client: httpx.AsyncClient, path: str) -> None:
    await _login(client)
    response = await client.get(path, headers=JSON_HEADERS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_mcp_accepts_valid_bearer(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/mcp/demo",
        headers={**JSON_HEADERS, "Authorization": f"Bearer {TEST_SECRET}"},
    )
    # Phase 0 has no MCP handler, but bearer auth passed the middleware.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_oauth_callback_is_public(client: httpx.AsyncClient) -> None:
    response = await client.get("/mcp/demo/oauth/callback")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- CSRF -------------------------------------------------------------------


async def test_cookie_unsafe_request_without_origin_is_forbidden(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post("/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_cookie_unsafe_request_wrong_origin_is_forbidden(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


async def test_cookie_unsafe_request_correct_origin_allowed(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Origin": TEST_BASE_URL})
    assert response.status_code == 200


async def test_cookie_unsafe_request_correct_referer_allowed(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Referer": f"{TEST_BASE_URL}/dashboard"})
    assert response.status_code == 200


async def test_mismatching_origin_rejected_even_with_valid_referer(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post(
        "/logout",
        headers={
            "Origin": "https://evil.example.com",
            "Referer": f"{TEST_BASE_URL}/dashboard",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_null_origin_is_rejected(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Origin": "null"})
    assert response.status_code == 403


async def test_malformed_referer_is_rejected(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.post("/logout", headers={"Referer": "https://[::1/dashboard"})
    assert response.status_code == 403


async def test_default_port_origin_is_equivalent(client: httpx.AsyncClient) -> None:
    await _login(client)
    response = await client.post(
        "/logout", headers={"Origin": f"{TEST_BASE_URL}:443"}
    )
    assert response.status_code == 200


async def test_referer_fallback_only_used_without_origin(
    client: httpx.AsyncClient,
) -> None:
    await _login(client)
    response = await client.post(
        "/logout", headers={"Referer": "https://evil.example.com/dashboard"}
    )
    assert response.status_code == 403


async def test_bearer_skips_csrf(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/logout", headers={"Authorization": f"Bearer {TEST_SECRET}"}
    )
    assert response.status_code == 200


# --- error handling ---------------------------------------------------------


async def _boom(request: Request) -> Response:
    raise RuntimeError("boom-internal-detail")


async def test_unhandled_exception_is_generic_500(app: Starlette) -> None:
    app.router.routes.append(Route("/boom", _boom))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as http:
        response = await http.get(
            "/boom", headers={"Authorization": f"Bearer {TEST_SECRET}"}
        )
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal server error occurred."}
    }
    assert "boom-internal-detail" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


async def test_unknown_route_uses_error_shape(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/no-such-route", headers={"Authorization": f"Bearer {TEST_SECRET}"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- headers ----------------------------------------------------------------


async def test_security_and_security_headers_on_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        response.headers["content-security-policy"]
        == "default-src 'self'; frame-ancestors 'none'"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-request-id"]


async def test_static_assets_are_public_with_csp(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/styles.css")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_login_and_static_html_have_no_inline_script(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")
    assert "<script src=" in response.text
    assert "<script>" not in response.text


async def test_sync_and_async_fake_clients_share_server(
    redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    await redis_async.set("shared-key", "shared-value")
    sync_kv = cast("_SyncKV", redis_sync)
    assert sync_kv.get("shared-key") == "shared-value"


# --- log correlation --------------------------------------------------------


async def test_request_summary_log_correlates_with_header(
    client: httpx.AsyncClient, json_logs: JsonLogCapture
) -> None:
    response = await client.get("/healthz")
    request_id = response.headers["x-request-id"]

    summaries = json_logs.with_message("request")
    assert summaries
    summary = summaries[-1]
    assert summary["request_id"] == request_id
    assert summary["status"] == 200
    assert summary["path"] == "/healthz"


async def test_login_failure_log_correlates_with_header(
    client: httpx.AsyncClient, json_logs: JsonLogCapture
) -> None:
    response = await _login(client, secret="wrong")
    request_id = response.headers["x-request-id"]

    failures = json_logs.with_message("login_failed")
    assert failures
    assert failures[-1]["request_id"] == request_id
    assert json_logs.with_message("request")[-1]["status"] == 401


async def test_generic_500_logs_correlate_with_header(
    app: Starlette, json_logs: JsonLogCapture
) -> None:
    app.router.routes.append(Route("/boom", _boom))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as http:
        response = await http.get(
            "/boom", headers={"Authorization": f"Bearer {TEST_SECRET}"}
        )
    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    assert request_id

    summaries = json_logs.with_message("request")
    assert summaries
    assert summaries[-1]["status"] == 500
    assert summaries[-1]["request_id"] == request_id

    diagnostics = json_logs.with_message("unhandled_exception")
    assert diagnostics
    diagnostic = diagnostics[-1]
    assert diagnostic["request_id"] == request_id
    assert diagnostic["path"] == "/boom"
    assert "exception" in diagnostic


async def test_concurrent_requests_have_distinct_request_ids(
    client: httpx.AsyncClient, json_logs: JsonLogCapture
) -> None:
    first, second = await asyncio.gather(client.get("/healthz"), client.get("/healthz"))
    ids = {first.headers["x-request-id"], second.headers["x-request-id"]}
    assert len(ids) == 2

    summaries = json_logs.with_message("request")
    assert {summary["request_id"] for summary in summaries} == ids
    assert "-" not in {summary["request_id"] for summary in summaries}
