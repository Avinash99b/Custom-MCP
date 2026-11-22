"""End-to-end health scheduler tests with the real host/loader and fake Redis.

The scheduler is driven directly (``run_cycle``) against the assembled app so
the persisted ``core:plugin:{slug}:meta`` state, the real plugin workers and the
lifecycle locks are all exercised together. Timing-sensitive concurrency cases
use plugin-owned ``threading.Event`` gates rather than arbitrary sleeps.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest_asyncio
from starlette.applications import Starlette

from custom_mcp.health import HealthScheduler
from custom_mcp.plugins.manager import PluginManager
from custom_mcp.storage.redis_client import AsyncRedis
from mcp_plugin_sdk import Status
from tests.helpers import build_plugin_zip

_META = "core:plugin:{slug}:meta"

_COMMON_TAIL = '''\
    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown tool")
'''

_CONNECTED_SOURCE = (
    '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []
REFRESH_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        return Status.CONNECTED

'''
    + _COMMON_TAIL
)

_NOT_CONNECTED_SOURCE = (
    '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        return Status.NOT_CONNECTED

'''
    + _COMMON_TAIL
)

_RAISING_SOURCE = (
    '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        raise RuntimeError("upstream health endpoint failed")

'''
    + _COMMON_TAIL
)

_SLOW_SOURCE = (
    '''\
from __future__ import annotations

import threading

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []
STARTED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        STARTED.set()
        RELEASE.wait(5)
        return Status.CONNECTED

'''
    + _COMMON_TAIL
)

_SLOW_NOT_CONNECTED_SOURCE = (
    '''\
from __future__ import annotations

import threading

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []
STARTED = threading.Event()
RELEASE = threading.Event()


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        STARTED.set()
        RELEASE.wait(5)
        return Status.NOT_CONNECTED

'''
    + _COMMON_TAIL
)

_AUTH_EXPIRED_OK_SOURCE = (
    '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []
REFRESH_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        return Status.AUTH_EXPIRED

    def refresh(self):
        REFRESH_CALLS.append("refresh")
        return Status.CONNECTED

'''
    + _COMMON_TAIL
)

_AUTH_EXPIRED_FAIL_SOURCE = (
    '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

PING_CALLS: list[str] = []
REFRESH_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_CALLS.append("ping")
        return Status.AUTH_EXPIRED

    def refresh(self):
        REFRESH_CALLS.append("refresh")
        return Status.AUTH_EXPIRED

'''
    + _COMMON_TAIL
)

_OCCUPIED_SOURCE = (
    '''\
from __future__ import annotations

import threading

from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    Status,
    Tool,
    ToolResult,
)

PING_MODE = "connected"
PING_BLOCK = False
PING_ENTERED = threading.Event()
PING_RELEASE = threading.Event()
TOOL_CALLS: list[str] = []
TOOL_ENTERED = threading.Event()
TOOL_READY = threading.Event()
TOOL_TARGET = 4
TOOL_RELEASE = threading.Event()
REFRESH_CALLS: list[str] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        schema = {"type": "object", "properties": {}}
        return [Tool(name="hold", description="Hold slots.", input_schema=schema)]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        PING_ENTERED.set()
        if PING_BLOCK:
            PING_RELEASE.wait(5)
        return Status(PING_MODE)

    def refresh(self):
        REFRESH_CALLS.append("refresh")
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        TOOL_CALLS.append("hold")
        TOOL_ENTERED.set()
        if len(TOOL_CALLS) >= TOOL_TARGET:
            TOOL_READY.set()
        TOOL_RELEASE.wait(5)
        return ToolResult.success({})
'''
)


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[Starlette]:
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def running_app(app: Starlette) -> AsyncIterator[Starlette]:
    async with _running(app) as active:
        yield active


def _scheduler(
    app: Starlette, *, threshold: int = 3, timeout: float = 1.0
) -> HealthScheduler:
    manager: PluginManager = app.state.plugin_manager
    return HealthScheduler(
        manager,
        app.state.plugin_host,
        interval=60.0,
        timeout=timeout,
        threshold=threshold,
    )


async def _install(app: Starlette, slug: str, source: str) -> None:
    manager: PluginManager = app.state.plugin_manager
    record = await manager.install(build_plugin_zip(slug=slug, plugin_source=source))
    assert record.load_state == "loaded"


async def _meta(redis_async: AsyncRedis, slug: str) -> dict[str, Any]:
    raw = await redis_async.get(_META.format(slug=slug))
    return json.loads(raw or "{}")


def _module(slug: str) -> Any:
    return sys.modules[f"mcp_plugin_{slug}.plugin"]


async def _wait_until(
    manager: PluginManager,
    slug: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 3.0,  # noqa: ASYNC109 - bounded test wait
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        health = await manager.read_health(slug)
        if predicate(health):
            return health
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"health for {slug!r} did not settle: {health}")
        await asyncio.sleep(0.01)


async def _wait_thread_event(
    event: Any, *, timeout: float = 3.0  # noqa: ASYNC109 - bounded test wait
) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, timeout), timeout + 1)


# --- state machine ----------------------------------------------------------


async def test_connected_ping_resets_counts_and_clears_error(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "healthy", _CONNECTED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    await manager.apply_health(
        "healthy",
        status=Status.CONNECTED,
        fail_count=2,
        transient_error_count=4,
        last_ping_at=None,
        last_error="old failure",
    )

    scheduler = _scheduler(running_app)
    await scheduler.run_cycle()

    meta = await _meta(redis_async, "healthy")
    assert meta["status"] == "connected"
    assert meta["fail_count"] == 0
    assert meta["transient_error_count"] == 0
    assert meta["last_error"] is None
    assert isinstance(meta["last_ping_at"], str)


async def test_hard_failure_threshold_disconnects_and_logs(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "down", _NOT_CONNECTED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    scheduler = _scheduler(running_app, threshold=2)

    await scheduler.run_cycle()
    meta = await _meta(redis_async, "down")
    assert meta["status"] == "connected"
    assert meta["fail_count"] == 1

    await scheduler.run_cycle()
    meta = await _meta(redis_async, "down")
    assert meta["status"] == "not_connected"
    assert meta["fail_count"] == 2
    assert meta["last_error"] == "Plugin health check reported not connected."

    entries = await manager.logs("down") or []
    messages = [str(entry.get("message", "")) for entry in entries]
    assert "Plugin health check reported not connected." in messages
    assert "Health status changed from connected to not_connected." in messages

    # A disconnected plugin is no longer pinged.
    await scheduler.run_cycle()
    assert _module("down").PING_CALLS == ["ping", "ping"]
    assert (await _meta(redis_async, "down"))["fail_count"] == 2


async def test_transient_failures_never_disconnect(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "flaky", _RAISING_SOURCE)
    scheduler = _scheduler(running_app, threshold=1)

    for _ in range(3):
        await scheduler.run_cycle()

    meta = await _meta(redis_async, "flaky")
    assert meta["status"] == "connected"
    assert meta["fail_count"] == 0
    assert meta["transient_error_count"] == 3
    assert isinstance(meta["last_ping_at"], str)
    assert _module("flaky").PING_CALLS == ["ping", "ping", "ping"]


async def test_auth_expired_refresh_success_resets_health(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "expired-ok", _AUTH_EXPIRED_OK_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    await manager.apply_health(
        "expired-ok",
        status=Status.CONNECTED,
        fail_count=1,
        transient_error_count=2,
        last_ping_at=None,
        last_error="old",
    )

    await _scheduler(running_app).run_cycle()

    meta = await _meta(redis_async, "expired-ok")
    assert meta["status"] == "connected"
    assert meta["fail_count"] == 0
    assert meta["transient_error_count"] == 0
    assert meta["last_error"] is None
    assert _module("expired-ok").REFRESH_CALLS == ["refresh"]


async def test_auth_expired_refresh_failure_sets_auth_expired(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "expired-bad", _AUTH_EXPIRED_FAIL_SOURCE)
    scheduler = _scheduler(running_app, threshold=1)

    await scheduler.run_cycle()

    meta = await _meta(redis_async, "expired-bad")
    assert meta["status"] == "auth_expired"
    assert meta["fail_count"] == 1
    assert meta["last_error"] == "Plugin authentication could not be refreshed."
    assert _module("expired-bad").REFRESH_CALLS == ["refresh"]


# --- eligibility ------------------------------------------------------------


async def test_only_pingable_statuses_are_pinged(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    manager: PluginManager = running_app.state.plugin_manager
    statuses = {
        "off": Status.NOT_CONNECTED,
        "oauth": Status.OAUTH_REQUIRED,
        "errored": Status.ERROR,
    }
    for slug, status in statuses.items():
        await _install(running_app, slug, _CONNECTED_SOURCE)
        await manager.apply_health(
            slug,
            status=status,
            fail_count=0,
            transient_error_count=0,
            last_ping_at=None,
            last_error=None,
        )

    await _scheduler(running_app).run_cycle()

    for slug in statuses:
        assert _module(slug).PING_CALLS == []
        assert (await _meta(redis_async, slug))["last_ping_at"] is None


# --- concurrency ------------------------------------------------------------


async def test_hung_plugin_does_not_block_another(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "slow", _SLOW_SOURCE)
    await _install(running_app, "fast", _CONNECTED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    scheduler = _scheduler(running_app)
    slow = _module("slow")

    task = asyncio.create_task(scheduler.run_cycle())
    try:
        await _wait_thread_event(slow.STARTED)
        # The fast plugin settles while the slow worker is still blocked.
        health = await _wait_until(
            manager, "fast", lambda value: value["last_ping_at"] is not None
        )
        assert health["status"] == "connected"
        assert (await manager.read_health("slow"))["last_ping_at"] is None
    finally:
        slow.RELEASE.set()
    await asyncio.wait_for(task, timeout=3)

    assert (await manager.read_health("slow"))["last_ping_at"] is not None


async def test_no_overlapping_cycles(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "slow", _SLOW_SOURCE)
    scheduler = _scheduler(running_app)
    slow = _module("slow")

    first = asyncio.create_task(scheduler.run_cycle())
    try:
        await _wait_thread_event(slow.STARTED)
        # The second tick is skipped while the first cycle is still running.
        await asyncio.wait_for(scheduler.run_cycle(), timeout=0.5)
        assert slow.PING_CALLS == ["ping"]
    finally:
        slow.RELEASE.set()
    await asyncio.wait_for(first, timeout=3)
    assert slow.PING_CALLS == ["ping"]


# --- capacity contention ----------------------------------------------------


async def test_ping_deadline_includes_waiting_for_capacity(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    """A ping queued behind occupied plugin slots times out at the configured
    deadline (recorded as a transient) instead of stalling the whole cycle."""
    await _install(running_app, "busy", _OCCUPIED_SOURCE)
    await _install(running_app, "healthy", _CONNECTED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    host = running_app.state.plugin_host
    runtime = manager.runtime_for("busy")
    assert runtime is not None
    busy = _module("busy")
    capacity = running_app.state.settings.PER_PLUGIN_CONCURRENCY
    busy.TOOL_TARGET = capacity
    tools = [
        asyncio.create_task(
            host.call(runtime, "invoke_tool", "hold", {}, timeout=5)
        )
        for _ in range(capacity)
    ]
    try:
        await _wait_thread_event(busy.TOOL_ENTERED)
        await _wait_thread_event(busy.TOOL_READY)
        # Every slot is now occupied. The cycle must complete in roughly the
        # ping deadline: the queued ping fails into a transient at 0.2s.
        scheduler = _scheduler(running_app, timeout=0.2)
        await scheduler.run_cycle()
        health = await manager.read_health("busy")
        assert health["last_ping_at"] is not None
        assert health["transient_error_count"] == 1
        assert health["fail_count"] == 0
        assert health["status"] == "connected"
        # A second cycle proceeds and observes another transient: a stalled
        # first ping cannot suppress the plugin's subsequent probes.
        await scheduler.run_cycle()
        health = await manager.read_health("busy")
        assert health["transient_error_count"] == 2
    finally:
        busy.TOOL_RELEASE.set()
        await asyncio.wait_for(asyncio.gather(*tools, return_exceptions=True), 5)
    # Capacity recovered: a later ping succeeds and resets the transient count.
    await scheduler.run_cycle()
    health = await manager.read_health("busy")
    assert health["status"] == "connected"
    assert health["transient_error_count"] == 0


async def test_refresh_capacity_wait_releases_lifecycle_lock(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    """A refresh queued behind occupied slots times out and persists a hard
    failure instead of holding the lifecycle lock for the whole occupancy."""
    await _install(running_app, "expiring", _OCCUPIED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    host = running_app.state.plugin_host
    runtime = manager.runtime_for("expiring")
    assert runtime is not None
    busy = _module("expiring")
    busy.PING_MODE = "auth_expired"
    busy.PING_BLOCK = True
    await manager.apply_health(
        "expiring",
        status=Status.AUTH_EXPIRED,
        fail_count=0,
        transient_error_count=0,
        last_ping_at=None,
        last_error=None,
    )
    capacity = running_app.state.settings.PER_PLUGIN_CONCURRENCY
    # Only ``capacity - 1`` tool workers can start while the ping holds the
    # last slot, so the ready latch signals once those are all running.
    busy.TOOL_TARGET = max(1, capacity - 1)
    scheduler = _scheduler(running_app, timeout=0.5, threshold=1)
    cycle = asyncio.create_task(scheduler.run_cycle())
    tools: list[Any] = []
    try:
        # The ping enters first and holds one slot.
        await _wait_thread_event(busy.PING_ENTERED)
        # Queue ``capacity`` tool calls: only ``capacity - 1`` can run while
        # the ping holds the last slot. Releasing the ping hands its slot to
        # the fourth tool (FIFO), so every slot stays occupied and the refresh
        # that follows is forced to wait on full capacity.
        tools = [
            asyncio.create_task(
                host.call(runtime, "invoke_tool", "hold", {}, timeout=5)
            )
            for _ in range(capacity)
        ]
        await _wait_thread_event(busy.TOOL_READY)
        busy.PING_RELEASE.set()
        await asyncio.wait_for(cycle, timeout=3)
        health = await manager.read_health("expiring")
        # The refresh never reached the plugin; the failed refresh is a hard
        # failure that resolves to auth_expired.
        assert health["status"] == "auth_expired"
        assert health["fail_count"] == 1
        assert health["transient_error_count"] == 0
        assert busy.REFRESH_CALLS == []
        # The lifecycle lock was released once the deadline fired.
        assert not runtime.lock.locked()
    finally:
        busy.PING_RELEASE.set()
        busy.TOOL_RELEASE.set()
        await asyncio.wait_for(asyncio.gather(*tools, return_exceptions=True), 5)
        await asyncio.wait_for(cycle, 5)


# --- stale runtime guard ----------------------------------------------------


async def test_disconnect_during_ping_is_not_undone(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "slow", _SLOW_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    scheduler = _scheduler(running_app)
    slow = _module("slow")

    task = asyncio.create_task(scheduler.run_cycle())
    try:
        await _wait_thread_event(slow.STARTED)
        await manager.disconnect("slow")
        assert (await _meta(redis_async, "slow"))["status"] == "not_connected"
    finally:
        slow.RELEASE.set()
    await asyncio.wait_for(task, timeout=3)

    meta = await _meta(redis_async, "slow")
    assert meta["status"] == "not_connected"
    assert meta["fail_count"] == 0
    assert meta["transient_error_count"] == 0
    # The stale connected result never persisted a ping timestamp.
    assert meta["last_ping_at"] is None


async def test_replacement_during_ping_is_not_undone(
    running_app: Starlette, redis_async: AsyncRedis
) -> None:
    await _install(running_app, "slow", _SLOW_NOT_CONNECTED_SOURCE)
    manager: PluginManager = running_app.state.plugin_manager
    await manager.apply_health(
        "slow",
        status=Status.CONNECTED,
        fail_count=5,
        transient_error_count=0,
        last_ping_at=None,
        last_error=None,
    )
    scheduler = _scheduler(running_app, threshold=1)
    old_module = _module("slow")
    old_runtime = manager.runtime_for("slow")

    task = asyncio.create_task(scheduler.run_cycle())
    try:
        await _wait_thread_event(old_module.STARTED)
        assert await manager.reload("slow") is True
        assert manager.runtime_for("slow") is not old_runtime
    finally:
        # Release the *old* generation's worker; the replacement has its own
        # module-level events.
        old_module.RELEASE.set()
    await asyncio.wait_for(task, timeout=3)

    meta = await _meta(redis_async, "slow")
    # The old generation's NOT_CONNECTED observation is discarded: the new
    # runtime is connected and the seeded fail count is untouched.
    assert meta["status"] == "connected"
    assert meta["fail_count"] == 5
    assert meta["last_ping_at"] is None


# --- lifespan wiring --------------------------------------------------------


async def test_lifespan_starts_and_stops_the_scheduler(app: Starlette) -> None:
    scheduler: HealthScheduler = app.state.health
    assert not scheduler.running
    async with _running(app):
        assert scheduler.running
    assert not scheduler.running
