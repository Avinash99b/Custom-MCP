"""End-to-end MCP-over-the-wire tests for the assembled app (Phase 3).

These drive a real ``mcp`` streamable-HTTP client through ``create_app`` (fake
Redis, real host/loader/manager, bearer auth) so the router, the manager hook
and the middleware are exercised together. Plugins are installed through the
REST API, which is what triggers a live mount.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.mcp.router import MCPRouter
from custom_mcp.plugins.host import PluginHost
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from mcp import ClientSession
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_hello_zip, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
JSON_HEADERS = {"Accept": "application/json"}

_V2_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="added",
                description="Only present in version two.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"v2": True})
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as client:
            yield client


@pytest_asyncio.fixture
async def life_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
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
    app: Starlette,
    slug: str,
    *,
    trailing: bool = True,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    url = f"{TEST_BASE_URL}/mcp/{slug}" + ("/" if trailing else "")
    async with streamablehttp_client(
        url, headers=headers, httpx_client_factory=_client_factory(transport)
    ) as (read, write, _), ClientSession(read, write) as session:
        yield session


def _text(result: CallToolResult) -> str:
    """Return the first text content block of a tool result."""
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise AssertionError("tool result had no text content block")


async def _import(
    client: httpx.AsyncClient, data: bytes, *, mode: str = "reject"
) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", data, "application/zip")},
        params={"mode": mode},
        headers=AUTH,
    )


# --- bearer-protected wire calls --------------------------------------------


@pytest.mark.parametrize("trailing", [True, False])
async def test_wire_bearer_initialize_list_and_call(
    life_client: httpx.AsyncClient, app: Starlette, trailing: bool
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201

    async with _wire_session(app, "hello", trailing=trailing, headers=AUTH) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "hello"

        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"echo", "add"}
        assert tools["echo"].inputSchema["additionalProperties"] is False

        echo = await session.call_tool("echo", {"text": "hi"})
        assert echo.isError is False
        assert echo.structuredContent == {"text": "hi"}
        assert json.loads(_text(echo)) == {"text": "hi"}

        add = await session.call_tool("add", {"a": 2, "b": 3})
        assert add.isError is False
        assert add.structuredContent == {"sum": 5}


async def test_wire_argument_error_is_uniform(
    life_client: httpx.AsyncClient, app: Starlette
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201

    async with _wire_session(app, "hello", headers=AUTH) as session:
        await session.initialize()
        result = await session.call_tool("echo", {})

    assert result.isError is True
    assert _text(result).startswith(
        "[invalid_arguments] Invalid arguments for tool 'echo':"
    )


async def test_mcp_requires_bearer_and_rejects_cookie_only(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    # A session cookie authenticates the dashboard but never the MCP endpoints.
    login = await life_client.post(
        "/login", json={"secret": TEST_SECRET}, headers=JSON_HEADERS
    )
    assert login.status_code == 200
    cookie_only = await life_client.get("/mcp/hello", headers=JSON_HEADERS)
    assert cookie_only.status_code == 401
    assert cookie_only.headers["www-authenticate"] == "Bearer"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as anon:
        anonymous = await anon.get("/mcp/hello", headers=JSON_HEADERS)
    assert anonymous.status_code == 401


async def test_unknown_and_invalid_slugs_are_json_404(
    life_client: httpx.AsyncClient,
) -> None:
    for path in ["/mcp/nope/", "/mcp/nope", "/mcp/Not_Valid/", "/mcp/"]:
        response = await life_client.post(path, headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "not_found", "message": "No such MCP endpoint."}
        }


# --- live lifecycle wiring --------------------------------------------------


async def test_replace_swaps_the_wire_tool_set(
    life_client: httpx.AsyncClient, app: Starlette
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201
    async with _wire_session(app, "hello", headers=AUTH) as session:
        assert {t.name for t in (await session.list_tools()).tools} == {"echo", "add"}

    v2 = build_plugin_zip(slug="hello", version="2.0.0", plugin_source=_V2_SOURCE)
    assert (await _import(life_client, v2, mode="replace")).status_code == 201

    async with _wire_session(app, "hello", headers=AUTH) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == {"added"}
        result = await session.call_tool("added", {})
    assert result.structuredContent == {"v2": True}


async def test_remove_unmounts_the_slug(
    life_client: httpx.AsyncClient, app: Starlette
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201
    assert (await life_client.delete("/api/plugins/hello", headers=AUTH)).status_code == 204

    response = await life_client.post("/mcp/hello/", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_lifespan_mounts_a_stored_package_at_startup(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    data_dir = Path(settings.DATA_DIR)
    packages = data_dir / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "hello.zip").write_bytes(build_hello_zip())

    app = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(app):
        assert app.state.mcp_router.is_mounted("hello") is True
        runtime = app.state.plugin_manager.runtime_for("hello")
        assert runtime is not None

    # Shutdown must unmount and close the owner task.
    assert app.state.mcp_router.is_mounted("hello") is False
    assert runtime.mounted.task.done() is True


async def test_create_app_accepts_an_injected_router(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    injected = MCPRouter(cast("PluginHost", object()))
    app = create_app(
        settings=settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
        mcp_router=injected,
    )
    assert app.state.mcp_router is injected
    async with _running(app):
        # No stored packages: nothing to mount, and shutdown stays safe.
        assert injected.mounted_slugs == ()
    assert injected.mounted_slugs == ()


# --- credential redaction, atomic swap, cancellation-safe removal -----------

_LEAK_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="leak",
                description="Echo an upstream rejection body.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        key = self.ctx.secrets.get("core.api_key", "")
        return ToolResult.failure("upstream", f"Upstream rejected Bearer {key}")
'''

_V2_GATED_SOURCE = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

ENTERED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        ENTERED.set()
        RELEASE.wait(5)

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="added",
                description="Only present in version two.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"v2": True})
'''

_BLOCKING_SOURCE = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

ENTERED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="block",
                description="Block until released.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        ENTERED.set()
        RELEASE.wait(5)
        return ToolResult.success({"ok": True})
'''


async def test_stored_credential_is_redacted_in_mcp_error(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    """A plugin error body echoing a stored credential must reach the client redacted."""
    assert (
        await _import(
            life_client,
            build_plugin_zip(slug="leaky", plugin_source=_LEAK_SOURCE),
        )
    ).status_code == 201
    await app.state.redis_async.set(
        "plugin:leaky:secret:core.api_key", json.dumps("phase3-dummy-api-credential")
    )

    async with _wire_session(app, "leaky", headers=AUTH) as session:
        await session.initialize()
        result = await session.call_tool("leak", {})

    assert result.isError is True
    text = _text(result)
    assert "phase3-dummy-api-credential" not in text
    assert "***" in text


async def test_replace_keeps_endpoint_answering_during_drain(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    """A live replacement must not 404 the endpoint while the old app drains."""
    assert (await _import(life_client, build_hello_zip())).status_code == 201

    v2 = build_plugin_zip(slug="hello", version="2.0.0", plugin_source=_V2_GATED_SOURCE)
    replace = asyncio.create_task(_import(life_client, v2, mode="replace"))

    module = None
    for _ in range(500):
        await asyncio.sleep(0.005)
        module = sys.modules.get("mcp_plugin_hello.plugin")
        if (
            module is not None
            and getattr(module, "ENTERED", None)
            and module.ENTERED.is_set()
        ):
            break
    assert module is not None and module.ENTERED.is_set()

    try:
        # The new generation is loading; the old endpoint must still answer.
        async with _wire_session(app, "hello", headers=AUTH) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == {"echo", "add"}
    finally:
        module.RELEASE.set()

    assert (await replace).status_code == 201
    async with _wire_session(app, "hello", headers=AUTH) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
    assert tools == {"added"}


async def test_cancelled_delete_is_consistent(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    """A DELETE cancelled mid-drain must not leave a loaded-but-unmounted plugin."""
    assert (
        await _import(
            life_client,
            build_plugin_zip(slug="gate", plugin_source=_BLOCKING_SOURCE),
        )
    ).status_code == 201
    module = sys.modules["mcp_plugin_gate.plugin"]
    manager = app.state.plugin_manager
    router = app.state.mcp_router

    async with _wire_session(app, "gate", headers=AUTH) as session:
        await session.initialize()
        call = asyncio.create_task(session.call_tool("block", {}))
        await asyncio.to_thread(module.ENTERED.wait, 5)

        delete = asyncio.create_task(life_client.delete("/api/plugins/gate", headers=AUTH))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if router.current("gate") is None:
                break
        delete.cancel()
        module.RELEASE.set()
        with pytest.raises(asyncio.CancelledError):
            await delete
        # The in-flight call may finish or fail as the app retires; it must not
        # hang. Either outcome is acceptable.
        await asyncio.gather(call, return_exceptions=True)

    # Consistent end state: fully removed, never loaded/connected while 404.
    assert await manager.detail("gate") is None
    assert router.is_mounted("gate") is False


# --- in-flight resource/writes, invalid reload, error-code redaction --------

_RESOURCE_SOURCE = '''\
from __future__ import annotations

import io
import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

ENTERED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._buffer = io.StringIO("live")

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="read",
                description="Read a resource.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        ENTERED.set()
        RELEASE.wait(5)
        return ToolResult.success({"value": self._buffer.getvalue()})

    def shutdown(self) -> None:
        self._buffer.close()
'''

_MIGRATION_V1 = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

ENTERED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="inc",
                description="Increment the counter.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        ENTERED.set()
        RELEASE.wait(5)
        value = int(self.ctx.storage.get("counter", 0)) + 1
        self.ctx.storage.set("counter", value)
        return ToolResult.success({"counter": value})
'''

_MIGRATION_V2 = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        value = int(self.ctx.storage.get("counter", 0)) + 100
        self.ctx.storage.set("counter", value)

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="migrated",
                description="Migration done.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"ok": True})
'''

_CODE_LEAK_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="leak",
                description="Return an upstream error code.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        key = self.ctx.secrets.get("core.api_key", "")
        return ToolResult.failure(
            f"auth_error:{key}", "Upstream denied credential."
        )
'''


@pytest.mark.parametrize("mode", ["replace", "reload"])
async def test_inflight_old_call_survives_live_runtime_change(
    app: Starlette, life_client: httpx.AsyncClient, mode: str
) -> None:
    """An in-flight old call must finish before its instance is shut down."""
    assert (
        await _import(
            life_client,
            build_plugin_zip(slug="res", plugin_source=_RESOURCE_SOURCE),
        )
    ).status_code == 201
    module = sys.modules["mcp_plugin_res.plugin"]

    async with _wire_session(app, "res", headers=AUTH) as session:
        await session.initialize()
        call = asyncio.create_task(session.call_tool("read", {}))
        await asyncio.to_thread(module.ENTERED.wait, 5)

        if mode == "replace":
            v2 = build_plugin_zip(slug="res", version="2.0.0", plugin_source=_V2_SOURCE)
            change = asyncio.create_task(_import(life_client, v2, mode="replace"))
        else:
            change = asyncio.create_task(
                life_client.post("/api/plugins/res/reload", headers=AUTH)
            )

        # Release the in-flight call while the replacement is draining.
        module.RELEASE.set()
        result = await call

    assert result.isError is False
    assert result.structuredContent == {"value": "live"}
    assert (await change).status_code in (200, 201)


async def test_inflight_old_write_is_preserved_by_upgrade_migration(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    """A completed old-call write must not be overwritten by the migration."""
    assert (
        await _import(
            life_client,
            build_plugin_zip(slug="mig", plugin_source=_MIGRATION_V1),
        )
    ).status_code == 201
    await app.state.redis_async.set("plugin:mig:data:counter", "0")
    module = sys.modules["mcp_plugin_mig.plugin"]

    async with _wire_session(app, "mig", headers=AUTH) as session:
        await session.initialize()
        call = asyncio.create_task(session.call_tool("inc", {}))
        await asyncio.to_thread(module.ENTERED.wait, 5)

        v2 = build_plugin_zip(slug="mig", version="2.0.0", plugin_source=_MIGRATION_V2)
        replace = asyncio.create_task(_import(life_client, v2, mode="replace"))

        module.RELEASE.set()
        result = await call

    assert result.structuredContent == {"counter": 1}
    assert (await replace).status_code == 201
    # The migration read the drained write (1) and added 100 -> 101.
    assert await app.state.redis_async.get("plugin:mig:data:counter") == "101"


async def test_failed_reload_invalid_package_does_not_serve_old_tools(
    app: Starlette, life_client: httpx.AsyncClient, settings: Settings
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201
    (Path(settings.DATA_DIR) / "packages" / "hello.zip").write_bytes(b"not a zip")

    assert await app.state.plugin_manager.reload("hello") is True
    detail = (await life_client.get("/api/plugins/hello", headers=AUTH)).json()
    assert detail["load_state"] == "load_failed"

    stale = await life_client.post("/mcp/hello/", headers=AUTH)
    assert stale.status_code == 404

    assert (
        await life_client.delete("/api/plugins/hello", headers=AUTH)
    ).status_code == 204
    assert (await life_client.post("/mcp/hello/", headers=AUTH)).status_code == 404


async def test_upstream_error_code_is_redacted_too(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    """A credential echoed in the SDK error *code* must be redacted as well."""
    assert (
        await _import(
            life_client,
            build_plugin_zip(slug="codeleak", plugin_source=_CODE_LEAK_SOURCE),
        )
    ).status_code == 201
    await app.state.redis_async.set(
        "plugin:codeleak:secret:core.api_key", json.dumps("phase3-code-dummy-api-credential")
    )

    async with _wire_session(app, "codeleak", headers=AUTH) as session:
        await session.initialize()
        result = await session.call_tool("leak", {})

    assert result.isError is True
    text = _text(result)
    assert "phase3-code-dummy-api-credential" not in text
    assert "***" in text
