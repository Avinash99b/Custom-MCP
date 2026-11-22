"""Unit tests for the Phase 8a OAuth backend (state, connect and callback).

Covers the store-only state helpers plus real-host connect/complete flows with a
fake-Redis pair, and the public callback route driven directly. Plugin sources
are inline (the ``oauth_demo`` example is owned by another slice).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from starlette.requests import Request

from custom_mcp.api.oauth import oauth_callback
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.loader import PluginLoader
from custom_mcp.plugins.logs import PluginLogs
from custom_mcp.plugins.manager import PluginConnectError, PluginManager
from custom_mcp.storage.manager import StorageManager
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.helpers import TEST_BASE_URL, build_plugin_zip

SLUG = "oauthdemo"
CLIENT_ID = "client-id-value-1234"
CLIENT_SECRET = "client-secret-value-5678"

_OAUTH_PLUGIN = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

CALLS = []
CACHED_SECRET = [None]
REDIRECT_URL = [None]
SIGN_IN_URL = [None]
RESULT_STATUS = ["connected"]


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.OAUTH]

    def list_tools(self):
        cached = CACHED_SECRET[0]
        description = "uses " + cached if isinstance(cached, str) and cached else "whoami"
        return [
            Tool(
                name="whoami",
                description=description,
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        CALLS.append(("connect", self.ctx.secrets.get("core.oauth_client_id")))
        redirect = REDIRECT_URL[0]
        if redirect is not None:
            return ConnectResult(status=Status.OAUTH_REQUIRED, redirect_url=redirect)
        return ConnectResult(status=Status.OAUTH_REQUIRED)

    def get_oauth_sign_in_url(self, state):
        override = SIGN_IN_URL[0]
        if override is not None:
            return override
        return (
            "https://provider.example.com/authorize?client_id=cid&state=" + state
        )

    def on_oauth_credentials_set(self, client_id, client_secret):
        CALLS.append(
            ("creds", client_id, client_secret, self.ctx.secrets.get("core.oauth_client_secret"))
        )
        CACHED_SECRET[0] = client_secret

    def on_oauth_result(self, code, state):
        if code == "boom":
            raise RuntimeError("token exchange failed for " + CLIENT_SECRET)
        self.ctx.secrets.set("oauth_access_token", "token-" + code)
        if RESULT_STATUS[0] == "connected":
            return ConnectResult(status=Status.CONNECTED)
        return ConnectResult(status=Status.ERROR)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({"ok": True})
'''


def _store_manager(redis: AsyncRedis, tmp_path: Path) -> PluginManager:
    """A store-only manager: enough for the state create/consume helpers."""
    return PluginManager(
        redis,
        data_dir=tmp_path / "data",
        max_plugin_zip_bytes=2 * 1024 * 1024,
        base_url=TEST_BASE_URL,
    )


@asynccontextmanager
async def _full_manager(
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    tmp_path: Path,
    *,
    source: str = _OAUTH_PLUGIN,
    slug: str = SLUG,
) -> AsyncIterator[PluginManager]:
    """A loaded manager backed by real host/loader/storage/logs and fake Redis."""
    host = PluginHost(
        worker_threads=2,
        per_plugin_concurrency=2,
        call_timeout=5.0,
        tool_timeout=5.0,
        shutdown_timeout=1.0,
    )
    logs = PluginLogs(redis_sync, redis_async)
    storage = StorageManager(redis_sync, redis_async)
    loader = PluginLoader(host, load_timeout=5.0, sanitize_error=logs.redact, log=logs.write)
    manager = PluginManager(
        redis_async,
        data_dir=tmp_path / "data",
        max_plugin_zip_bytes=2 * 1024 * 1024,
        base_url=TEST_BASE_URL,
        host=host,
        loader=loader,
        storage=storage,
        logs=logs,
    )
    try:
        await manager.startup()
        await manager.install(
            build_plugin_zip(slug=slug, auth_modes=("oauth",), plugin_source=source)
        )
        yield manager
    finally:
        await manager.shutdown_all()
        await host.aclose()


def _module(slug: str = SLUG) -> Any:
    import sys

    return sys.modules[f"mcp_plugin_{slug}.plugin"]


def _state_from_url(url: str) -> str:
    params = dict(parse_qsl(urlsplit(url).query))
    return params["state"]


def _state_key(state: str) -> str:
    return f"core:oauth_state:{state}"


def _callback_request(manager: PluginManager, slug: str, query: str) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(plugin_manager=manager))
    path = f"/mcp/{slug}/oauth/callback"
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [],
        "server": ("mcp.example.com", 443),
        "client": ("127.0.0.1", 12345),
        "path_params": {"slug": slug},
        "app": app,
        "state": {},
    }
    return Request(scope)


async def _meta(manager: PluginManager, slug: str = SLUG) -> dict[str, Any]:
    return await manager.read_health(slug)


# --- state helpers (store-only) ---------------------------------------------


async def test_create_oauth_state_stores_slug_and_expiry(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _store_manager(redis_async, tmp_path)
    state = await manager.create_oauth_state("demo")

    raw = await redis_async.get(_state_key(state))
    assert raw is not None
    payload = json.loads(raw)
    assert payload["slug"] == "demo"
    assert isinstance(payload["created_at"], str) and payload["created_at"]
    assert set(payload) == {"slug", "created_at"}

    ttl = await redis_async.ttl(_state_key(state))
    assert 0 < ttl <= 600


async def test_consume_oauth_state_is_single_use(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _store_manager(redis_async, tmp_path)
    state = await manager.create_oauth_state("demo")

    assert await manager.consume_oauth_state(state, "demo") is True
    # GETDEL removed the key; a replay cannot succeed.
    assert await redis_async.get(_state_key(state)) is None
    assert await manager.consume_oauth_state(state, "demo") is False


async def test_consume_oauth_state_rejects_slug_mismatch(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _store_manager(redis_async, tmp_path)
    state = await manager.create_oauth_state("demo")

    assert await manager.consume_oauth_state(state, "other") is False
    # A mismatched consume still spends the token (single-use, no replay).
    assert await redis_async.get(_state_key(state)) is None


@pytest.mark.parametrize("state", ["", "unknown-state"])
async def test_consume_oauth_state_rejects_unknown_and_empty(
    redis_async: AsyncRedis, tmp_path: Path, state: str
) -> None:
    manager = _store_manager(redis_async, tmp_path)
    assert await manager.consume_oauth_state(state, "demo") is False


# --- oauth connect (real host) ----------------------------------------------


async def test_connect_oauth_sign_in_url_stores_credentials(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        detail = await manager.connect(
            SLUG,
            mode="oauth",
            credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

        assert detail["status"] == "oauth_required"
        assert detail["auth_mode"] == "oauth"
        redirect = detail["redirect_url"]
        assert redirect.startswith("https://provider.example.com/authorize")
        state = _state_from_url(redirect)
        assert await redis_async.get(_state_key(state)) is not None

        # Credentials are stored before the hook (exact, untrimmed).
        assert await redis_async.get(
            f"plugin:{SLUG}:secret:core.oauth_client_id"
        ) == json.dumps(CLIENT_ID)
        assert await redis_async.get(
            f"plugin:{SLUG}:secret:core.oauth_client_secret"
        ) == json.dumps(CLIENT_SECRET)

        module = _module()
        assert ("creds", CLIENT_ID, CLIENT_SECRET, CLIENT_SECRET) in module.CALLS

        # No secret leaks into the response, and the description is redacted.
        rendered = json.dumps(detail)
        assert CLIENT_SECRET not in rendered
        assert detail["tools"][0]["description"] == "uses ***"


async def test_connect_oauth_reuses_stored_client_id(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        first = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        assert first["status"] == "oauth_required"

        second = await manager.connect(SLUG, mode="oauth", credentials={})
        assert second["status"] == "oauth_required"
        assert "redirect_url" in second
        # The second connect reused the stored client id (no secret resubmitted).
        module = _module()
        creds_entries = [entry for entry in module.CALLS if entry[0] == "creds"]
        assert creds_entries[-1] == ("creds", CLIENT_ID, None, None)


@pytest.mark.parametrize("credentials", [{}, {"client_id": None}, {"client_id": 5}])
async def test_connect_oauth_requires_client_id(
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    tmp_path: Path,
    credentials: dict[str, Any],
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        with pytest.raises(PluginConnectError) as excinfo:
            await manager.connect(SLUG, mode="oauth", credentials=credentials)
        assert excinfo.value.code == "invalid_request"
        assert excinfo.value.message == "A client id is required."
        # Nothing stored and no state created.
        assert await redis_async.get(f"plugin:{SLUG}:secret:core.oauth_client_id") is None
        keys = [key async for key in redis_async.scan_iter(match="core:oauth_state:*")]
        assert keys == []


async def test_connect_oauth_inserts_state_into_redirect_url(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        _module().REDIRECT_URL[0] = (
            "https://provider.example.com/cb?code=abc&state=old#frag"
        )
        detail = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )

        assert detail["status"] == "oauth_required"
        redirect = detail["redirect_url"]
        params = parse_qsl(urlsplit(redirect).query)
        names = [name for name, _ in params]
        # ``state`` is replaced, not duplicated, and the fragment survives.
        assert names.count("state") == 1
        assert dict(params)["code"] == "abc"
        assert dict(params)["state"] != "old"
        assert urlsplit(redirect).fragment == "frag"
        state = dict(params)["state"]
        assert await redis_async.get(_state_key(state)) is not None


@pytest.mark.parametrize(
    "hostile",
    ["javascript:alert(1)", "//evil.example.com/cb", "not a url", "ftp://x/y"],
)
async def test_connect_oauth_rejects_hostile_redirect_url(
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    tmp_path: Path,
    hostile: str,
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        _module().REDIRECT_URL[0] = hostile
        detail = await manager.connect(
            SLUG,
            mode="oauth",
            credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

        # Never surface a hostile redirect: the connect fails safely instead.
        assert detail["status"] == "error"
        assert "redirect_url" not in detail
        rendered = json.dumps(detail)
        assert hostile not in rendered
        assert CLIENT_SECRET not in rendered


async def test_connect_oauth_rejects_non_http_sign_in_url(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        _module().SIGN_IN_URL[0] = "ftp://provider.example.com/authorize"
        detail = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        assert detail["status"] == "error"
        assert "redirect_url" not in detail
        assert "ftp://" not in json.dumps(detail)


async def test_connect_oauth_secret_retained_for_description_redaction(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        await manager.connect(
            SLUG,
            mode="oauth",
            credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        runtime = manager.runtime_for(SLUG)
        assert runtime is not None
        assert CLIENT_SECRET in runtime.retained_secrets

        # ``disconnect`` wipes the stored secret but the plugin cached it in
        # memory; the post-wipe description must still be redacted.
        detail = await manager.disconnect(SLUG)
        assert await redis_async.get(
            f"plugin:{SLUG}:secret:core.oauth_client_secret"
        ) is None
        assert CLIENT_SECRET not in json.dumps(detail)
        assert detail["tools"][0]["description"] == "uses ***"


# --- complete_oauth (real host) ---------------------------------------------


async def test_complete_oauth_connects_and_consumes_state(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        started = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        state = _state_from_url(started["redirect_url"])

        detail = await manager.complete_oauth(SLUG, code="good", state=state)
        assert detail["status"] == "connected"
        assert detail["auth_mode"] == "oauth"
        # The state was consumed by the completion.
        assert await redis_async.get(_state_key(state)) is None

        # A reused state cannot mutate status again.
        with pytest.raises(PluginConnectError):
            await manager.complete_oauth(SLUG, code="good", state=state)
        assert (await _meta(manager))["status"] == "connected"


async def test_complete_oauth_forged_state_leaves_status_unchanged(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        await manager.connect(SLUG, mode="oauth", credentials={"client_id": CLIENT_ID})
        before = await _meta(manager)
        assert before["status"] == "oauth_required"

        with pytest.raises(PluginConnectError) as excinfo:
            await manager.complete_oauth(SLUG, code="good", state="forged")
        assert excinfo.value.code == "invalid_request"

        after = await _meta(manager)
        assert after["status"] == "oauth_required"
        assert after["auth_mode"] == "oauth"


async def test_complete_oauth_plugin_failure_sets_safe_error(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        started = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        state = _state_from_url(started["redirect_url"])

        detail = await manager.complete_oauth(SLUG, code="boom", state=state)
        assert detail["status"] == "error"
        rendered = json.dumps(detail)
        # Plugin exception text (and the secret it embedded) never surface.
        assert "token exchange failed" not in rendered
        assert CLIENT_SECRET not in rendered


# --- callback route ---------------------------------------------------------


async def test_callback_invalid_slug_is_404(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        response = await oauth_callback(_callback_request(manager, "Bad", "state=x"))
        assert response.status_code == 404
        body = json.loads(bytes(response.body))
        assert body["error"]["code"] == "not_found"


async def test_callback_unknown_slug_is_404(redis_async: AsyncRedis, tmp_path: Path) -> None:
    # A valid-format but unknown slug has no dashboard entry to redirect to, so
    # the route mirrors every other slug-bound plugin route and returns 404.
    manager = _store_manager(redis_async, tmp_path)
    response = await oauth_callback(_callback_request(manager, "ghost", "state=x"))
    assert response.status_code == 404
    body = json.loads(bytes(response.body))
    assert body["error"]["code"] == "not_found"


async def test_callback_success_redirects_to_fixed_relative_url(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        started = await manager.connect(
            SLUG,
            mode="oauth",
            credentials={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        state = _state_from_url(started["redirect_url"])

        response = await oauth_callback(
            _callback_request(manager, SLUG, f"code=good&state={state}")
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/#/plugins/{SLUG}?oauth=success"
        # No secret or state token in the redirect target.
        assert CLIENT_SECRET not in response.headers["location"]
        assert state not in response.headers["location"]
        assert (await _meta(manager))["status"] == "connected"


async def test_callback_missing_state_redirects_failed(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        await manager.connect(SLUG, mode="oauth", credentials={"client_id": CLIENT_ID})
        response = await oauth_callback(_callback_request(manager, SLUG, "code=good"))
        assert response.status_code == 303
        assert response.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
        assert (await _meta(manager))["status"] == "oauth_required"


async def test_callback_forged_state_never_changes_status(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        await manager.connect(SLUG, mode="oauth", credentials={"client_id": CLIENT_ID})
        response = await oauth_callback(
            _callback_request(manager, SLUG, "code=good&state=forged")
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
        assert (await _meta(manager))["status"] == "oauth_required"


async def test_callback_provider_error_requires_valid_state(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        started = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        state = _state_from_url(started["redirect_url"])

        # A forged state with a provider error still fails and cannot mutate.
        forged = await oauth_callback(
            _callback_request(
                manager, SLUG, "error=access_denied&state=forged-state"
            )
        )
        assert forged.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
        assert (await _meta(manager))["status"] == "oauth_required"

        # A valid state consumes the token and redirects failed (no exception
        # text, no status mutation).
        valid = await oauth_callback(
            _callback_request(
                manager, SLUG, f"error=access_denied&state={state}"
            )
        )
        assert valid.status_code == 303
        assert valid.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
        assert "access_denied" not in valid.headers["location"]
        assert (await _meta(manager))["status"] == "oauth_required"
        # The provider-error path consumed the single-use state.
        assert await redis_async.get(_state_key(state)) is None

        replay = await oauth_callback(
            _callback_request(manager, SLUG, f"error=access_denied&state={state}")
        )
        assert replay.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"


async def test_callback_plugin_failure_redirects_failed(
    redis_async: AsyncRedis, redis_sync: SyncRedis, tmp_path: Path
) -> None:
    async with _full_manager(redis_async, redis_sync, tmp_path) as manager:
        started = await manager.connect(
            SLUG, mode="oauth", credentials={"client_id": CLIENT_ID}
        )
        state = _state_from_url(started["redirect_url"])

        response = await oauth_callback(
            _callback_request(manager, SLUG, f"code=boom&state={state}")
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/#/plugins/{SLUG}?oauth=failed"
        # The plugin ran (a valid state was consumed) but failed safely.
        assert (await _meta(manager))["status"] == "error"
