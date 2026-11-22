"""Phase 4 credential-containment regressions.

These drive the real app (fake Redis, real host/loader/manager, bearer auth) and
the live MCP wire to reproduce the four Phase 4 gate findings:

1. an in-flight tool failure that echoes a stored key is redacted against the
   secrets captured when the call started, even though ``disconnect`` already
   wiped them;
2. a timed-out ``on_api_key_set`` hook's leaked worker cannot recreate a secret
   or log the credential after ``disconnect`` sealed the retired generation;
3. a key-bearing tool description is redacted in connect/detail JSON and in the
   live ``tools/list``;
4. a whitespace-only API key is rejected with 422, while a padded key is stored
   exactly (never trimmed).

The still-valid connect/disconnect behaviour is asserted alongside: the hook
observes the key already stored, the live tool set swaps, and ``disconnect``
wipes every secret and resets mode/status.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from mcp import ClientSession
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_env, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
SLUG = "contain"
META_KEY = f"core:plugin:{SLUG}:meta"
API_KEY = "containment-secret-key-9876"
GATED_TOOL = "diagnostic"

# An api_key plugin: ``whoami`` is always listed; ``diagnostic`` appears only
# once a key is stored and its description embeds that key. ``diagnostic`` blocks
# when called with ``{"diagnostic": true}`` and always fails echoing the key.
# ``on_api_key_set`` can block so the host times the call out and leaks a worker.
_PLUGIN_SOURCE = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

HOOK_ENTERED = threading.Event()
HOOK_RELEASE = threading.Event()
HOOK_DONE = threading.Event()
TOOL_ENTERED = threading.Event()
TOOL_RELEASE = threading.Event()
BLOCK_HOOK = False
KEY_AT_HOOK: list[str] = []
DISCONNECTED: list[bool] = []


def _key(ctx: Any) -> str:
    value = ctx.secrets.get("core.api_key")
    return value if isinstance(value, str) else ""


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        tools = [
            Tool(
                name="whoami",
                description="Return non-secret metadata.",
                input_schema={"type": "object", "properties": {}},
            )
        ]
        key = _key(self.ctx)
        if key:
            tools.append(
                Tool(
                    name="diagnostic",
                    description="Diagnostic helper for " + key,
                    input_schema={
                        "type": "object",
                        "properties": {"diagnostic": {"type": "boolean"}},
                    },
                )
            )
        return tools

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        stored = _key(self.ctx)
        KEY_AT_HOOK.append("match" if stored == api_key else "mismatch")
        if BLOCK_HOOK:
            HOOK_ENTERED.set()
            HOOK_RELEASE.wait(30)
        # A late worker on a retired generation must not be able to persist a
        # derived secret or a credential-bearing log line after disconnect.
        self.ctx.secrets.set("derived", api_key)
        self.ctx.log("info", "credential hook used " + api_key)
        HOOK_DONE.set()

    def on_disconnect(self) -> None:
        DISCONNECTED.append(True)

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        key = _key(self.ctx)
        if name == "whoami":
            return ToolResult.success({"slug": self.ctx.slug})
        if args.get("diagnostic"):
            TOOL_ENTERED.set()
            TOOL_RELEASE.wait(30)
        self.ctx.log("info", "diagnostic used key " + key)
        return ToolResult.failure(
            "upstream_rejection", "Upstream rejected Bearer " + key
        )
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=TEST_BASE_URL,
            timeout=httpx.Timeout(60.0),
        ) as client:
            yield client


@pytest_asyncio.fixture
async def plugin_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with _running(app) as client:
        yield client


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
        return {tool.name for tool in (await session.list_tools()).tools}


def _text(result: Any) -> str:
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise AssertionError("tool result had no text content block")


def _module(slug: str = SLUG) -> Any:
    return sys.modules[f"mcp_plugin_{slug.replace('-', '_')}.plugin"]


async def _import_plugin(client: httpx.AsyncClient, slug: str = SLUG) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={
            "file": (
                "plugin.zip",
                build_plugin_zip(
                    slug=slug,
                    auth_modes=("api_key",),
                    plugin_source=_PLUGIN_SOURCE,
                ),
                "application/zip",
            )
        },
        headers=AUTH,
    )


async def _connect(
    client: httpx.AsyncClient, *, api_key: Any = API_KEY, slug: str = SLUG
) -> httpx.Response:
    return await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "api_key", "credentials": {"api_key": api_key}},
        headers=AUTH,
    )


async def _secret_keys(redis_async: AsyncRedis, slug: str = SLUG) -> list[str]:
    return [key async for key in redis_async.scan_iter(match=f"plugin:{slug}:secret:*")]


# --- still-valid behaviour --------------------------------------------------


async def test_normal_connect_then_disconnect_swaps_and_wipes(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
) -> None:
    assert (await _import_plugin(plugin_client)).status_code == 201
    module = _module()

    assert module.KEY_AT_HOOK == []
    assert await _wire_tool_names(app) == {"whoami"}

    response = await _connect(plugin_client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert API_KEY not in response.text

    body = response.json()
    assert body["auth_mode"] == "api_key"
    assert body["status"] == "connected"
    assert {tool["name"] for tool in body["tools"]} == {"whoami", GATED_TOOL}

    # The hook ran *after* the core stored the credential, and its own derived
    # write landed while the generation was still live.
    assert module.KEY_AT_HOOK == ["match"]
    assert await redis_async.get(
        f"plugin:{SLUG}:secret:core.api_key"
    ) == json.dumps(API_KEY)
    assert await redis_async.get(f"plugin:{SLUG}:secret:derived") == json.dumps(
        API_KEY
    )

    # The live MCP tool set swapped too.
    assert await _wire_tool_names(app) == {"whoami", GATED_TOOL}

    response = await plugin_client.post(
        f"/api/plugins/{SLUG}/disconnect", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["auth_mode"] is None
    assert body["status"] == "not_connected"
    assert [tool["name"] for tool in body["tools"]] == ["whoami"]

    meta = json.loads(await redis_async.get(META_KEY) or "{}")
    assert meta["auth_mode"] is None
    assert meta["status"] == "not_connected"

    assert module.DISCONNECTED == [True]
    assert await _secret_keys(redis_async) == []
    assert await _wire_tool_names(app) == {"whoami"}
    assert API_KEY not in response.text


# --- Finding 4: whitespace-only / exact key handling ------------------------


async def test_whitespace_only_api_key_is_rejected(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import_plugin(plugin_client)).status_code == 201
    response = await _connect(plugin_client, api_key="   ")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert await redis_async.get(f"plugin:{SLUG}:secret:core.api_key") is None
    assert await _secret_keys(redis_async) == []


async def test_api_key_is_stored_exactly_not_trimmed(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import_plugin(plugin_client)).status_code == 201
    exact = "  padded-key-123456  "
    response = await _connect(plugin_client, api_key=exact)
    assert response.status_code == 200
    assert await redis_async.get(
        f"plugin:{SLUG}:secret:core.api_key"
    ) == json.dumps(exact)


# --- Finding 3: credential-bearing tool description -------------------------


async def test_key_bearing_tool_description_is_redacted(
    plugin_client: httpx.AsyncClient, app: Starlette
) -> None:
    assert (await _import_plugin(plugin_client)).status_code == 201
    response = await _connect(plugin_client)
    assert response.status_code == 200
    assert API_KEY not in response.text

    body = response.json()
    connect_tools = {tool["name"]: tool for tool in body["tools"]}
    assert GATED_TOOL in connect_tools
    description = connect_tools[GATED_TOOL]["description"]
    assert API_KEY not in description
    assert description == "Diagnostic helper for ***"

    # Detail JSON never exposes the key either.
    detail = await plugin_client.get(f"/api/plugins/{SLUG}", headers=AUTH)
    assert detail.status_code == 200
    assert API_KEY not in detail.text
    detail_tools = {tool["name"]: tool for tool in detail.json()["tools"]}
    assert API_KEY not in detail_tools[GATED_TOOL]["description"]

    # The live tools/list is built from the redacted cache.
    async with _wire_session(app) as session:
        await session.initialize()
        listed = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert GATED_TOOL in listed
    advertised = listed[GATED_TOOL].description or ""
    assert API_KEY not in advertised
    assert "***" in advertised


# --- Finding 1: in-flight gated tool failure during disconnect --------------


async def test_inflight_gated_failure_is_redacted_after_disconnect(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
    json_logs: Any,
) -> None:
    assert (await _import_plugin(plugin_client)).status_code == 201
    module = _module()
    assert (await _connect(plugin_client)).status_code == 200

    async with _wire_session(app) as session:
        await session.initialize()
        call = asyncio.create_task(
            session.call_tool(GATED_TOOL, {"diagnostic": True})
        )
        await asyncio.to_thread(module.TOOL_ENTERED.wait, 5)

        disconnect = asyncio.create_task(
            plugin_client.post(f"/api/plugins/{SLUG}/disconnect", headers=AUTH)
        )
        # Wait until disconnect has sealed the generation and wiped the key, so
        # the in-flight failure is rendered only after the secrets are gone.
        wiped = False
        for _ in range(500):
            await asyncio.sleep(0.01)
            if await redis_async.get(f"plugin:{SLUG}:secret:core.api_key") is None:
                wiped = True
                break
        assert wiped

        module.TOOL_RELEASE.set()
        result = await call
        disconnect_response = await disconnect

    # The failure is surfaced, but the captured key is redacted on the wire.
    assert result.isError is True
    error_text = _text(result)
    assert API_KEY not in error_text
    assert "***" in error_text

    assert disconnect_response.status_code == 200
    assert API_KEY not in disconnect_response.text
    assert disconnect_response.json()["auth_mode"] is None

    # The late tool log is dropped by the sealed generation, so neither persisted
    # logs nor captured stdout contain the key.
    persisted = json.dumps(await app.state.plugin_manager.logs(SLUG))
    assert API_KEY not in persisted
    assert API_KEY not in json.dumps(json_logs.payloads())

    # The late worker recreated no secret.
    assert await _secret_keys(redis_async) == []


# --- Finding 2: timed-out key hook's leaked worker is contained -------------


@pytest.mark.parametrize(
    "call_timeout", [1, None], ids=["one-second", "default-timeout"]
)
async def test_timed_out_key_hook_late_worker_is_contained(
    call_timeout: int | None,
    tmp_path: Any,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    json_logs: Any,
) -> None:
    env: dict[str, str] = {"DATA_DIR": str(tmp_path / "data")}
    if call_timeout is not None:
        env["PLUGIN_CALL_TIMEOUT_SECONDS"] = str(call_timeout)
    settings = Settings.from_env(build_env(**env))
    app = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)

    async with _running(app) as client:
        assert (await _import_plugin(client)).status_code == 201
        module = _module()
        module.BLOCK_HOOK = True

        # The hook blocks past the call timeout: the connect records an error.
        response = await _connect(client)
        assert response.status_code == 200
        body = response.json()
        assert body["auth_mode"] == "api_key"
        assert body["status"] == "error"
        assert API_KEY not in response.text
        assert module.KEY_AT_HOOK == ["match"]

        # Disconnect seals and wipes while the timed-out worker is still blocked.
        disconnect_response = await client.post(
            f"/api/plugins/{SLUG}/disconnect", headers=AUTH
        )
        assert disconnect_response.status_code == 200
        assert disconnect_response.json()["auth_mode"] is None
        meta = json.loads(await redis_async.get(META_KEY) or "{}")
        assert meta["auth_mode"] is None
        assert meta["status"] == "not_connected"
        assert await _secret_keys(redis_async) == []

        # Release the leaked worker: its writes and logs must be no-ops.
        module.HOOK_RELEASE.set()
        await asyncio.to_thread(module.HOOK_DONE.wait, 5)

        assert await _secret_keys(redis_async) == []
        persisted = json.dumps(await app.state.plugin_manager.logs(SLUG))
        assert API_KEY not in persisted
        assert API_KEY not in json.dumps(json_logs.payloads())

        meta = json.loads(await redis_async.get(META_KEY) or "{}")
        assert meta["auth_mode"] is None
        assert meta["status"] == "not_connected"


__all__ = ["plugin_client"]
