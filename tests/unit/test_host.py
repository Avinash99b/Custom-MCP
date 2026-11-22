"""Unit tests for bounded host-mediated plugin execution and validation."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from custom_mcp.plugins.host import PluginCallError, PluginHost
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


class _StubPlugin(MCPPlugin):
    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)

    def _override(self, method: str, default: Any, *args: Any) -> Any:
        override = getattr(self, f"_override_{method}", None)
        if override is not None:
            return override(*args)
        return default

    def auth_methods(self) -> list[AuthMode]:
        return self._override("auth_methods", [AuthMode.NONE])

    def list_tools(self) -> list[Tool]:
        return self._override("list_tools", [])

    def connect(self) -> ConnectResult:
        return self._override("connect", ConnectResult(status=Status.NOT_CONNECTED))

    def ping(self) -> Status:
        return self._override("ping", Status.NOT_CONNECTED)

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return self._override("invoke_tool", ToolResult.success({}), name, args)


@asynccontextmanager
async def _host(**kwargs: Any) -> AsyncIterator[PluginHost]:
    host = PluginHost(**kwargs)
    try:
        yield host
    finally:
        await host.aclose()


def _context(slug: str = "demo") -> PluginContext:
    return PluginContext(
        slug=slug,
        storage=_Storage(),
        secrets=_Storage(),
        log=lambda level, message: None,
        callback_url=f"https://mcp.example.com/mcp/{slug}/oauth/callback",
    )


def _set_override(plugin: _StubPlugin, name: str, value: Any) -> None:
    """Install a test-only method override without tripping constant-setattr lint."""
    attribute = name if name.startswith("_override_") else f"_override_{name}"
    object.__setattr__(plugin, attribute, value)


def _manifest(slug: str = "demo") -> Manifest:
    return Manifest(
        name=slug,
        version="1.0.0",
        entry="plugin:Plugin",
        auth_modes=(AuthMode.NONE,),
        display_name=slug,
        description="",
    )


def _make(
    slug: str = "demo",
    *,
    plugin: MCPPlugin | None = None,
    sem: asyncio.Semaphore | None = None,
    lock: asyncio.Lock | None = None,
    generation: int = 1,
) -> PluginRuntime:
    context = _context(slug)
    instance = plugin or _StubPlugin(context)
    return PluginRuntime(
        slug=slug,
        manifest=_manifest(slug),
        context=context,
        lock=lock or asyncio.Lock(),
        sem=sem or asyncio.Semaphore(4),
        generation=generation,
        load_state=LoadState.LOADED,
        instance=instance,
    )


async def _expect_error(
    runtime: PluginRuntime, host: PluginHost, method: str, *args: Any
) -> PluginCallError:
    with pytest.raises(PluginCallError) as caught:
        await host.call(runtime, method, *args, timeout=1.0)
    return caught.value


async def test_call_runs_off_loop_and_validates_result() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        seen: list[int] = []

        def invoke(name: str, args: dict[str, Any]) -> ToolResult:
            seen.append(threading.get_ident())
            return ToolResult.success({"echo": args["value"]})

        _set_override(plugin, "_override_invoke_tool", invoke)
        runtime = _make(plugin=plugin)

        result = await host.call(runtime, "invoke_tool", "echo", {"value": 3})
        assert isinstance(result, ToolResult)
        assert result.ok is True
        assert result.result == {"echo": 3}
        assert seen and seen[0] != threading.get_ident()
        assert host.leaked_workers("demo") == 0


async def test_auth_and_tool_validation_success() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        tool = Tool(
            name="echo",
            description="Echo.",
            input_schema={"type": "object", "properties": {}},
        )
        _set_override(plugin, "_override_auth_methods", lambda: [AuthMode.NONE])
        _set_override(plugin, "_override_list_tools", lambda: [tool])
        runtime = _make(plugin=plugin)

        assert await host.call(runtime, "auth_methods") == [AuthMode.NONE]
        tools = await host.call(runtime, "list_tools")
        assert tools == [tool]
        assert PluginHost.validate_tools([tool]) == [tool]


async def test_ping_and_connect_validate_enums() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_ping", lambda: Status.CONNECTED)
        _set_override(plugin, "_override_connect",
            lambda: ConnectResult(status=Status.OAUTH_REQUIRED, message="go"),
        )
        runtime = _make(plugin=plugin)
        assert await host.call(runtime, "ping") is Status.CONNECTED
        connect = await host.call(runtime, "connect")
        assert isinstance(connect, ConnectResult)
        assert connect.status is Status.OAUTH_REQUIRED


async def test_malformed_tool_result_is_normalized() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_invoke_tool", lambda name, args: object())
        runtime = _make(plugin=plugin)
        error = await _expect_error(runtime, host, "invoke_tool", "x", {})
        assert error.kind == "malformed_return"
        assert "secret" not in error.detail_safe


async def test_inconsistent_tool_result_is_malformed() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_invoke_tool",
            lambda name, args: ToolResult(ok=True, result=None, error=None),
        )
        runtime = _make(plugin=plugin)
        error = await _expect_error(runtime, host, "invoke_tool", "x", {})
        assert error.kind == "malformed_return"


async def test_nan_tool_result_is_malformed() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_invoke_tool",
            lambda name, args: ToolResult(ok=True, result={"value": float("nan")}),
        )
        runtime = _make(plugin=plugin)
        error = await _expect_error(runtime, host, "invoke_tool", "x", {})
        assert error.kind == "malformed_return"


async def test_non_object_and_invalid_schemas_are_malformed() -> None:
    for schema in (
        {"type": "string"},
        {"type": "object", "properties": {"x": {"type": "not-a-real-type"}}},
    ):
        async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
            plugin = _StubPlugin(_context())
            _set_override(plugin, "_override_list_tools",
                lambda schema=schema: [
                    Tool(name="bad", description="Bad.", input_schema=schema)
                ],
            )
            runtime = _make(plugin=plugin)
            error = await _expect_error(runtime, host, "list_tools")
            assert error.kind == "malformed_return"


async def test_duplicate_and_invalid_tool_names_are_malformed() -> None:
    for tools in (
        [
            Tool(name="dup", description="a", input_schema={"type": "object"}),
            Tool(name="dup", description="b", input_schema={"type": "object"}),
        ],
        [Tool(name="bad name!", description="a", input_schema={"type": "object"})],
    ):
        async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
            plugin = _StubPlugin(_context())
            _set_override(plugin, "_override_list_tools", lambda tools=tools: tools)
            runtime = _make(plugin=plugin)
            error = await _expect_error(runtime, host, "list_tools")
            assert error.kind == "malformed_return"


async def test_malformed_status_connect_and_auth_are_normalized() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_ping", lambda: "connected")
        _set_override(plugin, "_override_connect", lambda: Status.CONNECTED)
        _set_override(plugin, "_override_auth_methods", lambda: [AuthMode.NONE, AuthMode.NONE])
        runtime = _make(plugin=plugin)
        assert (await _expect_error(runtime, host, "ping")).kind == "malformed_return"
        assert (await _expect_error(runtime, host, "connect")).kind == "malformed_return"
        assert (
            await _expect_error(runtime, host, "auth_methods")
        ).kind == "malformed_return"


async def test_hook_returning_value_is_malformed() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        object.__setattr__(plugin, "shutdown", lambda: "not-none")
        runtime = _make(plugin=plugin)
        error = await _expect_error(runtime, host, "shutdown")
        assert error.kind == "malformed_return"


async def test_exception_is_normalized_without_leaking_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            raise RuntimeError("super-secret-credential")

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        with caplog.at_level(logging.ERROR, logger="custom_mcp.plugins.host"):
            error = await _expect_error(runtime, host, "invoke_tool", "x", {})
        assert error.kind == "exception"
        assert error.safe_message == error.detail_safe
        assert "super-secret-credential" not in error.detail_safe
        assert "super-secret-credential" not in caplog.text


async def test_explicit_timeout_reports_timeout_and_leak() -> None:
    started = threading.Event()
    release = threading.Event()
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def slow(name: str, args: dict[str, Any]) -> ToolResult:
            started.set()
            release.wait(5)
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", slow)
        runtime = _make(plugin=plugin)
        try:
            with pytest.raises(PluginCallError) as caught:
                await host.call(runtime, "invoke_tool", "x", {}, timeout=0.05)
            assert caught.value.kind == "timeout"
            assert host.leaked_workers("demo") == 1
            assert await asyncio.to_thread(started.wait, 2)
        finally:
            release.set()
        assert await host.wait_for_idle("demo", 2)
        assert host.leaked_workers("demo") == 0


async def test_capacity_wait_obeys_call_timeout_and_leaks_no_slot() -> None:
    """A pending call must time out at its deadline instead of waiting for the
    occupying worker to finish, and the timed-out waiter must not consume a slot."""
    started = threading.Event()
    release = threading.Event()
    async with _host(worker_threads=4, per_plugin_concurrency=1) as host:
        plugin = _StubPlugin(_context())

        def hold(name: str, args: dict[str, Any]) -> ToolResult:
            started.set()
            release.wait(5)
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", hold)
        runtime = _make(plugin=plugin, sem=asyncio.Semaphore(1))
        first = asyncio.create_task(host.call(runtime, "invoke_tool", "fill", {}))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            with pytest.raises(PluginCallError) as caught:
                await host.call(runtime, "invoke_tool", "queued", {}, timeout=0.1)
            assert caught.value.kind == "timeout"
            # The live call still owns the only slot; nothing leaked or was
            # double-counted by the cancelled capacity waiter.
            assert runtime.sem.locked()
            assert host.leaked_workers("demo") == 0
        finally:
            release.set()
        await first
        assert await host.wait_for_idle("demo", 2)
        # Capacity recovered after the live call finished.
        assert await host.call(runtime, "ping", timeout=1.0) is Status.NOT_CONNECTED


async def test_capacity_wait_timeout_total_budget_under_occupancy() -> None:
    """The capacity wait must be counted inside the call's real deadline even
    when every slot stays occupied for much longer than the timeout."""
    entered = threading.Event()
    release = threading.Event()
    started_count = 0
    count_lock = threading.Lock()

    def hold(name: str, args: dict[str, Any]) -> ToolResult:
        nonlocal started_count
        with count_lock:
            started_count += 1
        entered.set()
        release.wait(5)
        return ToolResult.success({})

    async with _host(worker_threads=4, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())
        _set_override(plugin, "_override_invoke_tool", hold)
        runtime = _make(plugin=plugin, sem=asyncio.Semaphore(2))
        fills = [
            asyncio.create_task(host.call(runtime, "invoke_tool", "fill", {}))
            for _ in range(2)
        ]
        try:
            await asyncio.to_thread(entered.wait, 2)
            # Both fill workers must be inside the plugin before the queued
            # call starts its bounded capacity wait.
            deadline = asyncio.get_running_loop().time() + 2
            while True:
                with count_lock:
                    if started_count >= 2:
                        break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("both fill workers did not start")
                await asyncio.sleep(0.001)
            loop = asyncio.get_running_loop()
            begun = loop.time()
            with pytest.raises(PluginCallError) as caught:
                await host.call(runtime, "invoke_tool", "queued", {}, timeout=0.2)
            elapsed = loop.time() - begun
            assert caught.value.kind == "timeout"
            # The deadline, not the 5s worker release, ended the wait.
            assert elapsed < 1.0
        finally:
            release.set()
            for task in fills:
                await asyncio.wait_for(task, 2)


async def test_overloaded_before_new_call_when_leaked() -> None:
    started = threading.Event()
    release = threading.Event()
    async with _host(
        worker_threads=2, per_plugin_concurrency=1
    ) as host:
        plugin = _StubPlugin(_context())

        def slow(name: str, args: dict[str, Any]) -> ToolResult:
            started.set()
            release.wait(5)
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", slow)
        runtime = _make(plugin=plugin)
        try:
            with pytest.raises(PluginCallError) as caught:
                await host.call(runtime, "invoke_tool", "x", {}, timeout=0.05)
            assert caught.value.kind == "timeout"
            assert await asyncio.to_thread(started.wait, 2)
            with pytest.raises(PluginCallError) as overloaded:
                await host.call(runtime, "ping", timeout=1.0)
            assert overloaded.value.kind == "plugin_overloaded"
        finally:
            release.set()
        assert await host.wait_for_idle("demo", 2)
        assert await host.call(runtime, "ping") is Status.NOT_CONNECTED


async def test_caller_cancellation_tracks_unfinished_worker() -> None:
    started = threading.Event()
    release = threading.Event()
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def slow(name: str, args: dict[str, Any]) -> ToolResult:
            started.set()
            release.wait(5)
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", slow)
        runtime = _make(plugin=plugin)
        task = asyncio.create_task(host.call(runtime, "invoke_tool", "x", {}))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert host.leaked_workers("demo") == 1
        finally:
            release.set()
        assert await host.wait_for_idle("demo", 2)
        assert host.leaked_workers("demo") == 0


async def test_per_plugin_concurrency_queues_second_call() -> None:
    first_started = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    async with _host(worker_threads=2, per_plugin_concurrency=1) as host:
        plugin = _StubPlugin(_context())

        def invoke(name: str, args: dict[str, Any]) -> ToolResult:
            if name == "first":
                first_started.set()
                first_release.wait(5)
            else:
                second_entered.set()
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", invoke)
        runtime = _make(plugin=plugin, sem=asyncio.Semaphore(1))
        first = asyncio.create_task(host.call(runtime, "invoke_tool", "first", {}))
        assert await asyncio.to_thread(first_started.wait, 2)
        second = asyncio.create_task(host.call(runtime, "invoke_tool", "second", {}))
        try:
            assert await asyncio.to_thread(second_entered.wait, 0.2) is False
        finally:
            first_release.set()
        await first
        await second
        assert second_entered.is_set()


async def test_queued_call_cancellation_does_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_started = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    loop = asyncio.get_running_loop()
    async with _host(worker_threads=1, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def invoke(name: str, args: dict[str, Any]) -> ToolResult:
            if name == "first":
                first_started.set()
                first_release.wait(5)
            else:
                second_entered.set()
            return ToolResult.success({})

        _set_override(plugin, "_override_invoke_tool", invoke)
        runtime = _make(plugin=plugin)

        original_submit = host._executor.submit
        submit_count = 0
        second_submitted = asyncio.Event()

        def tracking_submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            nonlocal submit_count
            future = original_submit(fn, *args, **kwargs)
            submit_count += 1
            if submit_count == 2:
                loop.call_soon_threadsafe(second_submitted.set)
            return future

        monkeypatch.setattr(host._executor, "submit", tracking_submit)

        first = asyncio.create_task(host.call(runtime, "invoke_tool", "first", {}))
        assert await asyncio.to_thread(first_started.wait, 2)
        second = asyncio.create_task(host.call(runtime, "invoke_tool", "second", {}))
        await asyncio.wait_for(second_submitted.wait(), 2)
        second.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await second
            assert host.leaked_workers("demo") == 0
        finally:
            first_release.set()
        await first
        assert not second_entered.is_set()
        _set_override(plugin, "_override_invoke_tool", lambda name, args: ToolResult.success({}))
        third = await host.call(runtime, "invoke_tool", "third", {})
        assert third.ok is True
        assert not second_entered.is_set()


async def test_shared_leak_counter_survives_runtime_generation() -> None:
    started = threading.Event()
    release = threading.Event()
    async with _host(worker_threads=2, per_plugin_concurrency=1) as host:
        first_plugin = _StubPlugin(_context())

        def slow(name: str, args: dict[str, Any]) -> ToolResult:
            started.set()
            release.wait(5)
            return ToolResult.success({})

        _set_override(first_plugin, "invoke_tool", slow)
        sem = asyncio.Semaphore(1)
        lock = asyncio.Lock()
        first = _make(plugin=first_plugin, sem=sem, lock=lock, generation=1)
        second = _make(plugin=_StubPlugin(_context()), sem=sem, lock=lock, generation=2)
        try:
            with pytest.raises(PluginCallError) as caught:
                await host.call(first, "invoke_tool", "x", {}, timeout=0.05)
            assert caught.value.kind == "timeout"
            assert await asyncio.to_thread(started.wait, 2)
            with pytest.raises(PluginCallError) as overloaded:
                await host.call(second, "ping", timeout=1.0)
            assert overloaded.value.kind == "plugin_overloaded"
        finally:
            release.set()
        assert await host.wait_for_idle("demo", 2)
        assert await host.call(second, "ping") is Status.NOT_CONNECTED


class _BadRepr(Exception):
    def __repr__(self) -> str:
        raise ValueError("private-credential")


class _SlowRepr(Exception):
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def __repr__(self) -> str:
        self._release.wait(5)
        return "slow-repr"


async def test_repr_failure_is_normalized_off_loop() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            raise _BadRepr()

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        with pytest.raises(PluginCallError) as caught:
            await host.call(runtime, "invoke_tool", "x", {})
        assert caught.value.kind == "exception"
        assert "private-credential" not in caught.value.detail_safe
        assert "private-credential" not in (caught.value.detail_log or "")


async def test_slow_repr_hangs_worker_not_loop_and_leaks() -> None:
    release = threading.Event()
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            raise _SlowRepr(release)

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        try:
            with pytest.raises(PluginCallError) as caught:
                await host.call(runtime, "invoke_tool", "x", {}, timeout=0.05)
            assert caught.value.kind == "timeout"
            assert host.leaked_workers("demo") == 1
        finally:
            release.set()
        assert await host.wait_for_idle("demo", 2)
        assert host.leaked_workers("demo") == 0


async def test_spoofed_exception_attributes_are_not_trusted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages: list[str] = []

    async def log(slug: str, level: str, message: str) -> None:
        messages.append(message)

    async with _host(
        worker_threads=2, per_plugin_concurrency=2, log=log
    ) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            exc = RuntimeError("sentinel-private")
            exc.__dict__["safe_message"] = "sentinel-private"
            exc.__dict__["detail_safe"] = "sentinel-private"
            raise exc

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        with caplog.at_level(logging.ERROR, logger="custom_mcp.plugins.host"), (
            pytest.raises(PluginCallError)
        ) as caught:
            await host.call(runtime, "invoke_tool", "x", {})
        assert caught.value.detail_safe == "Plugin raised an exception."
        assert "sentinel-private" not in caplog.text
        assert messages
        assert "sentinel-private" not in messages[-1]
        assert "sentinel-private" in (caught.value.detail_log or "")


async def test_blocking_safe_message_property_is_never_probed() -> None:
    probed = threading.Event()

    class _BlockingAttr(Exception):
        def __init__(self) -> None:
            super().__init__("sentinel-private")

        @property
        def safe_message(self) -> str:
            probed.set()
            return "sentinel-private"

    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            raise _BlockingAttr()

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        with pytest.raises(PluginCallError) as caught:
            await host.call(runtime, "invoke_tool", "x", {}, timeout=1.0)
        assert caught.value.detail_safe == "Plugin raised an exception."
        assert not probed.is_set()


async def test_abandoned_worker_exception_is_consumed() -> None:
    started = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    seen: list[Any] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
            plugin = _StubPlugin(_context())

            def late_boom(name: str, args: dict[str, Any]) -> ToolResult:
                started.set()
                release.wait(5)
                raise ValueError("private-credential")

            _set_override(plugin, "_override_invoke_tool", late_boom)
            runtime = _make(plugin=plugin)
            try:
                with pytest.raises(PluginCallError) as caught:
                    await host.call(runtime, "invoke_tool", "x", {}, timeout=0.05)
                assert caught.value.kind == "timeout"
                assert host.leaked_workers("demo") == 1
            finally:
                release.set()
            assert await host.wait_for_idle("demo", 2)
            assert host.leaked_workers("demo") == 0
        assert seen == []
    finally:
        loop.set_exception_handler(previous)


async def test_log_callback_failure_is_generic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_log(slug: str, level: str, message: str) -> None:
        raise ValueError("private-credential")

    async with _host(worker_threads=2, per_plugin_concurrency=2, log=failing_log) as host:
        plugin = _StubPlugin(_context())

        def boom(name: str, args: dict[str, Any]) -> ToolResult:
            raise RuntimeError("boom")

        _set_override(plugin, "_override_invoke_tool", boom)
        runtime = _make(plugin=plugin)
        with caplog.at_level(logging.WARNING, logger="custom_mcp.plugins.host"), (
            pytest.raises(PluginCallError)
        ) as caught:
            await host.call(runtime, "invoke_tool", "x", {})
        assert caught.value.kind == "exception"
        assert "private-credential" not in caplog.text


async def test_call_callable_off_loop_and_exception() -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        runtime = _make()
        seen: list[int] = []

        def work(value: int) -> int:
            seen.append(threading.get_ident())
            return value + 1

        assert await host.call_callable(runtime, work, 2) == 3
        assert seen and seen[0] != threading.get_ident()

        def boom() -> None:
            raise ValueError("boom")

        with pytest.raises(PluginCallError) as caught:
            await host.call_callable(runtime, boom)
        assert caught.value.kind == "exception"



