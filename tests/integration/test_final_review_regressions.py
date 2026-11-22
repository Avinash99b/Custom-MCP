"""Regression tests for the final whole-tree review findings.

Two defects reproduced independently during the final review:

1. **Published-state credential leak (reload/replace/startup).** The loader's
   raw tool listing was written straight into ``record.tools`` (served verbatim
   by ``GET /api/plugins/{slug}``) and only redacted later in ``_refresh_tools``.
   A plugin whose ``list_tools`` embeds a stored credential therefore exposed it
   through the detail API for as long as its ``connect`` took. Fix: redact the
   listing before it is ever published (``_redact_listed_tools``).
2. **Silent lost write on a slow in-flight call.** ``MCPRouter.drain`` had a 5 s
   budget; a legitimate tool call bounded only by ``TOOL_TIMEOUT_SECONDS`` could
   outlast it, after which the old generation was sealed and the call's storage
   write was dropped while the call still reported success. Fix: derive the
   drain budget from ``TOOL_TIMEOUT_SECONDS`` so a legitimate call always
   finishes before the seal and the replacement snapshot.

The re-verification added two more findings, pinned by the tests below:

N1. **Drain paid twice and pinned by idle streams.** ``PluginManager`` drained
    the retiring generation before sealing it, then ``MCPRouter.mount`` retired
    that same generation and drained it again, and a long-lived streamable-HTTP
    ``GET`` (SSE) counted as in-flight for its whole lifetime -- so one replace
    could wait ``2 x drain_timeout``. Fix: only ``POST`` traffic is counted and
    drained, and a generation's budget is spent at most once.
N2. **Secret redaction skipped ``input_schema`` and the tool name.**
    ``_redact_listed_tools`` rewrote only ``description``, so a credential in a
    schema ``default``/``enum``/``const``/``title``/``description``, a property
    name, or the tool name leaked through both the detail API and MCP
    ``tools/list``. Fix: redact every string leaf of the schema and the name,
    dropping a tool whose schema can no longer validate arguments.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from mcp import ClientSession
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_env, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
JSON_HEADERS = {"Accept": "application/json", **AUTH}
SECRET = "SUPER-SECRET-KEY-42"


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as client:
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
async def _wire_session(app: Starlette, slug: str) -> AsyncIterator[ClientSession]:
    transport = httpx.ASGITransport(app=app)
    url = f"{TEST_BASE_URL}/mcp/{slug}/"
    async with (
        streamablehttp_client(
            url, headers=AUTH, httpx_client_factory=_client_factory(transport)
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        yield session


async def _import(client: httpx.AsyncClient, data: bytes, mode: str = "reject") -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("p.zip", data, "application/zip")},
        params={"mode": mode},
        headers=JSON_HEADERS,
    )


def _current_module(slug: str) -> Any:
    return sys.modules.get(f"mcp_plugin_{slug}.plugin")


# --- finding 1: no unredacted description in the load window -----------------

_LEAKER_SOURCE = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

CONNECT_ENTERED = threading.Event()
CONNECT_RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        key = self.ctx.secrets.get("core.api_key") or "absent"
        return [
            Tool(
                name="whoami",
                description="diagnostic key=" + str(key),
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        # Block only when the plugin's own scoped data flag is set, so the load
        # -> post-connect-refresh window is wide enough to observe.
        if self.ctx.storage.get("slow") is True:
            CONNECT_ENTERED.set()
            CONNECT_RELEASE.wait(30)
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({})
'''


async def test_reload_never_publishes_an_unredacted_description(
    app: Starlette, redis_sync: SyncRedis
) -> None:
    slug = "reloadredact"
    async with _running(app) as client:
        imported = await _import(
            client,
            build_plugin_zip(slug=slug, auth_modes=("api_key",), plugin_source=_LEAKER_SOURCE),
        )
        assert imported.status_code == 201, imported.text
        connected = await client.post(
            f"/api/plugins/{slug}/connect",
            json={"mode": "api_key", "credentials": {"api_key": SECRET}},
            headers=JSON_HEADERS,
        )
        assert connected.status_code == 200, connected.text
        # The key is now stored; a connected listing must be redacted.
        detail = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        assert SECRET not in str(detail["tools"])

        # Make the reload's connect() block so we can read the detail mid-flight.
        redis_sync.set(f"plugin:{slug}:data:slow", "true")
        assert _current_module(slug) is not None

        reload_task = asyncio.ensure_future(
            client.post(f"/api/plugins/{slug}/reload", headers=JSON_HEADERS)
        )
        entered = False
        for _ in range(1000):
            await asyncio.sleep(0.01)
            module = _current_module(slug)
            if module is not None and module.CONNECT_ENTERED.is_set():
                entered = True
                break
        assert entered, "reload connect() never blocked; the window was not observable"

        mid = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        _current_module(slug).CONNECT_RELEASE.set()
        assert (await reload_task).status_code == 200
        assert SECRET not in str(mid["tools"]), "stored credential exposed during the load window"
        after = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        assert SECRET not in str(after["tools"])


# --- finding 2: a slow in-flight call's write is not silently lost -----------

_SLOW_V1 = '''\
from __future__ import annotations

import time
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [
            Tool(
                name="slow_bump",
                description="slow bump",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        # Longer than the old 5s drain budget but well under TOOL_TIMEOUT_SECONDS.
        time.sleep(6.5)
        value = int(self.ctx.storage.get("counter", 0)) + 1
        self.ctx.storage.set("counter", value)
        return ToolResult.success({"counter": value, "pid": "v1"})
'''

_SLOW_V2 = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [
            Tool(
                name="bump",
                description="bump",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        value = int(self.ctx.storage.get("counter", 0)) + 1
        self.ctx.storage.set("counter", value)
        return ToolResult.success({"counter": value, "pid": "v2"})
'''


async def test_slow_inflight_call_write_survives_a_replace(
    app: Starlette, redis_sync: SyncRedis
) -> None:
    slug = "slowwrite"
    async with _running(app) as client:
        assert (
            await _import(client, build_plugin_zip(slug=slug, plugin_source=_SLOW_V1))
        ).status_code == 201
        redis_sync.set(f"plugin:{slug}:data:counter", "100")

        async def slow_call() -> Any:
            async with _wire_session(app, slug) as session:
                return await session.call_tool("slow_bump", {})

        call = asyncio.ensure_future(slow_call())
        await asyncio.sleep(0.5)
        # The owner replaces the plugin while the call is still running.
        replace = asyncio.ensure_future(
            _import(client, build_plugin_zip(slug=slug, plugin_source=_SLOW_V2), "replace")
        )

        result = await call
        assert result.isError is False
        assert '"counter": 101' in result.content[0].text
        assert (await replace).status_code == 201
        # The successful call's write must not have been dropped by the seal.
        assert redis_sync.get(f"plugin:{slug}:data:counter") == "101"


# --- N1: one drain budget per generation, idle SSE streams are not drained ----

_DRAIN_PROBE_V1 = '''\
from __future__ import annotations

import time
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [
            Tool(
                name="slow_hold",
                description="Hold the request open.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        # Far longer than the (deliberately tiny) drain budget, so the pre-seal
        # drain must time out with the request still in flight.
        time.sleep(20)
        self.ctx.storage.set("landed", "yes")
        return ToolResult.success({"ok": True})
'''

_DRAIN_PROBE_V2 = _DRAIN_PROBE_V1.replace(
    'name="slow_hold"', 'name="slow_hold_v2"'
).replace("time.sleep(20)", "pass")


@asynccontextmanager
async def _open_sse_stream(app: Starlette, slug: str) -> AsyncIterator[None]:
    """Keep a streamable-HTTP GET open for the duration of the block."""
    transport = httpx.ASGITransport(app=app)
    stop = asyncio.Event()

    async def hold() -> None:
        async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as c:
            try:
                async with c.stream(
                    "GET",
                    f"/mcp/{slug}/",
                    headers={**AUTH, "Accept": "text/event-stream"},
                    timeout=httpx.Timeout(120.0),
                ) as response:
                    async for _ in response.aiter_raw():
                        if stop.is_set():
                            return
            except BaseException:
                return

    task = asyncio.ensure_future(hold())
    try:
        # Give the GET time to reach the router and register its SSE stream.
        await asyncio.sleep(0.5)
        yield
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_idle_sse_stream_does_not_delay_a_replace(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    """An open GET stream must not be counted as in-flight request traffic.

    Pre-fix the GET pinned ``_Mounted.inflight`` for its whole lifetime, so a
    replace with an idle client waited the full drain budget -- twice, once for
    ``PluginManager``'s drain and once for the retiring ``mount``.
    """
    slug = "ssedrain"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), TOOL_TIMEOUT_SECONDS="10")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    router = app.state.mcp_router
    drain_calls: list[tuple[int, float]] = []
    original = router._drain

    async def counting_drain(mounted: Any, **kwargs: Any) -> None:
        started = time.monotonic()
        await original(mounted, **kwargs)
        drain_calls.append((mounted.generation, round(time.monotonic() - started, 2)))

    router._drain = counting_drain

    async with _running(app) as client:
        assert (
            await _import(
                client, build_plugin_zip(slug=slug, plugin_source=_DRAIN_PROBE_V1)
            )
        ).status_code == 201

        async with _open_sse_stream(app, slug):
            await asyncio.sleep(0.5)
            mounted = router._apps[slug]
            # The GET is open, but it carries no client request.
            assert mounted.inflight == 0

            started = time.monotonic()
            replaced = await _import(
                client,
                build_plugin_zip(slug=slug, plugin_source=_DRAIN_PROBE_V2),
                mode="replace",
            )
            elapsed = time.monotonic() - started

    assert replaced.status_code == 201
    # The whole budget is 15s; an idle stream must not consume any of it.
    assert elapsed < 3.0, f"replace waited {elapsed:.2f}s on an idle SSE stream"
    assert sum(seconds for _, seconds in drain_calls) < 1.0


async def test_one_replace_pays_the_drain_budget_once(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    """A POST that outlives the budget must not be waited for twice.

    ``PluginManager`` drains before the seal and ``MCPRouter.mount`` retires the
    same generation, so pre-fix a slow call cost ``2 x drain_timeout``. The
    second drain must be free, while the first still protects the write ordering.
    """
    slug = "singledrain"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), TOOL_TIMEOUT_SECONDS="20")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    router = app.state.mcp_router
    budget = 2.0
    router._drain_timeout = budget

    drain_calls: list[dict[str, Any]] = []
    original = router._drain

    async def counting_drain(mounted: Any, **kwargs: Any) -> None:
        started = time.monotonic()
        await original(mounted, **kwargs)
        drain_calls.append(
            {
                "generation": mounted.generation,
                "inflight": mounted.inflight,
                "spent": mounted.drain_spent,
                "seconds": round(time.monotonic() - started, 2),
            }
        )

    router._drain = counting_drain

    async with _running(app) as client:
        assert (
            await _import(
                client, build_plugin_zip(slug=slug, plugin_source=_DRAIN_PROBE_V1)
            )
        ).status_code == 201

        async def slow_call() -> Any:
            async with _wire_session(app, slug) as session:
                return await session.call_tool("slow_hold", {})

        call = asyncio.ensure_future(slow_call())
        await asyncio.sleep(0.75)
        assert router._apps[slug].inflight == 1

        started = time.monotonic()
        replaced = await _import(
            client,
            build_plugin_zip(slug=slug, plugin_source=_DRAIN_PROBE_V2),
            mode="replace",
        )
        elapsed = time.monotonic() - started
        await asyncio.gather(call, return_exceptions=True)

    assert replaced.status_code == 201
    # Only the retiring generation (1) is drained; the new one (2) is drained
    # later at shutdown, which is outside this measurement.
    retiring = [call for call in drain_calls if call["generation"] == 1]
    assert len(retiring) == 2, drain_calls
    # Two drains of the retiring generation, but only the first one may wait.
    assert retiring[0]["spent"] is True, drain_calls
    assert retiring[0]["seconds"] > 0.0, drain_calls
    assert retiring[1]["seconds"] == 0.0, drain_calls
    assert elapsed < budget * 1.6, f"replace waited {elapsed:.2f}s for a {budget}s budget"


async def test_shutdown_is_bounded_with_an_open_sse_stream(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    """Shutdown must not wait a drain budget for an idle client.

    The container's ``stop_grace_period`` is sized from
    ``graceful_shutdown_seconds``; this pins the property that makes that bound
    hold -- an open SSE stream costs nothing at shutdown.
    """
    slug = "shutbound"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), TOOL_TIMEOUT_SECONDS="30")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    router = app.state.mcp_router

    context = app.router.lifespan_context(app)
    await context.__aenter__()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as client:
        assert (
            await _import(
                client, build_plugin_zip(slug=slug, plugin_source=_DRAIN_PROBE_V2)
            )
        ).status_code == 201

    async def hold_get() -> None:
        async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as c:
            try:
                async with c.stream(
                    "GET",
                    f"/mcp/{slug}/",
                    headers={**AUTH, "Accept": "text/event-stream"},
                    timeout=httpx.Timeout(120.0),
                ) as response:
                    async for _ in response.aiter_raw():
                        pass
            except BaseException:
                return

    get_task = asyncio.ensure_future(hold_get())
    try:
        await asyncio.sleep(0.75)
        assert router._apps[slug].inflight == 0

        started = time.monotonic()
        await context.__aexit__(None, None, None)
        elapsed = time.monotonic() - started
    finally:
        get_task.cancel()
        await asyncio.gather(get_task, return_exceptions=True)

    assert elapsed < 3.0, f"shutdown waited {elapsed:.2f}s on an idle SSE stream"


# --- N2: schema- and name-embedded secrets are redacted on every surface -----

_SCHEMA_LEAK_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        key = str(self.ctx.secrets.get("core.api_key") or "absent")
        return [
            Tool(
                name="schema_leak",
                description="benign description",
                input_schema={
                    "type": "object",
                    "title": "key=" + key,
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": "also key=" + key,
                            "default": "key=" + key,
                            "enum": ["key=" + key, "safe"],
                            "const": "key=" + key,
                        },
                        "nested": {
                            "type": "object",
                            "properties": {
                                "deep": {"type": "string", "default": "key=" + key}
                            },
                        },
                    },
                },
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

_NAME_LEAK_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        key = str(self.ctx.secrets.get("core.api_key") or "absent")
        return [
            Tool(
                name="safe_tool",
                description="benign",
                input_schema={"type": "object", "properties": {}},
            ),
            Tool(
                name="leak_" + key,
                description="benign",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

_PATTERN_LEAK_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        key = str(self.ctx.secrets.get("core.api_key") or "absent")
        return [
            Tool(
                name="safe_tool",
                description="benign",
                input_schema={"type": "object", "properties": {}},
            ),
            Tool(
                name="pattern_tool",
                description="benign",
                # Redacting this would leave "***" -- an invalid regex -- so the
                # schema could no longer validate arguments.
                input_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "pattern": "^" + key + "$"}
                    },
                },
            ),
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''


async def _install_and_connect(
    client: httpx.AsyncClient, slug: str, source: str
) -> dict[str, Any]:
    imported = await _import(
        client, build_plugin_zip(slug=slug, auth_modes=("api_key",), plugin_source=source)
    )
    assert imported.status_code == 201, imported.text
    connected = await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "api_key", "credentials": {"api_key": SECRET}},
        headers=JSON_HEADERS,
    )
    assert connected.status_code == 200, connected.text
    return connected.json()


async def test_schema_embedded_secret_is_redacted_on_both_surfaces(
    app: Starlette, redis_sync: SyncRedis
) -> None:
    """A credential in any schema string leaf must not reach either surface."""
    slug = "schemaredact"
    async with _running(app) as client:
        connected = await _install_and_connect(client, slug, _SCHEMA_LEAK_SOURCE)

        # Surface 1: the detail API serves ``record.tools`` verbatim.
        assert SECRET not in json.dumps(connected["tools"]), "connect response leaked"
        detail = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        assert SECRET not in json.dumps(detail["tools"]), "detail API leaked"

        # Surface 2: MCP ``tools/list`` serves the schema straight to FastMCP.
        async with _wire_session(app, slug) as session:
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}

    assert SECRET not in json.dumps(
        {name: tool.model_dump() for name, tool in listed.items()}
    ), "MCP tools/list leaked"

    # The tool survives, still advertises a valid schema, and the credential is
    # replaced rather than the whole tool being dropped.
    assert "schema_leak" in listed
    schema = listed["schema_leak"].inputSchema
    Draft202012Validator.check_schema(schema)
    assert "***" in json.dumps(schema)
    # Nested leaves are covered too, not just top-level schema fields.
    assert schema["properties"]["nested"]["properties"]["deep"]["default"] == "key=***"


async def test_secret_in_a_tool_name_drops_only_that_tool(
    app: Starlette, redis_sync: SyncRedis
) -> None:
    """A tool whose *name* embeds a credential cannot be safely renamed."""
    slug = "nameredact"
    async with _running(app) as client:
        connected = await _install_and_connect(client, slug, _NAME_LEAK_SOURCE)
        assert SECRET not in json.dumps(connected["tools"])
        detail = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        assert SECRET not in json.dumps(detail["tools"])

        async with _wire_session(app, slug) as session:
            listed = {tool.name for tool in (await session.list_tools()).tools}

    assert SECRET not in json.dumps(sorted(listed))
    # Only the offending tool is dropped; the clean one keeps working.
    assert listed == {"safe_tool"}
    assert [tool["name"] for tool in detail["tools"]] == ["safe_tool"]


async def test_schema_that_cannot_survive_redaction_is_dropped(
    app: Starlette, redis_sync: SyncRedis
) -> None:
    """A credential must never be surfaced, even at the cost of dropping a tool.

    Redacting a ``pattern`` that *is* the credential leaves an invalid regular
    expression, so the schema would reject every call. The safe choice is to
    stop advertising that tool rather than publish a broken schema -- and never
    to publish the credential.
    """
    slug = "patternredact"
    async with _running(app) as client:
        connected = await _install_and_connect(client, slug, _PATTERN_LEAK_SOURCE)
        assert SECRET not in json.dumps(connected["tools"])

        async with _wire_session(app, slug) as session:
            listed = {tool.name for tool in (await session.list_tools()).tools}

        assert listed == {"safe_tool"}
        detail = (await client.get(f"/api/plugins/{slug}", headers=JSON_HEADERS)).json()
        assert [tool["name"] for tool in detail["tools"]] == ["safe_tool"]
        assert SECRET not in json.dumps(detail)

        # The dropped tool never appears, so a caller cannot invoke it blind.
        async with _wire_session(app, slug) as session:
            result = await session.call_tool("pattern_tool", {"value": "x"})
        assert result.isError is True

