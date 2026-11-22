"""Wire and unit tests for the dynamic per-slug MCP router (Phase 3).

Requests are driven through a real ``mcp`` streamable-HTTP client bound to the
router with an injected ``httpx.AsyncClient`` factory, so ``initialize`` /
``list_tools`` / ``call_tool`` exercise the same code path as production. The
host is a deterministic in-loop double so a hot swap can prove exactly which
plugin instance an in-flight call was bound to.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult
from starlette.applications import Starlette
from starlette.routing import Mount

from custom_mcp.mcp.router import MCPRouter
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp import ClientSession
from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)
from tests.helpers import JsonLogCapture

BASE_URL = "https://mcp.example.com"

NESTED_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "payload": {"$ref": "#/$defs/item"},
        "count": {"type": "integer", "default": 1},
    },
    "required": ["payload"],
    "additionalProperties": False,
    "$defs": {
        "item": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        }
    },
}

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class _Storage:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.data[key] = value

    def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.data if key.startswith(prefix))


class _Plugin(MCPPlugin):
    """A plugin whose methods must never be reached (the host double runs)."""

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return []

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self) -> Status:
        return Status.NOT_CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("router bypassed the host and called the plugin")


class _StubHost:
    """Deterministic in-loop host double.

    ``invoke_tool`` succeeds with a dict echoing the runtime generation so a hot
    swap can prove which instance a call was bound to. When ``gate`` is set, a
    call blocks on ``release`` so the test controls when it finishes.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gate = False

    async def call(self, runtime: PluginRuntime, method: str, *args: Any) -> Any:
        if method != "invoke_tool":
            raise AssertionError(f"unexpected host call: {method}")
        name, arguments = args
        self.calls.append((runtime.slug, name))
        payload = {
            "slug": runtime.slug,
            "generation": runtime.generation,
            "tool": name,
            "args": arguments,
        }
        if self.gate:
            self.started.set()
            await self.release.wait()
        return ToolResult.success(payload)


def _context(slug: str) -> PluginContext:
    return PluginContext(
        slug=slug,
        storage=_Storage(),
        secrets=_Storage(),
        log=lambda level, message: None,
        callback_url=f"{BASE_URL}/mcp/{slug}/oauth/callback",
    )


def _manifest(slug: str) -> Manifest:
    return Manifest(
        name=slug,
        version="1.0.0",
        entry="plugin:Plugin",
        auth_modes=(AuthMode.NONE,),
        display_name=slug,
        description="",
    )


def _runtime(
    tools: list[Tool],
    *,
    slug: str = "demo",
    generation: int = 0,
    status: Status = Status.CONNECTED,
) -> PluginRuntime:
    context = _context(slug)
    return PluginRuntime(
        slug=slug,
        manifest=_manifest(slug),
        context=context,
        lock=asyncio.Lock(),
        sem=asyncio.Semaphore(4),
        generation=generation,
        load_state=LoadState.LOADED,
        instance=_Plugin(context),
        tools=tools,
        status=status,
    )


def _app(router: MCPRouter) -> Starlette:
    return Starlette(routes=[Mount("/mcp", app=router)])


def _client_factory(transport: httpx.ASGITransport) -> Callable[..., httpx.AsyncClient]:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"transport": transport, "base_url": BASE_URL}
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


@asynccontextmanager
async def _wire(
    app: Starlette, slug: str, *, trailing: bool = True, headers: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    suffix = "/" if trailing else ""
    url = f"{BASE_URL}/mcp/{slug}{suffix}"
    async with streamablehttp_client(
        url, headers=headers, httpx_client_factory=_client_factory(transport)
    ) as (read, write, _), ClientSession(read, write) as session:
        yield session


async def _wait_for(predicate: Callable[[], bool], limit: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0)


def _text(result: CallToolResult) -> str:
    """Return the first text content block of a tool result."""
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise AssertionError("tool result had no text content block")


# --- wire behaviour at both slash forms -------------------------------------


@pytest.mark.parametrize("trailing", [True, False])
async def test_wire_initialize_lists_and_calls_tool(trailing: bool) -> None:
    host = _StubHost()
    router = MCPRouter(cast("PluginHost", host))
    runtime = _runtime(
        [
            Tool(name="echo", description="Echo.", input_schema=ECHO_SCHEMA),
            Tool(name="put", description="Put.", input_schema=NESTED_SCHEMA),
        ]
    )
    assert await router.mount(runtime) is True
    app = _app(router)

    async with _wire(app, "demo", trailing=trailing) as session:
        init = await session.initialize()
        assert init.serverInfo.name == "demo"

        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"echo", "put"}
        # The raw nested schema is advertised byte-for-byte.
        assert tools["put"].inputSchema == NESTED_SCHEMA
        assert json.dumps(tools["put"].inputSchema) == json.dumps(NESTED_SCHEMA)

        result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is False
        assert result.structuredContent == {
            "slug": "demo",
            "generation": 0,
            "tool": "echo",
            "args": {"text": "hi"},
        }
        assert json.loads(_text(result)) == result.structuredContent


async def test_wire_invalid_arguments_are_uniform_and_safe() -> None:
    host = _StubHost()
    router = MCPRouter(cast("PluginHost", host))
    runtime = _runtime([Tool(name="put", description="Put.", input_schema=NESTED_SCHEMA)])
    await router.mount(runtime)
    app = _app(router)

    async with _wire(app, "demo") as session:
        await session.initialize()
        result = await session.call_tool("put", {"payload": {"label": 5}, "extra": True})

    assert result.isError is True
    text = _text(result)
    assert text.startswith("[invalid_arguments] Invalid arguments for tool 'put':")
    # No argument values are echoed back.
    assert "5" not in text
    assert host.calls == []


