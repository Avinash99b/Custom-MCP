"""Router-level tests for which requests the drain waits on (finding N1).

The drain must protect a tool call, which is always executed inside a ``POST``
handler, but must not wait on a long-lived streamable-HTTP ``GET`` (the
server-to-client SSE stream). It must also cover the ``202 Accepted`` POST form,
where the response is returned immediately and the result is delivered later on
a separate stream: the tool call still runs inside that POST, so the POST must
be counted.

These drive the router's ASGI entry point with a stand-in inner app that records
the in-flight counter it observes, so the accounting is asserted directly rather
than inferred from timing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from custom_mcp.mcp.router import MCPRouter
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)


class _RecordingApp:
    """A stand-in mounted app that records the router's in-flight counter.

    ``respond_first`` models the streamable-HTTP ``202 Accepted`` shape: the
    response is sent immediately and the work continues afterwards, with the
    result delivered on a separate stream.
    """

    def __init__(
        self,
        status: int = 200,
        hold: asyncio.Event | None = None,
        *,
        respond_first: bool = False,
    ) -> None:
        self.status = status
        self.hold = hold
        self.respond_first = respond_first
        self.observed: list[int] = []
        self.mounted: Any = None

    @asynccontextmanager
    async def lifespan(self, app: Any) -> AsyncIterator[None]:
        yield

    async def _respond(self, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.observed.append(self.mounted.inflight)
        if self.respond_first:
            # Send the 202 first, then keep working -- the tool call is still
            # running inside this POST handler.
            await self._respond(send)
            if self.hold is not None:
                await self.hold.wait()
            return
        if self.hold is not None:
            await self.hold.wait()
        await self._respond(send)


class _Plugin(MCPPlugin):
    """Unreachable: the stand-in app answers instead of the real tool path."""

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return []

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("the router should have used the stand-in app")


class _Store:
    def get(self, key: str, default: Any = None) -> Any:
        return default

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        return None

    def delete(self, key: str) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return []


def _runtime() -> PluginRuntime:
    context = PluginContext(
        slug="demo",
        storage=_Store(),
        secrets=_Store(),
        log=lambda level, message: None,
        callback_url="https://mcp.example.com/mcp/demo/oauth/callback",
    )
    return PluginRuntime(
        slug="demo",
        manifest=Manifest(
            name="demo",
            version="1.0.0",
            entry="plugin:Plugin",
            auth_modes=(AuthMode.NONE,),
            display_name="demo",
            description="",
        ),
        context=context,
        lock=asyncio.Lock(),
        sem=asyncio.Semaphore(4),
        generation=1,
        load_state=LoadState.LOADED,
        instance=_Plugin(context),
        tools=[],
        status=Status.CONNECTED,
    )


@asynccontextmanager
async def _mounted_app(
    monkeypatch: pytest.MonkeyPatch, app: _RecordingApp
) -> AsyncIterator[MCPRouter]:
    """Mount one generation whose FastMCP app is ``app``."""
    router = MCPRouter(cast("PluginHost", object()), drain_timeout=0.2)
    monkeypatch.setattr(
        "custom_mcp.mcp.router.build_plugin_app",
        lambda runtime, host, **kwargs: app,
    )
    runtime = _runtime()
    assert await router.mount(runtime) is True
    app.mounted = router._apps[runtime.slug]
    try:
        yield router
    finally:
        await router.unmount(runtime.slug)


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"{}", "more_body": False}


async def _send(message: MutableMapping[str, Any]) -> None:
    return None


def _scope(method: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": "/mcp/demo/",
        "root_path": "/mcp",
        "headers": [],
    }


async def test_post_is_counted_while_get_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a POST carries a client request that could invoke a tool."""
    app = _RecordingApp()
    async with _mounted_app(monkeypatch, app) as router:
        await router(_scope("POST"), _receive, _send)
        await router(_scope("GET"), _receive, _send)
        await router(_scope("DELETE"), _receive, _send)

    # The POST saw a live in-flight count; the stream/teardown requests did not.
    assert app.observed == [1, 0, 0]


async def test_202_accepted_post_is_still_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 202 POST runs its tool call inside the handler, so it must be drained.

    The response is sent immediately and the result arrives later on a separate
    stream; counting only the visible response would let the drain seal the
    generation out from under the running call.
    """
    hold = asyncio.Event()
    app = _RecordingApp(status=202, hold=hold, respond_first=True)
    async with _mounted_app(monkeypatch, app) as router:
        request = asyncio.ensure_future(router(_scope("POST"), _receive, _send))
        # Let the POST reach the inner app; it answers 202 and then keeps going.
        for _ in range(100):
            await asyncio.sleep(0)
            if app.observed:
                break
        mounted = app.mounted
        assert mounted.inflight == 1, "a 202 POST must be counted as in-flight"

        # A drain started now must wait for the request rather than pass through,
        # even though the response has already been sent.
        drain = asyncio.ensure_future(router.drain("demo"))
        await asyncio.sleep(0.05)
        assert not drain.done(), "the drain returned while a POST was still running"

        hold.set()
        await request
        await drain

    assert app.observed == [1]
    assert mounted.inflight == 0


async def test_drain_does_not_wait_on_an_idle_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """An open SSE GET must not consume the drain budget."""
    hold = asyncio.Event()
    app = _RecordingApp(hold=hold)
    async with _mounted_app(monkeypatch, app) as router:
        stream = asyncio.ensure_future(router(_scope("GET"), _receive, _send))
        for _ in range(100):
            await asyncio.sleep(0)
            if app.observed:
                break
        mounted = app.mounted
        assert mounted.inflight == 0

        # The GET is still held open, but the drain must return immediately.
        loop = asyncio.get_running_loop()
        started = loop.time()
        await router.drain("demo")
        assert loop.time() - started < 0.1

        hold.set()
        await stream
