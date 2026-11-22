"""Unit tests for isolated plugin import, construction and unloading."""

from __future__ import annotations

import asyncio
import builtins
import io
import logging
import sys
import threading
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.loader import PluginLoader, PluginLoadError
from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import AuthMode, PluginContext, Status
from tests.runtime_cases import builders

SLOW_IMPORT_SOURCE = '''
import builtins

_import_started = getattr(builtins, "TEST_IMPORT_STARTED", None)
_import_release = getattr(builtins, "TEST_IMPORT_RELEASE", None)
if _import_started is not None:
    _import_started.set()
if _import_release is not None:
    _import_release.wait(5)

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

SLOW_CONSTRUCTOR_SOURCE = '''
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        started = ctx.storage.get("started")
        release = ctx.storage.get("release")
        if started is not None:
            started.set()
        if release is not None:
            release.wait(5)

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

SLOW_PING_SOURCE = '''
import threading

PING_STARTED = threading.Event()
PING_RELEASE = threading.Event()

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        PING_STARTED.set()
        PING_RELEASE.wait(5)
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''


FAILED_INSTANCE_AUTH_SOURCE = '''
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("open", True)

    def auth_methods(self):
        raise RuntimeError("auth boom")

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})

    def shutdown(self):
        self.ctx.storage.set("open", False)
        self.ctx.storage.set("closed", True)
'''

FAILED_INSTANCE_LIST_SOURCE = '''
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("open", True)

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [Tool(name="bad", description="bad", input_schema={"type": "string"})]

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})

    def shutdown(self):
        self.ctx.storage.set("open", False)
        self.ctx.storage.set("closed", True)
'''

FAILED_INSTANCE_SLOW_SHUTDOWN_SOURCE = '''
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)

    def auth_methods(self):
        raise RuntimeError("auth boom")

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})

    def shutdown(self):
        started = self.ctx.storage.get("shutdown_started")
        release = self.ctx.storage.get("shutdown_release")
        if started is not None:
            started.set()
        if release is not None:
            release.wait(5)
'''


AUTH_SPOOF_SOURCE = '''
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        exc = RuntimeError("sentinel-private")
        exc.__dict__["safe_message"] = "sentinel-private"
        exc.__dict__["detail_safe"] = "sentinel-private"
        raise exc

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

NOT_A_PLUGIN_SOURCE = '''
class FixturePlugin:
    pass
'''


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


@asynccontextmanager
async def _host(**kwargs: Any) -> AsyncIterator[PluginHost]:
    host = PluginHost(**kwargs)
    try:
        yield host
    finally:
        await host.aclose()


def _extract(tmp_path: Path, zip_bytes: bytes, name: str) -> Path:
    destination = tmp_path / name
    destination.mkdir()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        archive.extractall(destination)
    return destination


def _manifest(name: str) -> Manifest:
    return Manifest(
        name=name,
        version=builders.DEFAULT_VERSION,
        entry=builders.DEFAULT_ENTRY,
        auth_modes=(AuthMode.NONE,),
        display_name=name,
        description="",
    )


def _runtime(
    name: str,
    *,
    context: PluginContext | None = None,
    sem: asyncio.Semaphore | None = None,
    lock: asyncio.Lock | None = None,
    generation: int = 1,
) -> PluginRuntime:
    resolved = context or _context(name)
    return PluginRuntime(
        slug=name,
        manifest=_manifest(name),
        context=resolved,
        lock=lock or asyncio.Lock(),
        sem=sem or asyncio.Semaphore(4),
        generation=generation,
    )


def _context(name: str) -> PluginContext:
    return PluginContext(
        slug=name,
        storage=_Storage(),
        secrets=_Storage(),
        log=lambda level, message: None,
        callback_url=f"https://mcp.example.com/mcp/{name}/oauth/callback",
    )


def _prefix(name: str) -> str:
    return f"mcp_plugin_{name}"