async def test_unknown_and_invalid_slugs_return_json_404() -> None:
    router = MCPRouter(cast("PluginHost", _StubHost()))
    app = _app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        for path in ["/mcp/nope/", "/mcp/nope", "/mcp/", "/mcp/Not_Valid/", "/mcp/"]:
            response = await client.post(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"
            assert response.headers["cache-control"] == "no-store"


# --- hot swap and retirement ------------------------------------------------


async def test_hot_swap_keeps_in_flight_call_on_old_instance() -> None:
    host = _StubHost()
    host.gate = True
    router = MCPRouter(cast("PluginHost", host), drain_timeout=2.0)
    alpha = _runtime(
        [Tool(name="alpha_tool", description="A.", input_schema=ECHO_SCHEMA)],
        slug="alpha",
        generation=1,
    )
    beta = _runtime(
        [Tool(name="beta_tool", description="B.", input_schema=ECHO_SCHEMA)],
        slug="alpha",
        generation=2,
    )
    assert await router.mount(alpha) is True
    app = _app(router)

    async with _wire(app, "alpha") as session:
        await session.initialize()
        assert {t.name for t in (await session.list_tools()).tools} == {"alpha_tool"}

        call = asyncio.create_task(session.call_tool("alpha_tool", {"text": "x"}))
        await host.started.wait()

        swap = asyncio.create_task(router.mount(beta))
        await _wait_for(lambda: router.current("alpha") is beta)
        # The retired generation is draining and has not closed yet.
        assert alpha.mounted is not None
        assert alpha.mounted.task.done() is False

        host.release.set()
        await swap
        result = await call

    # The in-flight call stayed bound to alpha's immutable runtime.
    assert result.isError is False
    assert result.structuredContent == {
        "slug": "alpha",
        "generation": 1,
        "tool": "alpha_tool",
        "args": {"text": "x"},
    }
    # The retired generation's lifespan owner task exited after the drain.
    assert alpha.mounted.task.done() is True

    # A fresh session sees only the new generation's tools.
    async with _wire(app, "alpha") as session:
        tools = {t.name for t in (await session.list_tools()).tools}
    assert tools == {"beta_tool"}


async def test_drain_timeout_retires_without_waiting_forever(
    json_logs: JsonLogCapture,
) -> None:
    router = MCPRouter(cast("PluginHost", _StubHost()), drain_timeout=0.2)
    alpha = _runtime(
        [Tool(name="alpha_tool", description="A.", input_schema=ECHO_SCHEMA)],
        slug="alpha",
        generation=1,
    )
    beta = _runtime(
        [Tool(name="beta_tool", description="B.", input_schema=ECHO_SCHEMA)],
        slug="alpha",
        generation=2,
    )
    assert await router.mount(alpha) is True
    assert alpha.mounted is not None

    # Simulate a request that outlives the drain window: the counter never drops,
    # so the drain must give up after ~0.2s and still retire the owner task.
    alpha.mounted.inflight += 1
    loop = asyncio.get_running_loop()
    started = loop.time()
    await router.mount(beta)
    elapsed = loop.time() - started

    assert elapsed < 2.0
    assert alpha.mounted.task.done() is True
    assert router.current("alpha") is beta
    assert json_logs.with_message("mcp_drain_timeout")


# --- readiness --------------------------------------------------------------


class _FailingLifespanApp:
    """A stand-in FastMCP app whose lifespan raises on entry."""

    def lifespan(self, app: Starlette) -> Any:
        @asynccontextmanager
        async def context() -> AsyncIterator[None]:
            raise RuntimeError("lifespan boom")
            yield  # pragma: no cover

        return context()


async def test_mount_readiness_failure_is_not_published(
    monkeypatch: pytest.MonkeyPatch, json_logs: JsonLogCapture
) -> None:
    router = MCPRouter(cast("PluginHost", _StubHost()))
    monkeypatch.setattr(
        "custom_mcp.mcp.router.build_plugin_app",
        lambda runtime, host, **kwargs: _FailingLifespanApp(),
    )
    runtime = _runtime([Tool(name="echo", description="Echo.", input_schema=ECHO_SCHEMA)])

    assert await router.mount(runtime) is False
    assert router.is_mounted("demo") is False
    assert json_logs.with_message("mcp_mount_lifespan_failed")


async def test_unmount_unknown_is_a_noop_and_shutdown_empty_is_safe() -> None:
    router = MCPRouter(cast("PluginHost", _StubHost()))
    await router.unmount("unknown")
    await router.unmount("")
    await router.unmount("Not_Valid")
    await router.shutdown()
    assert router.mounted_slugs == ()


async def test_mount_skips_a_runtime_without_an_instance() -> None:
    router = MCPRouter(cast("PluginHost", _StubHost()))
    runtime = _runtime([Tool(name="echo", description="Echo.", input_schema=ECHO_SCHEMA)])
    runtime.instance = None
    assert await router.mount(runtime) is False
    assert router.is_mounted("demo") is False


async def test_on_runtime_change_mounts_and_unmounts() -> None:
    host = _StubHost()
    router = MCPRouter(cast("PluginHost", host))
    runtime = _runtime([Tool(name="echo", description="Echo.", input_schema=ECHO_SCHEMA)])

    await router.on_runtime_change("demo", runtime)
    assert router.is_mounted("demo") is True
    assert runtime.mounted is not None

    await router.on_runtime_change("demo", None)
    assert router.is_mounted("demo") is False
    assert runtime.mounted.task.done() is True
