"""End-to-end API-key connect/disconnect tests (Phase 4, slice 4a).

These drive the real app (fake Redis, real host/loader/manager, bearer auth)
through the REST connect/disconnect endpoints *and* the live MCP wire so the
tool-set swap and the reserved ``core.api_key`` ordering are exercised together.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
META_KEY = "core:plugin:apikey:meta"
API_KEY = "sekret-key-value-1234"
EXTRA_SECRET = "another-plugin-token-value"

# An api_key plugin: ``whoami`` is always listed, ``secret`` appears only once
# ``core.api_key`` is stored. The credential hook records that the core already
# stored the key, and ``on_disconnect`` records that it ran.
_APIKEY_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

KEY_AT_HOOK: list[str] = []
DISCONNECTED: list[bool] = []


def _has_key(ctx: Any) -> bool:
    value = ctx.secrets.get("core.api_key")
    return isinstance(value, str) and bool(value)


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
        if _has_key(self.ctx):
            tools.append(
                Tool(
                    name="secret",
                    description="Present only once a key is stored.",
                    input_schema={"type": "object", "properties": {}},
                )
            )
        return tools

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        stored = self.ctx.secrets.get("core.api_key")
        KEY_AT_HOOK.append("match" if stored == api_key else "mismatch")

    def on_disconnect(self) -> None:
        DISCONNECTED.append(True)

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "whoami":
            return ToolResult.success({"slug": self.ctx.slug})
        return ToolResult.success({"has_key": _has_key(self.ctx)})
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=TEST_BASE_URL
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
        kwargs: dict[str, Any] = {"transport": transport, "base_url": TEST_BASE_URL}
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
    app: Starlette, slug: str
) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    url = f"{TEST_BASE_URL}/mcp/{slug}/"
    async with streamablehttp_client(
        url, headers=AUTH, httpx_client_factory=_client_factory(transport)
    ) as (read, write, _), ClientSession(read, write) as session:
        yield session


async def _import_api_key(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={
            "file": (
                "plugin.zip",
                build_plugin_zip(
                    slug="apikey",
                    auth_modes=("api_key",),
                    plugin_source=_APIKEY_SOURCE,
                ),
                "application/zip",
            )
        },
        headers=AUTH,
    )


async def _connect(
    client: httpx.AsyncClient,
    *,
    payload: Any = None,
    content: bytes | None = None,
) -> httpx.Response:
    if content is not None:
        return await client.post(
            "/api/plugins/apikey/connect",
            content=content,
            headers={**AUTH, "content-type": "application/json"},
        )
    return await client.post(
        "/api/plugins/apikey/connect", json=payload, headers=AUTH
    )


async def _wire_tool_names(app: Starlette) -> set[str]:
    async with _wire_session(app, "apikey") as session:
        return {tool.name for tool in (await session.list_tools()).tools}


# --- connect ----------------------------------------------------------------


async def test_connect_api_key_persists_swaps_and_wire(
    plugin_client: httpx.AsyncClient, app: Starlette, redis_async: AsyncRedis
) -> None:
    imported = await _import_api_key(plugin_client)
    assert imported.status_code == 201
    detail = imported.json()
    assert detail["load_state"] == "loaded"
    assert detail["auth_methods"] == ["api_key"]
    assert detail["auth_mode"] is None
    assert detail["status"] == "not_connected"
    assert [tool["name"] for tool in detail["tools"]] == ["whoami"]

    module = sys.modules["mcp_plugin_apikey.plugin"]
    assert module.KEY_AT_HOOK == []
    assert await _wire_tool_names(app) == {"whoami"}

    response = await _connect(
        plugin_client,
        payload={"mode": "api_key", "credentials": {"api_key": API_KEY}},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert API_KEY not in response.text

    body = response.json()
    assert body["auth_mode"] == "api_key"
    assert body["status"] == "connected"
    assert {tool["name"] for tool in body["tools"]} == {"whoami", "secret"}

    meta = json.loads(await redis_async.get(META_KEY) or "{}")
    assert meta["auth_mode"] == "api_key"
    assert meta["status"] == "connected"
    assert meta["version"] == "1.0.0"

    # The hook ran *after* the core stored the credential in the reserved key.
    assert module.KEY_AT_HOOK == ["match"]
    assert await redis_async.get("plugin:apikey:secret:core.api_key") == json.dumps(
        API_KEY
    )

    # The live MCP tool set swapped too.
    assert await _wire_tool_names(app) == {"whoami", "secret"}


async def test_connect_unsupported_mode_is_422(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (await _import_api_key(plugin_client)).status_code == 201
    response = await _connect(plugin_client, payload={"mode": "oauth"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "api_key"},
        {"mode": "api_key", "credentials": {}},
        {"mode": "api_key", "credentials": {"api_key": ""}},
        {"mode": "api_key", "credentials": {"api_key": 5}},
        {"mode": "api_key", "credentials": []},
    ],
)
async def test_connect_requires_a_non_empty_api_key(
    plugin_client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    assert (await _import_api_key(plugin_client)).status_code == 201
    response = await _connect(plugin_client, payload=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_connect_invalid_json_body_is_422(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (await _import_api_key(plugin_client)).status_code == 201
    for content in (b"{not json", b'"a string"', b"[]"):
        response = await _connect(plugin_client, content=content)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


async def test_connect_oversized_body_is_413(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (await _import_api_key(plugin_client)).status_code == 201
    payload = {"mode": "api_key", "credentials": {"api_key": "x" * (70 * 1024)}}
    response = await _connect(plugin_client, payload=payload)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_connect_unknown_slug_is_404(
    plugin_client: httpx.AsyncClient,
) -> None:
    for slug in ("missing", "Bad"):
        response = await plugin_client.post(
            f"/api/plugins/{slug}/connect",
            json={"mode": "none"},
            headers=AUTH,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# --- disconnect -------------------------------------------------------------


async def test_disconnect_wipes_secrets_and_resets(
    plugin_client: httpx.AsyncClient,
    app: Starlette,
    redis_async: AsyncRedis,
    json_logs: Any,
) -> None:
    assert (await _import_api_key(plugin_client)).status_code == 201
    connected = await _connect(
        plugin_client,
        payload={"mode": "api_key", "credentials": {"api_key": API_KEY}},
    )
    assert connected.status_code == 200
    await redis_async.set("plugin:apikey:secret:token", json.dumps(EXTRA_SECRET))

    module = sys.modules["mcp_plugin_apikey.plugin"]
    assert module.DISCONNECTED == []

    response = await plugin_client.post("/api/plugins/apikey/disconnect", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

    body = response.json()
    assert body["auth_mode"] is None
    assert body["status"] == "not_connected"
    assert [tool["name"] for tool in body["tools"]] == ["whoami"]

    meta = json.loads(await redis_async.get(META_KEY) or "{}")
    assert meta["auth_mode"] is None
    assert meta["status"] == "not_connected"

    # Every secret, including the reserved core key, is gone.
    assert await redis_async.get("plugin:apikey:secret:core.api_key") is None
    assert await redis_async.get("plugin:apikey:secret:token") is None
    remaining = [
        key async for key in redis_async.scan_iter(match="plugin:apikey:secret:*")
    ]
    assert remaining == []

    # ``on_disconnect`` ran and the live tool set swapped back.
    assert module.DISCONNECTED == [True]
    assert await _wire_tool_names(app) == {"whoami"}

    # No secret value appears in the response or in any captured log record.
    assert API_KEY not in response.text
    assert EXTRA_SECRET not in response.text
    rendered_logs = json.dumps(json_logs.payloads())
    assert API_KEY not in rendered_logs
    assert EXTRA_SECRET not in rendered_logs


# --- not loaded -------------------------------------------------------------


async def test_connect_and_disconnect_not_loaded_is_409(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    packages = Path(settings.DATA_DIR) / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "broken.zip").write_bytes(b"not a zip archive")

    app = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(app) as client:
        connect = await client.post(
            "/api/plugins/broken/connect", json={"mode": "none"}, headers=AUTH
        )
        assert connect.status_code == 409
        assert connect.json()["error"]["code"] == "not_loaded"

        disconnect = await client.post(
            "/api/plugins/broken/disconnect", headers=AUTH
        )
        assert disconnect.status_code == 409
        assert disconnect.json()["error"]["code"] == "not_loaded"


__all__ = ["plugin_client"]