async def test_load_hello_calls_tools_and_unloads(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        zip_bytes = builders.make_runtime_zip(
            "hello", {"plugin.py": builders.HELLO_CLONE_SOURCE}
        )
        package_dir = _extract(tmp_path, zip_bytes, "hello-pkg")
        runtime = _runtime("hello")
        loop_thread = threading.get_ident()

        state = await loader.load(runtime, package_dir)
        assert state is LoadState.LOADED
        assert runtime.load_error is None
        assert runtime.instance is not None
        assert [tool.name for tool in runtime.tools] == ["echo"]
        assert runtime.auth_methods == [AuthMode.NONE]
        assert (package_dir / "__init__.py").is_file()

        result = await host.call(runtime, "invoke_tool", "echo", {"text": "hi"})
        assert result.ok is True
        assert result.result == {"text": "hi"}
        assert threading.get_ident() == loop_thread

        await loader.unload(runtime)
        assert runtime.load_state is LoadState.UNLOADING
        assert runtime.instance is None
        assert runtime.tools == []
        assert _prefix("hello") not in sys.modules


async def test_load_supports_relative_imports(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        zip_bytes = builders.make_runtime_zip(
            "rel-import",
            {
                "plugin.py": builders.RELATIVE_IMPORT_SOURCE,
                "helpers/__init__.py": builders.HELPERS_SOURCE,
            },
        )
        package_dir = _extract(tmp_path, zip_bytes, "rel")
        runtime = _runtime("rel-import", context=_context("rel-import"))
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        result = await host.call(
            runtime, "invoke_tool", "greet", {"name": "Ada"}
        )
        assert result.result == {"text": "Hello, Ada!"}
        assert _prefix("rel-import") in sys.modules
        await loader.unload(runtime)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("crash-ctor", builders.CRASHING_CONSTRUCTOR_SOURCE),
        ("crash-auth", builders.CRASHING_AUTH_SOURCE),
        ("crash-list", builders.CRASHING_LIST_SOURCE),
        ("auth-mismatch", builders.AUTH_MISMATCH_SOURCE),
        ("dup-tools", builders.DUPLICATE_TOOL_NAMES_SOURCE),
        ("bad-toolname", builders.INVALID_TOOL_NAME_SOURCE),
        ("bad-schema-type", builders.SCHEMA_NOT_OBJECT_SOURCE),
        ("invalid-schema", builders.INVALID_SCHEMA_SOURCE),
    ],
)
async def test_load_contract_failures_are_visible_and_clean(
    tmp_path: Path, name: str, source: str
) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        package_dir = _extract(
            tmp_path, builders.make_runtime_zip(name, {"plugin.py": source}), name
        )
        runtime = _runtime(name)
        state = await loader.load(runtime, package_dir)
        assert state is LoadState.LOAD_FAILED
        assert runtime.load_state is LoadState.LOAD_FAILED
        assert runtime.load_error
        assert runtime.instance is None
        assert runtime.tools == []
        assert _prefix(name) not in sys.modules


async def test_spoofed_loader_exception_attrs_are_not_trusted(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("auth-spoof", {"plugin.py": AUTH_SPOOF_SOURCE}),
            "auth-spoof",
        )
        runtime = _runtime("auth-spoof")
        assert await loader.load(runtime, package_dir) is LoadState.LOAD_FAILED
        assert runtime.load_error == "Plugin raised an exception."
        assert "sentinel-private" not in runtime.load_error


async def test_loader_contract_messages_remain_precise(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        not_plugin_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "not-plugin", {"plugin.py": NOT_A_PLUGIN_SOURCE}
            ),
            "not-plugin",
        )
        not_plugin = _runtime("not-plugin")
        assert await loader.load(not_plugin, not_plugin_dir) is LoadState.LOAD_FAILED
        assert "MCPPlugin subclass" in (not_plugin.load_error or "")

        auth_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "auth-mismatch-two", {"plugin.py": builders.AUTH_MISMATCH_SOURCE}
            ),
            "auth-mismatch-two",
        )
        auth = _runtime("auth-mismatch-two")
        assert await loader.load(auth, auth_dir) is LoadState.LOAD_FAILED
        assert "auth modes" in (auth.load_error or "")


