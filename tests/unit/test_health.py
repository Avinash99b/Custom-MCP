"""Focused health-scheduler state-machine tests.

These use a lightweight fake manager/host so each ping-result row, the
transient/hard split, the lifecycle lock around ``refresh`` and the stale
runtime guard can be asserted directly. The assembled stack is covered by
``tests/integration/test_health_scheduler.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from custom_mcp.health import HealthScheduler
from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.manager import PluginManager
from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import MCPPlugin, PluginContext, Status


class _EmptyStorage:
    def get(self, key: str, default: Any = None) -> Any:
        return default

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        return None

    def delete(self, key: str) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return []


def _new_runtime(
    slug: str,
    *,
    status: Status = Status.CONNECTED,
    load_state: LoadState = LoadState.LOADED,
    has_instance: bool = True,
    generation: int = 1,
) -> PluginRuntime:
    manifest = Manifest.from_dict(
        {
            "name": slug,
            "version": "1.0.0",
            "entry": "plugin:Plugin",
            "auth_modes": ["none"],
        }
    )
    context = PluginContext(
        slug=slug,
        storage=_EmptyStorage(),
        secrets=_EmptyStorage(),
        log=lambda level, message: None,
        callback_url=f"https://mcp.example.com/mcp/{slug}/oauth/callback",
    )
    return PluginRuntime(
        slug=slug,
        manifest=manifest,
        context=context,
        lock=asyncio.Lock(),
        sem=asyncio.Semaphore(1),
        generation=generation,
        load_state=load_state,
        instance=cast("MCPPlugin", object()) if has_instance else None,
        status=status,
    )


def _health(status: Status, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": status.value,
        "fail_count": 0,
        "transient_error_count": 0,
        "last_ping_at": None,
        "last_error": None,
    }
    values.update(overrides)
    return values


class FakeManager:
    def __init__(self, *runtimes: PluginRuntime) -> None:
        self.runtimes = {runtime.slug: runtime for runtime in runtimes}
        self.health = {
            runtime.slug: _health(runtime.status) for runtime in runtimes
        }
        self.applied: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str, str]] = []
        self.applied_events = {
            runtime.slug: asyncio.Event() for runtime in runtimes
        }

    def runtime_for(self, slug: str) -> PluginRuntime | None:
        return self.runtimes.get(slug)

    def loaded_runtimes(self) -> list[PluginRuntime]:
        return list(self.runtimes.values())

    def replace(self, slug: str, runtime: PluginRuntime) -> None:
        self.runtimes[slug] = runtime
        self.health.setdefault(slug, _health(runtime.status))
        self.applied_events.setdefault(slug, asyncio.Event())

    async def read_health(self, slug: str) -> dict[str, Any]:
        return dict(self.health[slug])

    async def apply_health(
        self,
        slug: str,
        *,
        status: Status | str,
        fail_count: int,
        transient_error_count: int,
        last_ping_at: str | None,
        last_error: str | None,
    ) -> None:
        value = status.value if isinstance(status, Status) else status
        self.health[slug].update(
            status=value,
            fail_count=fail_count,
            transient_error_count=transient_error_count,
            last_ping_at=last_ping_at,
            last_error=last_error,
        )
        self.applied.append(
            (
                slug,
                {
                    "status": value,
                    "fail_count": fail_count,
                    "transient_error_count": transient_error_count,
                    "last_ping_at": last_ping_at,
                    "last_error": last_error,
                },
            )
        )
        self.applied_events[slug].set()

    async def log_health(
        self, slug: str, message: str, *, level: str = "info"
    ) -> None:
        self.logs.append((slug, level, message))

    async def retain_current_secrets(self, slug: str) -> None:
        return None


Handler = Callable[[PluginRuntime], Awaitable[Any]]


class FakeHost:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Handler] = {}
        self.calls: list[tuple[str, str, float | None]] = []

    def set_handler(self, slug: str, method: str, handler: Handler) -> None:
        self.handlers[(slug, method)] = handler

    async def call(
        self,
        runtime: PluginRuntime,
        method: str,
        *args: Any,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors host signature
    ) -> Any:
        self.calls.append((runtime.slug, method, timeout))
        handler = self.handlers.get((runtime.slug, method))
        if handler is None:
            return Status.CONNECTED
        return await handler(runtime)


def _scheduler(
    manager: FakeManager,
    host: FakeHost,
    *,
    threshold: int = 3,
    timeout: float = 5.0,
) -> HealthScheduler:
    return HealthScheduler(
        cast("PluginManager", manager),
        cast("PluginHost", host),
        interval=1.0,
        timeout=timeout,
        threshold=threshold,
    )


# --- ping result rows -------------------------------------------------------


async def test_connected_resets_counts_and_clears_error() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    manager.health["demo"].update(
        fail_count=2, transient_error_count=4, last_error="old failure"
    )
    host = FakeHost()
    host.set_handler("demo", "ping", lambda _runtime: _result(Status.CONNECTED))
    scheduler = _scheduler(manager, host, timeout=3.0)

    await scheduler.run_cycle()

    health = manager.health["demo"]
    assert health["status"] == "connected"
    assert health["fail_count"] == 0
    assert health["transient_error_count"] == 0
    assert health["last_error"] is None
    assert isinstance(health["last_ping_at"], str)
    assert host.calls == [("demo", "ping", 3.0)]


async def test_not_connected_is_hard_and_threshold_changes_status() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    host.set_handler("demo", "ping", lambda _runtime: _result(Status.NOT_CONNECTED))
    scheduler = _scheduler(manager, host, threshold=2)

    await scheduler.run_cycle()
    health = manager.health["demo"]
    assert health["status"] == "connected"
    assert health["fail_count"] == 1

    await scheduler.run_cycle()
    health = manager.health["demo"]
    assert health["status"] == "not_connected"
    assert health["fail_count"] == 2
    assert (health["last_error"]) == "Plugin health check reported not connected."
    assert ("demo", "error", "Plugin health check reported not connected.") in manager.logs
    assert (
        "demo",
        "info",
        "Health status changed from connected to not_connected.",
    ) in manager.logs

    # Once disconnected the plugin is no longer eligible.
    await scheduler.run_cycle()
    assert [call[1] for call in host.calls] == ["ping", "ping"]


@pytest.mark.parametrize(
    "outcome",
    [
        Status.ERROR,
        Status.OAUTH_REQUIRED,
        RuntimeError("boom"),
        PluginCallError("timeout", "Plugin call timed out."),
        PluginCallError("plugin_overloaded", "Plugin is busy; try again later."),
    ],
)
async def test_transient_failures_never_count_as_hard(outcome: Any) -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()

    async def ping(_runtime: PluginRuntime) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    host.set_handler("demo", "ping", ping)
    scheduler = _scheduler(manager, host, threshold=1)

    for _ in range(3):
        await scheduler.run_cycle()

    health = manager.health["demo"]
    assert health["status"] == "connected"
    assert health["fail_count"] == 0
    assert health["transient_error_count"] == 3
    assert isinstance(health["last_ping_at"], str)
    assert isinstance(health["last_error"], str)
    assert len(host.calls) == 3
    # No hard-failure log and no status transition log.
    assert ("demo", "info", "Health status changed from connected to not_connected.") not in (
        manager.logs
    )


# --- auth-expired refresh ---------------------------------------------------


async def test_auth_expired_refresh_success_runs_under_lock() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    manager.health["demo"].update(fail_count=1, transient_error_count=2)
    host = FakeHost()
    lock_states: list[bool] = []

    host.set_handler("demo", "ping", lambda _runtime: _result(Status.AUTH_EXPIRED))

    async def refresh(active: PluginRuntime) -> Any:
        lock_states.append(active.lock.locked())
        return Status.CONNECTED

    host.set_handler("demo", "refresh", refresh)
    scheduler = _scheduler(manager, host)

    await scheduler.run_cycle()

    health = manager.health["demo"]
    assert health["status"] == "connected"
    assert health["fail_count"] == 0
    assert health["transient_error_count"] == 0
    assert health["last_error"] is None
    assert lock_states == [True]
    assert [call[1] for call in host.calls] == ["ping", "refresh"]


async def test_auth_expired_refresh_failure_is_hard_and_sets_auth_expired() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    lock_states: list[bool] = []

    host.set_handler("demo", "ping", lambda _runtime: _result(Status.AUTH_EXPIRED))

    async def refresh(active: PluginRuntime) -> Any:
        lock_states.append(active.lock.locked())
        return Status.AUTH_EXPIRED

    host.set_handler("demo", "refresh", refresh)
    scheduler = _scheduler(manager, host, threshold=2)

    await scheduler.run_cycle()
    health = manager.health["demo"]
    assert health["status"] == "connected"
    assert health["fail_count"] == 1

    await scheduler.run_cycle()
    health = manager.health["demo"]
    assert health["status"] == "auth_expired"
    assert health["fail_count"] == 2
    assert lock_states == [True, True]
    assert (
        "demo",
        "info",
        "Health status changed from connected to auth_expired.",
    ) in manager.logs


# --- eligibility ------------------------------------------------------------


async def test_only_loaded_pingable_instances_are_pinged() -> None:
    connected = _new_runtime("connected", status=Status.CONNECTED)
    expired = _new_runtime("expired", status=Status.AUTH_EXPIRED)
    disconnected = _new_runtime("disconnected", status=Status.NOT_CONNECTED)
    oauth = _new_runtime("oauth", status=Status.OAUTH_REQUIRED)
    errored = _new_runtime("errored", status=Status.ERROR)
    failed = _new_runtime(
        "failed", status=Status.CONNECTED, load_state=LoadState.LOAD_FAILED
    )
    no_instance = _new_runtime("no-instance", status=Status.CONNECTED, has_instance=False)
    manager = FakeManager(
        connected,
        expired,
        disconnected,
        oauth,
        errored,
        failed,
        no_instance,
    )
    host = FakeHost()
    scheduler = _scheduler(manager, host)

    await scheduler.run_cycle()

    pinged = {call[0] for call in host.calls if call[1] == "ping"}
    assert pinged == {"connected", "expired"}


# --- stale runtime guard ----------------------------------------------------


async def _blocking_ping(
    started: asyncio.Event, release: asyncio.Event, result: Status = Status.CONNECTED
) -> Handler:
    async def ping(_runtime: PluginRuntime) -> Any:
        started.set()
        await release.wait()
        return result

    return ping


async def test_result_is_discarded_when_disconnected_in_flight() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    started = asyncio.Event()
    release = asyncio.Event()
    host.set_handler("demo", "ping", await _blocking_ping(started, release))
    scheduler = _scheduler(manager, host)

    task = asyncio.create_task(scheduler.run_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)
    # Simulate a disconnect completing while the ping worker is in flight.
    manager.health["demo"]["status"] = Status.NOT_CONNECTED.value
    release.set()
    await asyncio.wait_for(task, timeout=2)

    assert manager.applied == []
    assert manager.health["demo"]["status"] == "not_connected"
    assert manager.health["demo"]["last_ping_at"] is None


async def test_result_is_discarded_when_runtime_replaced_in_flight() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    started = asyncio.Event()
    release = asyncio.Event()
    host.set_handler("demo", "ping", await _blocking_ping(started, release))
    scheduler = _scheduler(manager, host)

    task = asyncio.create_task(scheduler.run_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)
    replacement = _new_runtime("demo", generation=2)
    manager.replace("demo", replacement)
    release.set()
    await asyncio.wait_for(task, timeout=2)

    assert manager.applied == []
    assert manager.health["demo"]["last_ping_at"] is None
    assert manager.runtime_for("demo") is replacement


# --- scheduling -------------------------------------------------------------


async def test_cycles_do_not_overlap() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    started = asyncio.Event()
    release = asyncio.Event()
    host.set_handler("demo", "ping", await _blocking_ping(started, release))
    scheduler = _scheduler(manager, host)

    first = asyncio.create_task(scheduler.run_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)

    # The second tick returns immediately rather than starting another cycle.
    await asyncio.wait_for(scheduler.run_cycle(), timeout=0.5)
    assert len(host.calls) == 1

    release.set()
    await asyncio.wait_for(first, timeout=2)
    assert len(host.calls) == 1


async def test_hung_plugin_does_not_block_another() -> None:
    slow = _new_runtime("slow")
    fast = _new_runtime("fast")
    manager = FakeManager(slow, fast)
    host = FakeHost()
    started = asyncio.Event()
    release = asyncio.Event()
    host.set_handler("slow", "ping", await _blocking_ping(started, release))
    host.set_handler("fast", "ping", lambda _runtime: _result(Status.CONNECTED))
    scheduler = _scheduler(manager, host)

    task = asyncio.create_task(scheduler.run_cycle())
    await asyncio.wait_for(started.wait(), timeout=2)
    # The fast plugin is applied even while the slow one is still blocked.
    await asyncio.wait_for(manager.applied_events["fast"].wait(), timeout=2)
    assert manager.health["fast"]["last_ping_at"] is not None
    assert manager.health["slow"]["last_ping_at"] is None

    release.set()
    await asyncio.wait_for(task, timeout=2)
    assert manager.health["slow"]["last_ping_at"] is not None


async def test_start_stop_runs_cycles_and_pings_stop() -> None:
    runtime = _new_runtime("demo")
    manager = FakeManager(runtime)
    host = FakeHost()
    two_pings = asyncio.Event()
    calls = 0

    async def ping(_runtime: PluginRuntime) -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            two_pings.set()
        return Status.CONNECTED

    host.set_handler("demo", "ping", ping)
    scheduler = _scheduler(manager, host)
    scheduler = HealthScheduler(
        cast("PluginManager", manager),
        cast("PluginHost", host),
        interval=0.01,
        timeout=5.0,
        threshold=3,
    )

    await scheduler.start()
    await asyncio.wait_for(two_pings.wait(), timeout=2)
    await scheduler.stop()

    after_stop = len(host.calls)
    await asyncio.sleep(0.05)
    assert len(host.calls) == after_stop


async def _result(value: Any) -> Any:
    return value
