"""End-to-end OAuth 2.0 + PKCE integration tests (Phase 8, slice 8c).

These drive the assembled app (fake Redis, real host/loader/manager, the real
public callback route and the real health scheduler) against the loopback mock
provider in ``tests/oauth_provider.py``. The example plugin is installed through
the real multipart import endpoint, so the authorize request, the PKCE exchange
and the refresh rotation are exercised over genuine synchronous HTTP.

Determinism: every wait is a real event (a completed request, a drained health
cycle) or a bounded poll on persisted state; no arbitrary sleeps are used. Each
test installs its own plugin in a fresh fake-Redis/app pair and the provider
fixture shuts the loopback server down afterwards.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
import pytest_asyncio
import requests
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette

from custom_mcp.storage.redis_client import AsyncRedis
from mcp import ClientSession
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_hello_zip, build_plugin_zip
from tests.oauth_provider import MockOAuthProvider, s256

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}

SLUG = "oauth-demo"
OTHER_SLUG = "hello"
CALLBACK_PATH = f"/mcp/{SLUG}/oauth/callback"
CALLBACK_URL = f"{TEST_BASE_URL}{CALLBACK_PATH}"
META_KEY = f"core:plugin:{SLUG}:meta"
STATE_PREFIX = "core:oauth_state:"
PROVIDER_URL_ENV = "CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL"
CLIENT_SECRET = "oauth-demo-client-secret-value"

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "plugin_examples" / "oauth_demo"


def _oauth_zip() -> bytes:
    """Build the real ``oauth_demo`` example as an installable package."""
    source = (EXAMPLE_DIR / "plugin.py").read_text(encoding="utf-8")
    return build_plugin_zip(
        slug=SLUG,
        entry="plugin:OAuthDemoPlugin",
        auth_modes=("oauth",),
        plugin_source=source,
        manifest_extra={"min_core_version": "0.1.0"},
    )


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=TEST_BASE_URL,
            timeout=httpx.Timeout(30.0),
        ) as client:
            yield client


@pytest_asyncio.fixture
async def plugin_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with _running(app) as client:
        yield client


@pytest.fixture
def provider() -> Iterator[MockOAuthProvider]:
    """A started loopback OAuth provider, shut down after the test."""
    with MockOAuthProvider() as server:
        yield server


@pytest.fixture
def oauth_provider(
    provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> MockOAuthProvider:
    """Point the example plugin at the loopback provider before any hook runs."""
    monkeypatch.setenv(PROVIDER_URL_ENV, provider.base_url)
    return provider


# --- helpers ----------------------------------------------------------------


def _client_factory(transport: httpx.ASGITransport) -> Any:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "transport": transport,
            "base_url": TEST_BASE_URL,
            "timeout": httpx.Timeout(30.0),
        }
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


@asynccontextmanager
async def _wire_session(
    app: Starlette, slug: str = SLUG
) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    url = f"{TEST_BASE_URL}/mcp/{slug}/"
    async with streamablehttp_client(
        url, headers=AUTH, httpx_client_factory=_client_factory(transport)
    ) as (read, write, _), ClientSession(read, write) as session:
        yield session


async def _wire_tool_names(app: Starlette, slug: str = SLUG) -> set[str]:
    async with _wire_session(app, slug) as session:
        await session.initialize()
        return {tool.name for tool in (await session.list_tools()).tools}


async def _import_oauth(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", _oauth_zip(), "application/zip")},
        headers=AUTH,
    )


async def _import_hello(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("hello.zip", build_hello_zip(), "application/zip")},
        headers=AUTH,
    )


async def _connect(
    client: httpx.AsyncClient,
    *,
    client_id: str | None,
    client_secret: str | None = None,
    slug: str = SLUG,
) -> httpx.Response:
    credentials: dict[str, Any] = {}
    if client_id is not None:
        credentials["client_id"] = client_id
    if client_secret is not None:
        credentials["client_secret"] = client_secret
    return await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "oauth", "credentials": credentials},
        headers=AUTH,
    )


async def _detail(client: httpx.AsyncClient, slug: str = SLUG) -> dict[str, Any]:
    response = await client.get(f"/api/plugins/{slug}", headers=AUTH)
    assert response.status_code == 200
    return response.json()


async def _authorize(redirect_url: str) -> requests.Response:
    """Fetch the provider authorize URL without following its 302."""
    return await asyncio.to_thread(
        requests.get, redirect_url, allow_redirects=False, timeout=5.0
    )


async def _callback(
    client: httpx.AsyncClient,
    *,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> httpx.Response:
    params: dict[str, str] = {}
    if code is not None:
        params["code"] = code
    if state is not None:
        params["state"] = state
    if error is not None:
        params["error"] = error
    return await client.get(CALLBACK_PATH, params=params, follow_redirects=False)


def _params(location: str) -> dict[str, str]:
    parsed = urlsplit(location)
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


def _state_of(url: str) -> str:
    return _params(url)["state"]


async def _start_oauth(
    client: httpx.AsyncClient, provider: MockOAuthProvider
) -> str:
    response = await _connect(client, client_id=provider.client_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "oauth_required"
    redirect_url = body["redirect_url"]
    assert isinstance(redirect_url, str) and redirect_url
    return redirect_url


async def _meta(redis_async: AsyncRedis, slug: str = SLUG) -> dict[str, Any]:
    raw = await redis_async.get(f"core:plugin:{slug}:meta")
    return json.loads(raw or "{}")


async def _stored(
    redis_async: AsyncRedis, namespace: str, key: str, slug: str = SLUG
) -> Any:
    raw = await redis_async.get(f"plugin:{slug}:{namespace}:{key}")
    return json.loads(raw) if raw is not None else None


# --- 1. full PKCE happy path ------------------------------------------------


async def test_full_pkce_happy_path_connects_lists_me_and_leaks_nothing(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201

    before = await _detail(plugin_client)
    assert before["status"] == "not_connected"
    assert before["auth_mode"] is None
    assert [tool["name"] for tool in before["tools"]] == ["whoami"]

    started = await _connect(
        plugin_client,
        client_id=provider.client_id,
        client_secret=CLIENT_SECRET,
    )
    assert started.status_code == 200
    body = started.json()
    assert body["status"] == "oauth_required"
    assert body["auth_mode"] == "oauth"
    redirect_url = body["redirect_url"]
    assert redirect_url.startswith(provider.base_url + "/authorize")
    state = _state_of(redirect_url)
    assert await redis_async.get(f"{STATE_PREFIX}{state}") is not None

    # The plugin stored the PKCE verifier under data ``pkce:{state}`` (TTL 600).
    verifier = await _stored(redis_async, "data", f"pkce:{state}")
    assert isinstance(verifier, str) and 43 <= len(verifier) <= 128
    assert await redis_async.ttl(f"plugin:{SLUG}:data:pkce:{state}") in range(1, 601)

    # Fetch the sign-in URL from the real provider and follow its 302 by hand.
    authorize = await _authorize(redirect_url)
    assert authorize.status_code == 302
    recorded = provider.requests_for("/authorize")
    assert len(recorded) == 1
    query = recorded[0].query
    assert query["response_type"] == "code"
    assert query["client_id"] == provider.client_id
    assert query["redirect_uri"] == CALLBACK_URL
    assert query["state"] == state
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == s256(verifier)
    # The redirect URI is quoted in the raw authorize URL.
    assert f"redirect_uri={quote(CALLBACK_URL, safe='')}" in urlsplit(redirect_url).query

    params = _params(authorize.headers["Location"])
    assert params["state"] == state
    code = params["code"]

    callback = await _callback(plugin_client, code=code, state=state)
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/#/plugins/{SLUG}?oauth=success"

    after = await _detail(plugin_client)
    assert after["status"] == "connected"
    assert after["auth_mode"] == "oauth"
    assert {tool["name"] for tool in after["tools"]} == {"whoami", "me"}

    token_requests = provider.requests_for("/token")
    assert len(token_requests) == 1
    form = token_requests[0].form
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == code
    assert form["redirect_uri"] == CALLBACK_URL
    assert form["code_verifier"] == verifier
    assert s256(form["code_verifier"]) == query["code_challenge"]

    # The single-use state and the PKCE verifier are both consumed.
    assert await redis_async.get(f"{STATE_PREFIX}{state}") is None
    assert await redis_async.get(f"plugin:{SLUG}:data:pkce:{state}") is None

    # The live MCP wire now lists the token-gated ``me`` tool.
    assert await _wire_tool_names(app) == {"whoami", "me"}

    access = await _stored(redis_async, "secret", "oauth_access_token")
    refresh = await _stored(redis_async, "secret", "oauth_refresh_token")
    assert access in provider.active_access_tokens
    assert refresh in provider.active_refresh_tokens

    logs = await app.state.plugin_manager.logs(SLUG)
    listing = (await plugin_client.get("/api/plugins", headers=AUTH)).json()
    rendered = json.dumps(
        {"connect": body, "detail": after, "logs": logs, "list": listing}
    )
    for secret in (CLIENT_SECRET, code, verifier, access, refresh):
        assert isinstance(secret, str) and secret
        assert secret not in rendered
        assert secret not in callback.headers["location"]
    assert state not in callback.headers["location"]


# --- 2. state single use ----------------------------------------------------


async def test_callback_state_is_single_use(
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201
    redirect_url = await _start_oauth(plugin_client, provider)
    state = _state_of(redirect_url)
    authorize = await _authorize(redirect_url)
    params = _params(authorize.headers["Location"])

    first = await _callback(plugin_client, code=params["code"], state=state)
    assert first.headers["location"] == f"/#/plugins/{SLUG}?oauth=success"
    connected = await _meta(redis_async)
    assert connected["status"] == "connected"
    assert connected["auth_mode"] == "oauth"
    assert len(provider.requests_for("/token")) == 1

    # Replaying the exact callback cannot reuse the consumed state.
    replay = await _callback(plugin_client, code=params["code"], state=state)
    assert replay.status_code == 303
    assert replay.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert await _meta(redis_async) == connected
    assert len(provider.requests_for("/token")) == 1


# --- 3. forged / unknown state ----------------------------------------------


async def test_forged_state_never_calls_the_provider_or_mutates_status(
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201
    await _start_oauth(plugin_client, provider)
    before = await _meta(redis_async)
    assert before["status"] == "oauth_required"

    forged = await _callback(
        plugin_client, code="forged-code", state="never-issued-state"
    )
    assert forged.status_code == 303
    assert forged.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert await _meta(redis_async) == before
    assert provider.requests_for("/token") == []


# --- 4. slug binding --------------------------------------------------------


async def test_state_is_bound_to_the_callback_slug(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
) -> None:
    assert (await _import_oauth(plugin_client)).status_code == 201
    assert (await _import_hello(plugin_client)).status_code == 201
    assert (await _meta(redis_async, OTHER_SLUG))["status"] == "connected"

    # A state minted for ``oauth-demo`` ...
    manager = app.state.plugin_manager
    state = await manager.create_oauth_state(SLUG)
    assert await redis_async.get(f"{STATE_PREFIX}{state}") is not None

    # ... presented to the *other* plugin's callback cannot connect it.
    response = await plugin_client.get(
        f"/mcp/{OTHER_SLUG}/oauth/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/#/plugins/{OTHER_SLUG}?oauth=failed"

    # The mismatch is still single-use (the state is spent) and neither slug's
    # stored status changed.
    assert await redis_async.get(f"{STATE_PREFIX}{state}") is None
    assert (await _meta(redis_async, SLUG))["status"] == "not_connected"
    assert (await _meta(redis_async, OTHER_SLUG))["status"] == "connected"


# --- 5. expiry --------------------------------------------------------------


async def test_expired_or_deleted_state_fails_without_mutation(
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201
    redirect_url = await _start_oauth(plugin_client, provider)
    state = _state_of(redirect_url)
    before = await _meta(redis_async)
    assert before["status"] == "oauth_required"

    # Simulate the EX 600 TTL elapsing by removing the state key outright.
    assert await redis_async.delete(f"{STATE_PREFIX}{state}") == 1

    response = await _callback(plugin_client, code="any-code", state=state)
    assert response.status_code == 303
    assert response.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert await _meta(redis_async) == before
    assert provider.requests_for("/token") == []


# --- 6. provider error requires a valid state -------------------------------


async def test_provider_error_requires_a_valid_state(
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    provider.force_authorize_error = True
    assert (await _import_oauth(plugin_client)).status_code == 201
    redirect_url = await _start_oauth(plugin_client, provider)
    before = await _meta(redis_async)
    assert before["status"] == "oauth_required"

    authorize = await _authorize(redirect_url)
    assert authorize.status_code == 302
    params = _params(authorize.headers["Location"])
    assert params["error"] == "access_denied"
    assert params["state"] == _state_of(redirect_url)

    # A real provider error with the matching valid state: the state is
    # consumed, the redirect is the fixed failed target, and ``on_oauth_result``
    # is never invoked (no token exchange).
    valid = await _callback(
        plugin_client, error=params["error"], state=params["state"]
    )
    assert valid.status_code == 303
    assert valid.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert "access_denied" not in valid.headers["location"]
    assert await _meta(redis_async) == before
    assert provider.requests_for("/token") == []
    assert await redis_async.get(f"{STATE_PREFIX}{params['state']}") is None

    # The same error with a forged state also fails without mutation.
    forged = await _callback(
        plugin_client, error="access_denied", state="never-issued-state"
    )
    assert forged.status_code == 303
    assert forged.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert await _meta(redis_async) == before
    assert provider.requests_for("/token") == []


# --- 7. fixed relative redirect ---------------------------------------------


async def test_redirect_target_is_always_the_fixed_relative_path(
    plugin_client: httpx.AsyncClient,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201
    redirect_url = await _start_oauth(plugin_client, provider)
    authorize = await _authorize(redirect_url)
    params = _params(authorize.headers["Location"])

    success = await _callback(plugin_client, code=params["code"], state=params["state"])
    success_location = success.headers["location"]
    assert success_location == f"/#/plugins/{SLUG}?oauth=success"
    assert success_location.startswith("/#/")
    assert "://" not in success_location

    failed = await _callback(plugin_client, code="c", state="forged")
    assert failed.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
    assert failed.headers["location"].startswith("/#/")
    assert "://" not in failed.headers["location"]


async def test_unknown_slug_callback_is_404(plugin_client: httpx.AsyncClient) -> None:
    response = await plugin_client.get(
        "/mcp/ghost/oauth/callback",
        params={"code": "c", "state": "s"},
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- 8. refresh rotation via the health scheduler ---------------------------


async def test_health_scheduler_refresh_rotates_tokens(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201
    redirect_url = await _start_oauth(plugin_client, provider)
    state = _state_of(redirect_url)
    authorize = await _authorize(redirect_url)
    params = _params(authorize.headers["Location"])
    done = await _callback(plugin_client, code=params["code"], state=state)
    assert done.headers["location"] == f"/#/plugins/{SLUG}?oauth=success"

    refresh_before = await _stored(redis_async, "secret", "oauth_refresh_token")
    access_before = await _stored(redis_async, "secret", "oauth_access_token")
    assert refresh_before in provider.active_refresh_tokens
    assert len(provider.requests_for("/token")) == 1

    # Make the access token stale so the next ping observes ``auth_expired`` and
    # drives ``refresh`` under the scheduler's lifecycle lock.
    await redis_async.set(
        f"plugin:{SLUG}:secret:oauth_expires_at", json.dumps(time.time() - 10)
    )

    await app.state.health.run_cycle()

    health = await app.state.plugin_manager.read_health(SLUG)
    assert health["status"] == "connected"

    token_requests = provider.requests_for("/token")
    assert len(token_requests) == 2
    assert token_requests[0].form["grant_type"] == "authorization_code"
    assert token_requests[1].form["grant_type"] == "refresh_token"
    assert token_requests[1].form["refresh_token"] == refresh_before

    refresh_after = await _stored(redis_async, "secret", "oauth_refresh_token")
    access_after = await _stored(redis_async, "secret", "oauth_access_token")
    assert refresh_after != refresh_before
    assert access_after != access_before
    assert refresh_after in provider.active_refresh_tokens
    assert refresh_before not in provider.active_refresh_tokens
    assert (await _detail(plugin_client))["status"] == "connected"


# --- 9. stored client credential reuse --------------------------------------


async def test_connect_reuses_stored_client_id_and_requires_one_otherwise(
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    oauth_provider: MockOAuthProvider,
) -> None:
    provider = oauth_provider
    assert (await _import_oauth(plugin_client)).status_code == 201

    # Nothing stored and nothing supplied: rejected before any state change.
    rejected = await _connect(plugin_client, client_id=None)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_request"
    assert await redis_async.get(
        f"plugin:{SLUG}:secret:core.oauth_client_id"
    ) is None
    assert [key async for key in redis_async.scan_iter(match=f"{STATE_PREFIX}*")] == []

    # A first connect stores the client id.
    first = await _connect(plugin_client, client_id=provider.client_id)
    assert first.status_code == 200
    assert first.json()["status"] == "oauth_required"
    assert await _stored(redis_async, "secret", "core.oauth_client_id") == provider.client_id

    # A second connect omits the client id: the stored one is reused and the
    # provider accepts the authorize request built from it.
    second = await _connect(plugin_client, client_id=None)
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "oauth_required"
    authorize = await _authorize(body["redirect_url"])
    assert authorize.status_code == 302
    recorded = provider.requests_for("/authorize")
    assert len(recorded) == 1
    assert recorded[-1].query["client_id"] == provider.client_id