async def test_crashing_invoke_fails_at_call_not_load(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "crash-invoke", {"plugin.py": builders.CRASHING_INVOKE_SOURCE}
            ),
            "crash-invoke",
        )
        runtime = _runtime("crash-invoke")
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        with pytest.raises(PluginCallError) as caught:
            await host.call(runtime, "invoke_tool", "echo", {"text": "x"})
        assert caught.value.kind == "exception"
        assert "boom" not in caught.value.detail_safe
        await loader.unload(runtime)


async def test_malformed_tool_result_fails_only_when_called(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "bad-result", {"plugin.py": builders.MALFORMED_TOOL_RESULT_SOURCE}
            ),
            "bad-result",
        )
        runtime = _runtime("bad-result")
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        with pytest.raises(PluginCallError) as caught:
            await host.call(runtime, "invoke_tool", "echo", {"text": "x"})
        assert caught.value.kind == "malformed_return"
        await loader.unload(runtime)


async def test_failed_plugin_does_not_break_another(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        bad_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "bad-one", {"plugin.py": builders.CRASHING_CONSTRUCTOR_SOURCE}
            ),
            "bad-one",
        )
        good_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("good-one", {"plugin.py": builders.HELLO_CLONE_SOURCE}),
            "good-one",
        )
        bad = _runtime("bad-one")
        good = _runtime("good-one")
        assert await loader.load(bad, bad_dir) is LoadState.LOAD_FAILED
        assert await loader.load(good, good_dir) is LoadState.LOADED
        assert _prefix("bad-one") not in sys.modules
        assert _prefix("good-one") in sys.modules
        assert [tool.name for tool in good.tools] == ["echo"]
        await loader.unload(good)


async def test_unload_does_not_remove_prefix_collision(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        demo_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("demo", {"plugin.py": builders.HELLO_CLONE_SOURCE}),
            "demo",
        )
        extra_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "demo-extra", {"plugin.py": builders.HELLO_CLONE_SOURCE}
            ),
            "demo-extra",
        )
        demo = _runtime("demo")
        extra = _runtime("demo-extra")
        assert await loader.load(demo, demo_dir) is LoadState.LOADED
        assert await loader.load(extra, extra_dir) is LoadState.LOADED

        await loader.unload(demo)
        assert _prefix("demo") not in sys.modules
        assert f"{_prefix('demo')}.plugin" not in sys.modules
        assert _prefix("demo-extra") in sys.modules
        assert f"{_prefix('demo-extra')}.plugin" in sys.modules
        await loader.unload(extra)


async def test_unload_runs_bounded_shutdown_and_records_marker(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2, shutdown_timeout=1)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "shutdown-mark", {"plugin.py": builders.SHUTDOWN_MARKER_SOURCE}
            ),
            "shutdown-mark",
        )
        context = _context("shutdown-mark")
        runtime = _runtime("shutdown-mark", context=context)
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        instance = runtime.instance
        assert instance is not None
        await loader.unload(runtime)
        assert instance.ctx.storage.get("shutdown") is True
        assert runtime.instance is None
        assert _prefix("shutdown-mark") not in sys.modules


async def test_constructor_and_import_run_off_loop(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "thread-ids", {"plugin.py": builders.CONSTRUCTOR_THREAD_ID_SOURCE}
            ),
            "thread-ids",
        )
        context = _context("thread-ids")
        runtime = _runtime("thread-ids", context=context)
        loop_thread = threading.get_ident()
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        import_thread = context.storage.get("import_thread_id")
        constructor_thread = context.storage.get("constructor_thread_id")
        assert import_thread != loop_thread
        assert constructor_thread != loop_thread
        await loader.unload(runtime)


async def test_slow_constructor_late_cleanup_keeps_new_generation(
    tmp_path: Path,
) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=4) as host:
        loader = PluginLoader(host, load_timeout=0.2)
        old_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("slow-ctor", {"plugin.py": SLOW_CONSTRUCTOR_SOURCE}),
            "slow-ctor-old",
        )
        new_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "slow-ctor", {"plugin.py": builders.HELLO_CLONE_SOURCE}
            ),
            "slow-ctor-new",
        )
        started = threading.Event()
        release = threading.Event()
        context = _context("slow-ctor")
        context.storage.set("started", started)
        context.storage.set("release", release)
        sem = asyncio.Semaphore(4)
        lock = asyncio.Lock()
        old = _runtime("slow-ctor", context=context, sem=sem, lock=lock, generation=1)
        task = asyncio.create_task(loader.load(old, old_dir))
        new = _runtime(
            "slow-ctor", context=_context("slow-ctor"), sem=sem, lock=lock, generation=2
        )
        new_module = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await task is LoadState.LOAD_FAILED
            assert old.instance is None
            assert await loader.load(new, new_dir) is LoadState.LOADED
            new_module = sys.modules[f"{_prefix('slow-ctor')}.plugin"]
            assert [tool.name for tool in new.tools] == ["echo"]
        finally:
            release.set()
        assert await host.wait_for_idle("slow-ctor", 3)
        assert new_module is not None
        assert sys.modules.get(f"{_prefix('slow-ctor')}.plugin") is new_module
        assert new.load_state is LoadState.LOADED
        await loader.unload(new)


async def test_slow_import_timeout_cleans_late_modules(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=4) as host:
        loader = PluginLoader(host, load_timeout=0.2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("slow-import", {"plugin.py": SLOW_IMPORT_SOURCE}),
            "slow-import",
        )
        started = threading.Event()
        release = threading.Event()
        setattr(builtins, "TEST_IMPORT_STARTED", started)  # noqa: B010
        setattr(builtins, "TEST_IMPORT_RELEASE", release)  # noqa: B010
        runtime = _runtime("slow-import")
        task = asyncio.create_task(loader.load(runtime, package_dir))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await task is LoadState.LOAD_FAILED
            assert runtime.instance is None
            assert _prefix("slow-import") not in sys.modules
        finally:
            release.set()
            delattr(builtins, "TEST_IMPORT_STARTED")
            delattr(builtins, "TEST_IMPORT_RELEASE")
        assert await host.wait_for_idle("slow-import", 3)
        assert _prefix("slow-import") not in sys.modules
        assert runtime.instance is None


def test_late_cleanup_is_identity_scoped() -> None:
    import types

    from custom_mcp.plugins import loader as loader_module

    name = "mcp_plugin_demo"
    old = types.ModuleType(name)
    new = types.ModuleType(name)
    sys.modules[name] = new
    try:
        loader_module._remove_created({name: old})
        assert sys.modules[name] is new
    finally:
        sys.modules.pop(name, None)


async def test_shutdown_timeout_bounds_occupied_semaphore(tmp_path: Path) -> None:
    async with _host(
        worker_threads=2, per_plugin_concurrency=1
    ) as host:
        loader = PluginLoader(host, load_timeout=2, shutdown_timeout=0.2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip("slow-plugin", {"plugin.py": SLOW_PING_SOURCE}),
            "slow-ping",
        )
        runtime = _runtime("slow-plugin", sem=asyncio.Semaphore(1))
        assert await loader.load(runtime, package_dir) is LoadState.LOADED
        module = sys.modules[f"{_prefix('slow-plugin')}.plugin"]
        ping_task = asyncio.create_task(host.call(runtime, "ping"))
        assert await asyncio.to_thread(module.PING_STARTED.wait, 2)
        try:
            await asyncio.wait_for(loader.unload(runtime), timeout=2)
            assert runtime.instance is None
        finally:
            module.PING_RELEASE.set()
        await ping_task
        await host.wait_for_idle("slow-plugin", 2)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("failed-auth", FAILED_INSTANCE_AUTH_SOURCE),
        ("failed-list", FAILED_INSTANCE_LIST_SOURCE),
    ],
)
async def test_failed_load_shuts_down_constructed_instance(
    tmp_path: Path, name: str, source: str
) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2, shutdown_timeout=1)
        package_dir = _extract(
            tmp_path, builders.make_runtime_zip(name, {"plugin.py": source}), name
        )
        context = _context(name)
        runtime = _runtime(name, context=context)
        assert await loader.load(runtime, package_dir) is LoadState.LOAD_FAILED
        assert runtime.load_state is LoadState.LOAD_FAILED
        assert runtime.instance is None
        assert context.storage.get("open") is False
        assert context.storage.get("closed") is True
        assert "shutdown" not in (runtime.load_error or "").lower()


async def test_ctx_identity_check_is_offloop_and_bounded(tmp_path: Path) -> None:
    source = '''
import time

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def ctx(self):
        time.sleep(0.12)
        return self._ctx

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=0.03)
        package_dir = _extract(
            tmp_path, builders.make_runtime_zip("slow-ctx", {"plugin.py": source}), "slow-ctx"
        )
        runtime = _runtime("slow-ctx")
        assert await asyncio.wait_for(loader.load(runtime, package_dir), 2) is (
            LoadState.LOAD_FAILED
        )
        assert runtime.instance is None
        assert _prefix("slow-ctx") not in sys.modules
        assert await host.wait_for_idle("slow-ctx", 2)


async def test_failed_instance_shutdown_is_bounded(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2, shutdown_timeout=0.2)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "slow-shutdown", {"plugin.py": FAILED_INSTANCE_SLOW_SHUTDOWN_SOURCE}
            ),
            "slow-shutdown",
        )
        started = threading.Event()
        release = threading.Event()
        context = _context("slow-shutdown")
        context.storage.set("shutdown_started", started)
        context.storage.set("shutdown_release", release)
        runtime = _runtime("slow-shutdown", context=context)
        try:
            assert await asyncio.wait_for(loader.load(runtime, package_dir), 2) is (
                LoadState.LOAD_FAILED
            )
            assert await asyncio.to_thread(started.wait, 2)
            assert runtime.instance is None
        finally:
            release.set()
        assert await host.wait_for_idle("slow-shutdown", 2)


async def test_injected_callbacks_and_optional_load_error(tmp_path: Path) -> None:
    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        logs: list[tuple[str, str, str]] = []

        async def sanitize(slug: str, text: str) -> str:
            return f"safe:{slug}"

        async def log(slug: str, level: str, message: str) -> None:
            logs.append((slug, level, message))

        loader = PluginLoader(
            host, load_timeout=2, sanitize_error=sanitize, log=log
        )
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "cb-bad", {"plugin.py": builders.CRASHING_CONSTRUCTOR_SOURCE}
            ),
            "cb-bad",
        )
        runtime = _runtime("cb-bad")
        assert await loader.load(runtime, package_dir) is LoadState.LOAD_FAILED
        assert runtime.load_error == "safe:cb-bad"
        assert logs and logs[0][0] == "cb-bad" and logs[0][1] == "error"

        other = _runtime("cb-bad")
        with pytest.raises(PluginLoadError) as caught:
            await loader.load(other, package_dir, raise_on_error=True)
        assert caught.value.safe_message == "safe:cb-bad"


async def test_loader_log_callback_failure_is_generic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing_log(slug: str, level: str, message: str) -> None:
        raise ValueError("private-credential")

    async with _host(worker_threads=2, per_plugin_concurrency=2) as host:
        loader = PluginLoader(host, load_timeout=2, log=failing_log)
        package_dir = _extract(
            tmp_path,
            builders.make_runtime_zip(
                "log-fail", {"plugin.py": builders.CRASHING_CONSTRUCTOR_SOURCE}
            ),
            "log-fail",
        )
        runtime = _runtime("log-fail")
        with caplog.at_level(logging.WARNING, logger="custom_mcp.plugins.loader"):
            assert await loader.load(runtime, package_dir) is LoadState.LOAD_FAILED
        assert "private-credential" not in caplog.text


async def test_status_enum_and_plugin_call_error_public_fields() -> None:
    assert Status.NOT_CONNECTED.value == "not_connected"
    error = PluginCallError("timeout", "Timed out.", detail_log="internal repr")
    assert error.kind == "timeout"
    assert error.safe_message == "Timed out."
    assert error.detail_log == "internal repr"
