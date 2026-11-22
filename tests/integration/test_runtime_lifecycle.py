"""Phase 2 runtime lifecycle integration tests.

These exercise the assembled stack (``create_app`` default host/loader/storage/
logs + the manager) end to end with a real fake-Redis pair and real worker
threads. Fixture plugin sources come from ``tests/runtime_cases/builders.py``;
the hello example is built from ``plugin_examples/hello/``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.loader import PluginLoader
from custom_mcp.storage.manager import StorageManager
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.helpers import (
    TEST_BASE_URL,
    TEST_SECRET,
    build_env,
    build_hello_zip,
)
from tests.runtime_cases.builders import fixture_cases, make_runtime_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}

_CONNECT_ERROR_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        schema = {"type": "object", "properties": {}}
        return [Tool(name="echo", description="Echo.", input_schema=schema)]

    def connect(self):
        raise RuntimeError("connect boom")

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({"ok": True})
'''

_MARKER_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

MARKERS = []


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")

    def shutdown(self):
        MARKERS.append("shutdown")

    def on_disconnect(self):
        MARKERS.append("on_disconnect")
'''

_API_KEY_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        return []

    def connect(self):
        count = self.ctx.storage.get("connects", 0)
        self.ctx.storage.set("connects", int(count) + 1)
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_SLOW_SHUTDOWN_SOURCE = '''\
import time

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")

    def shutdown(self):
        time.sleep(0.5)
'''

_CRASHING_CONSTRUCTOR_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        raise RuntimeError("constructor boom")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
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
async def life_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with _running(app) as client:
        yield client


async def _import(
    client: httpx.AsyncClient,
    data: bytes,
    *,
    mode: str = "reject",
) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", data, "application/zip")},
        params={"mode": mode},
        headers=AUTH,
    )


async def _detail(client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    response = await client.get(f"/api/plugins/{slug}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    return body


def _tool_names(body: dict[str, Any]) -> set[str]:
    return {str(tool["name"]) for tool in body["tools"]}


# --- load, cache and worker execution ---------------------------------------


async def test_install_loads_hello_and_caches_tools(
    life_client: httpx.AsyncClient,
) -> None:
    response = await _import(life_client, build_hello_zip())

    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "hello"
    assert body["load_state"] == "loaded"
    assert body["load_error"] is None
    assert body["status"] == "connected"
    assert _tool_names(body) == {"echo", "add"}
    for tool in body["tools"]:
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


async def test_hello_tool_invokes_through_host(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, build_hello_zip())
    manager = app.state.plugin_manager
    runtime = manager.runtime_for("hello")
    assert runtime is not None
    assert runtime.instance is not None
    assert runtime.tools

    result = await app.state.plugin_host.call(
        runtime, "invoke_tool", "echo", {"text": "hi"}
    )
    assert result.ok is True
    assert result.result == {"text": "hi"}


_THREAD_TOOL_SOURCE = '''\
import threading

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        schema = {"type": "object", "properties": {}}
        return [Tool(name="who", description="Thread.", input_schema=schema)]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        self.ctx.storage.set("invoke_thread_id", threading.get_ident())
        return ToolResult.success({"thread": threading.get_ident()})
'''


async def test_invoke_tool_runs_on_a_worker_thread(
    app: Starlette, life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    response = await _import(
        life_client, make_runtime_zip("who", {"plugin.py": _THREAD_TOOL_SOURCE})
    )
    assert response.status_code == 201
    runtime = app.state.plugin_manager.runtime_for("who")
    assert runtime is not None

    main_thread = threading.get_ident()
    result = await app.state.plugin_host.call(runtime, "invoke_tool", "who", {})
    assert result.ok is True
    assert int(result.result["thread"]) != main_thread
    stored = await redis_async.get("plugin:who:data:invoke_thread_id")
    assert stored is not None and int(stored) != main_thread


async def test_constructor_and_import_run_on_worker_threads(
    life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    response = await _import(life_client, fixture_cases()["constructor_thread_id"])
    assert response.status_code == 201
    assert response.json()["slug"] == "thread-ids"
    assert response.json()["load_state"] == "loaded"

    main_thread = threading.get_ident()
    imported = await redis_async.get("plugin:thread-ids:data:import_thread_id")
    constructed = await redis_async.get("plugin:thread-ids:data:constructor_thread_id")
    assert imported is not None and int(imported) != main_thread
    assert constructed is not None and int(constructed) != main_thread


async def test_relative_import_subpackage_loads(life_client: httpx.AsyncClient) -> None:
    response = await _import(life_client, fixture_cases()["relative_import_subpackage"])
    assert response.status_code == 201
    body = response.json()
    assert body["load_state"] == "loaded"
    assert _tool_names(body) == {"greet"}


# --- contract failures leave the plugin visible but load_failed -------------


@pytest.mark.parametrize(
    "case",
    [
        "crashing_constructor",
        "crashing_auth",
        "crashing_list",
        "auth_manifest_mismatch",
        "duplicate_tool_names",
        "invalid_tool_name",
        "schema_not_object",
        "invalid_schema",
    ],
)
async def test_bad_plugin_contract_loads_failed(
    life_client: httpx.AsyncClient, case: str
) -> None:
    response = await _import(life_client, fixture_cases()[case])

    # A syntactically valid, trusted package is accepted (201) but contained.
    assert response.status_code == 201
    body = response.json()
    assert body["load_state"] == "load_failed"
    assert body["load_error"]
    assert body["tools"] == []
    assert body["status"] == "not_connected"


async def test_failed_load_is_isolated_from_other_plugins(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    assert (await _import(life_client, build_hello_zip())).status_code == 201
    assert (
        await _import(life_client, fixture_cases()["crashing_constructor"])
    ).status_code == 201

    hello = await _detail(life_client, "hello")
    broken = await _detail(life_client, "crash-ctor")
    assert hello["load_state"] == "loaded"
    assert _tool_names(hello) == {"echo", "add"}
    assert broken["load_state"] == "load_failed"

    rows = (await life_client.get("/api/plugins", headers=AUTH)).json()["plugins"]
    states = {row["slug"]: row["load_state"] for row in rows}
    assert states == {"hello": "loaded", "crash-ctor": "load_failed"}


# --- connect policy ---------------------------------------------------------


async def test_connect_error_keeps_plugin_loaded_with_tools(
    life_client: httpx.AsyncClient,
) -> None:
    response = await _import(
        life_client, make_runtime_zip("conn-err", {"plugin.py": _CONNECT_ERROR_SOURCE})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["load_state"] == "loaded"
    assert body["load_error"] is None
    assert body["status"] == "error"
    # Tools remain cached and listed even though connect failed.
    assert _tool_names(body) == {"echo"}


async def test_selected_stored_mode_reconnects_on_startup(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    first = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(first) as client:
        response = await _import(
            client,
            make_runtime_zip(
                "apikey", {"plugin.py": _API_KEY_SOURCE}, auth_modes=("api_key",)
            ),
        )
        assert response.status_code == 201
        body = response.json()
        # No stored mode selected yet, so no connect happens on install.
        assert body["load_state"] == "loaded"
        assert body["auth_mode"] is None
        assert body["status"] == "not_connected"

    assert await redis_async.get("plugin:apikey:data:connects") is None

    meta = await redis_async.get("core:plugin:apikey:meta")
    assert meta is not None
    parsed = json.loads(meta)
    parsed["auth_mode"] = "api_key"
    await redis_async.set("core:plugin:apikey:meta", json.dumps(parsed))

    second = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(second) as client:
        detail = await _detail(client, "apikey")
    assert detail["load_state"] == "loaded"
    assert detail["auth_mode"] == "api_key"
    assert detail["status"] == "connected"
    # Connect used the already-stored selected mode with no credential submission.
    assert await redis_async.get("plugin:apikey:data:connects") == "1"


async def test_startup_reloads_and_auto_connects_none(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    first = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(first) as client:
        assert (await _import(client, build_hello_zip())).status_code == 201

    second = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with _running(second) as client:
        detail = await _detail(client, "hello")
    assert detail["load_state"] == "loaded"
    assert detail["status"] == "connected"
    assert _tool_names(detail) == {"echo", "add"}


# --- reload -----------------------------------------------------------------


async def test_reload_reloads_and_bumps_generation(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, build_hello_zip())
    manager = app.state.plugin_manager
    before = manager.runtime_for("hello")
    assert before is not None
    assert "mcp_plugin_hello" in sys.modules

    response = await life_client.post("/api/plugins/hello/reload", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["load_state"] == "loaded"

    after = manager.runtime_for("hello")
    assert after is not None
    assert after.runtime_id != before.runtime_id
    assert after.generation == before.generation + 1
    # The stable lock and semaphore survive the generation swap.
    assert after.lock is before.lock
    assert after.sem is before.sem
    assert "mcp_plugin_hello" in sys.modules


async def test_reload_load_failure_is_visible_and_clears_tools(
    app: Starlette, settings: Settings, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, build_hello_zip())
    bad = make_runtime_zip("hello", {"plugin.py": _CRASHING_CONSTRUCTOR_SOURCE})
    (Path(settings.DATA_DIR) / "packages" / "hello.zip").write_bytes(bad)

    manager = app.state.plugin_manager
    assert await manager.reload("hello") is True

    detail = await _detail(life_client, "hello")
    assert detail["load_state"] == "load_failed"
    assert detail["tools"] == []
    assert detail["status"] == "not_connected"


async def test_concurrent_reloads_serialize(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, build_hello_zip())
    manager = app.state.plugin_manager
    before = manager.generation("hello")
    assert before is not None

    results = await asyncio.gather(manager.reload("hello"), manager.reload("hello"))
    assert len(results) == 2
    assert all(results)
    assert manager.generation("hello") == before + 2


# --- replace rollback -------------------------------------------------------


async def test_replace_load_failure_rolls_back_old_package(
    life_client: httpx.AsyncClient, settings: Settings, redis_async: AsyncRedis
) -> None:
    hello_bytes = build_hello_zip()
    assert (await _import(life_client, hello_bytes)).status_code == 201
    package_path = Path(settings.DATA_DIR) / "packages" / "hello.zip"

    bad = make_runtime_zip("hello", {"plugin.py": _CRASHING_CONSTRUCTOR_SOURCE})
    response = await _import(life_client, bad, mode="replace")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin"

    # Old package bytes, extraction, runtime and metadata are preserved.
    assert package_path.read_bytes() == hello_bytes
    detail = await _detail(life_client, "hello")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    assert _tool_names(detail) == {"echo", "add"}
    assert detail["status"] == "connected"
    meta = await redis_async.get("core:plugin:hello:meta")
    assert meta is not None
    assert '"version":"1.0.0"' in meta


# --- remove / shutdown ------------------------------------------------------


async def test_remove_calls_shutdown_not_on_disconnect(
    life_client: httpx.AsyncClient,
) -> None:
    response = await _import(
        life_client, make_runtime_zip("marker", {"plugin.py": _MARKER_SOURCE})
    )
    assert response.status_code == 201
    module = sys.modules.get("mcp_plugin_marker.plugin")
    assert module is not None

    removed = await life_client.delete("/api/plugins/marker", headers=AUTH)
    assert removed.status_code == 204
    # shutdown ran; on_disconnect did not; modules were dropped.
    assert getattr(module, "MARKERS", None) == ["shutdown"]
    assert "mcp_plugin_marker" not in sys.modules
    assert "mcp_plugin_marker.plugin" not in sys.modules


async def test_shutdown_is_bounded_by_host_timeout(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    from custom_mcp.plugins.host import PluginHost

    host = PluginHost(
        worker_threads=4,
        per_plugin_concurrency=2,
        call_timeout=5.0,
        tool_timeout=5.0,
        shutdown_timeout=0.1,
    )
    app = create_app(
        settings=settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
        plugin_host=host,
    )
    started = time.monotonic()
    async with _running(app) as client:
        response = await _import(
            client, make_runtime_zip("slow-down", {"plugin.py": _SLOW_SHUTDOWN_SOURCE})
        )
        assert response.status_code == 201
        assert response.json()["load_state"] == "loaded"
    # Shutdown sleeps 0.5s but the 0.1s bound means lifespan exit is fast.
    assert time.monotonic() - started < 3.0
    # The injected host is owned by the caller, not the lifespan.
    await host.aclose()


# --- logs endpoint ----------------------------------------------------------


async def test_logs_endpoint_limit_and_errors(
    app: Starlette, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, build_hello_zip())
    await app.state.plugin_logs.write("hello", "info", "hello from the core")

    ok = await life_client.get("/api/plugins/hello/logs?limit=200", headers=AUTH)
    assert ok.status_code == 200
    entries = ok.json()["logs"]
    assert entries and entries[0]["message"] == "hello from the core"

    clamped = await life_client.get("/api/plugins/hello/logs?limit=100000", headers=AUTH)
    assert clamped.status_code == 200

    invalid = await life_client.get("/api/plugins/hello/logs?limit=abc", headers=AUTH)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"

    negative = await life_client.get("/api/plugins/hello/logs?limit=-1", headers=AUTH)
    assert negative.status_code == 422

    unknown = await life_client.get("/api/plugins/missing/logs", headers=AUTH)
    assert unknown.status_code == 404


async def test_reload_and_logs_require_known_slug(
    life_client: httpx.AsyncClient,
) -> None:
    reload_response = await life_client.post(
        "/api/plugins/missing/reload", headers=AUTH
    )
    assert reload_response.status_code == 404
    assert reload_response.json()["error"]["code"] == "not_found"


# --- privacy: redaction fails closed ----------------------------------------


async def test_detail_redacts_last_error(
    life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    await _import(life_client, build_hello_zip())
    await redis_async.set("plugin:hello:secret:token", json.dumps("topsecretvalue"))
    meta = json.loads(await redis_async.get("core:plugin:hello:meta") or "{}")
    meta["last_error"] = "upstream said topsecretvalue"
    await redis_async.set("core:plugin:hello:meta", json.dumps(meta))

    detail = await _detail(life_client, "hello")
    assert detail["last_error"] is not None
    assert "topsecretvalue" not in detail["last_error"]
    assert "***" in detail["last_error"]


async def test_redactor_failure_never_leaks_original(
    app: Starlette,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _import(life_client, build_hello_zip())
    await redis_async.set("plugin:hello:secret:token", json.dumps("topsecretvalue"))
    meta = json.loads(await redis_async.get("core:plugin:hello:meta") or "{}")
    meta["last_error"] = "leak topsecretvalue"
    await redis_async.set("core:plugin:hello:meta", json.dumps(meta))
    app.state.plugin_manager._records["hello"].load_error = "raw topsecretvalue"

    async def boom(slug: str, message: str) -> str:
        raise RuntimeError("redactor down")

    monkeypatch.setattr(app.state.plugin_logs, "redact", boom)

    response = await life_client.get("/api/plugins/hello", headers=AUTH)
    assert response.status_code == 200
    assert "topsecretvalue" not in response.text
    body = response.json()
    # Fails closed: no original text survives a redaction failure.
    assert body["last_error"] is None
    assert body["load_error"] is None


# --- reload resource cleanup on invalid stored package ----------------------


async def test_reload_invalid_stored_zip_unloads_old(
    app: Starlette, settings: Settings, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, make_runtime_zip("marker", {"plugin.py": _MARKER_SOURCE}))
    module = sys.modules.get("mcp_plugin_marker.plugin")
    assert module is not None

    (Path(settings.DATA_DIR) / "packages" / "marker.zip").write_bytes(b"not a zip")
    manager = app.state.plugin_manager
    assert await manager.reload("marker") is True

    # The old generation was shut down before discard, and modules are gone.
    assert getattr(module, "MARKERS", None) == ["shutdown"]
    assert "mcp_plugin_marker" not in sys.modules
    detail = await _detail(life_client, "marker")
    assert detail["load_state"] == "load_failed"
    assert detail["tools"] == []
    assert detail["status"] == "not_connected"
    # No orphan: remove still succeeds afterwards.
    assert await manager.remove("marker") is True
    assert "mcp_plugin_marker" not in sys.modules


async def test_reload_slug_mismatch_marks_failed_without_new_slug(
    app: Starlette, settings: Settings, life_client: httpx.AsyncClient
) -> None:
    await _import(life_client, make_runtime_zip("marker", {"plugin.py": _MARKER_SOURCE}))
    module = sys.modules.get("mcp_plugin_marker.plugin")
    assert module is not None

    other = make_runtime_zip("other", {"plugin.py": _MARKER_SOURCE})
    (Path(settings.DATA_DIR) / "packages" / "marker.zip").write_bytes(other)
    manager = app.state.plugin_manager
    assert await manager.reload("marker") is True

    assert getattr(module, "MARKERS", None) == ["shutdown"]
    detail = await _detail(life_client, "marker")
    assert detail["load_state"] == "load_failed"
    assert "manifest slug" in (detail["load_error"] or "")
    # No registry injection under the new package name.
    assert await manager.detail("other") is None


# --- startup cancellation keeps loaded runtimes discoverable ----------------


async def test_startup_cancellation_leaves_loaded_runtimes_discoverable(
    app: Starlette, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = Path(settings.DATA_DIR) / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "alpha.zip").write_bytes(
        make_runtime_zip("alpha", {"plugin.py": _MARKER_SOURCE})
    )
    (packages / "beta.zip").write_bytes(
        make_runtime_zip("beta", {"plugin.py": _MARKER_SOURCE})
    )

    loader = app.state.plugin_loader
    original = loader.load

    async def flaky(runtime: Any, package_dir: Path, **kwargs: Any) -> Any:
        if runtime.slug == "beta":
            raise asyncio.CancelledError()
        return await original(runtime, package_dir, **kwargs)

    monkeypatch.setattr(loader, "load", flaky)
    manager = app.state.plugin_manager

    with pytest.raises(asyncio.CancelledError):
        await manager.startup()

    assert manager.runtime_for("alpha") is not None
    module = sys.modules.get("mcp_plugin_alpha.plugin")
    assert module is not None
    # The partially started runtime is discoverable and cleaned by shutdown.
    await manager.shutdown_all()
    assert getattr(module, "MARKERS", None) == ["shutdown"]
    assert "mcp_plugin_alpha" not in sys.modules


# --- host ownership and lifespan cleanup ------------------------------------


async def test_injected_host_not_closed_by_lifespan(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    recorded = {"closed": 0}

    class Tracked(PluginHost):
        async def aclose(self) -> None:
            recorded["closed"] += 1
            await super().aclose()

    host = Tracked()
    app = create_app(
        settings=settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
        plugin_host=host,
    )
    async with app.router.lifespan_context(app):
        pass
    assert recorded["closed"] == 0
    await host.aclose()
    assert recorded["closed"] == 1


async def test_owned_host_closed_once_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
) -> None:
    recorded = {"closed": 0}

    class Tracked(PluginHost):
        async def aclose(self) -> None:
            recorded["closed"] += 1
            await super().aclose()

    monkeypatch.setattr("custom_mcp.main.PluginHost", Tracked)
    app = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    async with app.router.lifespan_context(app):
        pass
    assert recorded["closed"] == 1


class _MemAsyncRedis:
    """Minimal app-owned async redis double for lifespan/cleanup tests."""

    def __init__(self, *, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping
        self.closed = False
        self.sets: dict[str, set[str]] = {}

    async def ping(self) -> bool:
        if self.fail_ping:
            raise ConnectionError("redis is down")
        return True

    async def smembers(self, name: str) -> set[str]:
        return set(self.sets.get(name, set()))

    async def sadd(self, name: str, *values: str) -> int:
        self.sets.setdefault(name, set()).update(values)
        return len(values)

    async def srem(self, name: str, *values: str) -> int:
        self.sets.get(name, set()).difference_update(values)
        return 0

    async def aclose(self) -> None:
        self.closed = True


class _MemSyncRedis:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_owned_host_closed_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    recorded = {"closed": 0}

    class Tracked(PluginHost):
        async def aclose(self) -> None:
            recorded["closed"] += 1
            await super().aclose()

    async_redis = _MemAsyncRedis(fail_ping=True)
    sync_redis = _MemSyncRedis()
    monkeypatch.setattr("custom_mcp.main.PluginHost", Tracked)
    monkeypatch.setattr("custom_mcp.main.create_async_redis", lambda url, **kw: async_redis)
    monkeypatch.setattr("custom_mcp.main.create_sync_redis", lambda url, **kw: sync_redis)

    app = create_app(settings=settings)
    with pytest.raises(ConnectionError):
        async with app.router.lifespan_context(app):
            pass
    assert recorded["closed"] == 1
    assert async_redis.closed is True
    assert sync_redis.closed is True


async def test_lifespan_cleanup_survives_cancelled_shutdown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_redis = _MemAsyncRedis()
    sync_redis = _MemSyncRedis()
    monkeypatch.setattr("custom_mcp.main.create_async_redis", lambda url, **kw: async_redis)
    monkeypatch.setattr("custom_mcp.main.create_sync_redis", lambda url, **kw: sync_redis)

    app = create_app(settings=settings)
    manager = app.state.plugin_manager

    async def cancelled() -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "shutdown_all", cancelled)

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass
    # Redis cleanup still ran despite the cancelled shutdown.
    assert async_redis.closed is True
    assert sync_redis.closed is True


# --- loader timeout wiring --------------------------------------------------


def test_loader_uses_configured_timeout(
    monkeypatch: pytest.MonkeyPatch, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    captured: dict[str, Any] = {}

    class Recorder:
        def __init__(self, host: PluginHost, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("custom_mcp.main.PluginLoader", Recorder)
    settings = Settings.from_env(build_env(PLUGIN_CALL_TIMEOUT_SECONDS="7"))
    host = PluginHost()
    create_app(
        settings=settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
        plugin_host=host,
    )
    assert captured.get("load_timeout") == 7.0


# --- startup cancellation during post-load connect --------------------------


async def test_startup_cancel_during_connect_keeps_runtime_for_shutdown(
    app: Starlette, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = Path(settings.DATA_DIR) / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    (packages / "alpha.zip").write_bytes(
        make_runtime_zip("alpha", {"plugin.py": _MARKER_SOURCE})
    )
    (packages / "beta.zip").write_bytes(
        make_runtime_zip("beta", {"plugin.py": _MARKER_SOURCE})
    )

    host = app.state.plugin_host
    original_call = host.call
    ready = asyncio.Event()
    release = asyncio.Event()

    async def gated(runtime: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "connect" and runtime.slug == "beta":
            ready.set()
            await release.wait()
        return await original_call(runtime, method, *args, **kwargs)

    monkeypatch.setattr(host, "call", gated)
    manager = app.state.plugin_manager

    task = asyncio.create_task(manager.startup())
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # beta's runtime was registered before connect, so shutdown can find it.
    assert manager.runtime_for("beta") is not None
    alpha_module = sys.modules.get("mcp_plugin_alpha.plugin")
    beta_module = sys.modules.get("mcp_plugin_beta.plugin")
    assert alpha_module is not None and beta_module is not None

    await manager.shutdown_all()
    assert getattr(alpha_module, "MARKERS", None) == ["shutdown"]
    assert getattr(beta_module, "MARKERS", None) == ["shutdown"]
    assert "mcp_plugin_alpha" not in sys.modules
    assert "mcp_plugin_beta" not in sys.modules


# --- lifespan cleanup under external cancellation ---------------------------


async def test_lifespan_external_cancel_during_shutdown_still_cleans(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_redis = _MemAsyncRedis()
    sync_redis = _MemSyncRedis()
    recorded = {"closed": 0}

    class Tracked(PluginHost):
        async def aclose(self) -> None:
            recorded["closed"] += 1
            await super().aclose()

    monkeypatch.setattr("custom_mcp.main.PluginHost", Tracked)
    monkeypatch.setattr("custom_mcp.main.create_async_redis", lambda url, **kw: async_redis)
    monkeypatch.setattr("custom_mcp.main.create_sync_redis", lambda url, **kw: sync_redis)

    app = create_app(settings=settings)
    manager = app.state.plugin_manager
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_shutdown() -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "shutdown_all", blocked_shutdown)

    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
    await asyncio.wait_for(entered.wait(), timeout=5)
    exit_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await exit_task

    assert recorded["closed"] == 1
    assert async_redis.closed is True
    assert sync_redis.closed is True


# --- candidate storage transaction (defect 1) -------------------------------

_V1_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_CANDIDATE_BOOM_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("counter", 999)
        self.ctx.storage.set("shutdown_marker", "candidate")
        self.ctx.secrets.set("token", "bad")
        self.ctx.secrets.set("staged", "newstagedsecret")
        self.ctx.log("info", "candidate wrote newstagedsecret")
        raise RuntimeError("candidate boom")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def shutdown(self):
        self.ctx.storage.set("shutdown_marker", "shutdown")

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_CANDIDATE_COMMIT_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        existing = self.ctx.storage.get("counter", 0)
        self.ctx.storage.set("counter", int(existing) + 35)
        self.ctx.secrets.set("token2", "committed-secret")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_CANDIDATE_CONNECT_ERROR_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("counter", 42)

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        raise RuntimeError("connect boom after write")

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''


async def test_replace_candidate_writes_are_discarded_on_load_failure(
    app: Starlette, settings: Settings, life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))
    await redis_async.set("plugin:cand:data:temp", json.dumps("t"), ex=600)
    package_path = Path(settings.DATA_DIR) / "packages" / "cand.zip"
    old_bytes = package_path.read_bytes()
    manager = app.state.plugin_manager
    old_runtime = manager.runtime_for("cand")
    assert old_runtime is not None

    response = await _import(
        life_client,
        make_runtime_zip("cand", {"plugin.py": _CANDIDATE_BOOM_SOURCE}),
        mode="replace",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin"

    # Staged candidate writes never reached live storage.
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert await redis_async.get("plugin:cand:secret:staged") is None
    assert await redis_async.get("plugin:cand:data:shutdown_marker") is None
    assert await redis_async.ttl("plugin:cand:data:temp") > 0

    # Old files, runtime and metadata are preserved.
    assert package_path.read_bytes() == old_bytes
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["status"] == "connected"
    assert detail["manifest"]["version"] == "1.0.0"

    # The staged credential was redacted before it reached the log.
    entries = await app.state.plugin_logs.recent("cand")
    text = json.dumps(entries)
    assert "newstagedsecret" not in text
    assert "***" in text


async def test_replace_commit_applies_delta_and_preserves_untouched(
    app: Starlette, settings: Settings, life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))
    await redis_async.set("plugin:cand:data:temp", json.dumps("t"), ex=600)
    manager = app.state.plugin_manager
    old_runtime = manager.runtime_for("cand")

    response = await _import(
        life_client,
        make_runtime_zip("cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}),
        mode="replace",
    )
    assert response.status_code == 201
    assert response.json()["load_state"] == "loaded"

    # Delta committed: touched key changed, untouched key + its TTL preserved.
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(42)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert await redis_async.get("plugin:cand:secret:token2") == json.dumps(
        "committed-secret"
    )
    assert await redis_async.ttl("plugin:cand:data:temp") > 0

    # Stable lock/semaphore survive the generation swap.
    new_runtime = manager.runtime_for("cand")
    assert new_runtime is not None
    assert new_runtime.lock is old_runtime.lock
    assert new_runtime.sem is old_runtime.sem
    assert new_runtime.generation == old_runtime.generation + 1


async def test_replace_connect_error_still_commits_upgrade(
    life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201

    response = await _import(
        life_client,
        make_runtime_zip("cand", {"plugin.py": _CANDIDATE_CONNECT_ERROR_SOURCE}),
        mode="replace",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["load_state"] == "loaded"
    assert body["status"] == "error"
    # The upgrade committed despite the connection error.
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(42)


async def test_replace_candidate_on_empty_prior_state(
    life_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201

    response = await _import(
        life_client,
        make_runtime_zip("cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}),
        mode="replace",
    )
    assert response.status_code == 201
    assert response.json()["load_state"] == "loaded"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(35)
    assert await redis_async.get("plugin:cand:secret:token2") == json.dumps(
        "committed-secret"
    )


_CANDIDATE_LATE_SOURCE = '''\
import time

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        # Bounded delay so the host load timeout fires while this worker keeps
        # running and later writes to its (staged) context.
        time.sleep(0.4)
        self.ctx.storage.set("counter", 999)
        self.ctx.secrets.set("token", "bad")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_CANDIDATE_CLEANUP_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

MARKERS = []


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("counter", 999)

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def shutdown(self):
        MARKERS.append("shutdown")

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''


async def test_replace_commit_barrier_late_cancel_keeps_new_state(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))
    await redis_async.set("plugin:cand:data:temp", json.dumps("t"), ex=600)

    manager = app.state.plugin_manager
    old_runtime = manager.runtime_for("cand")
    assert old_runtime is not None

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    storage = app.state.plugin_storage
    original_apply = storage.apply_delta

    def paused_apply(commit_id: str, sets: Any, deletes: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise RuntimeError("commit barrier was not released")
        return original_apply(commit_id, sets, deletes)

    monkeypatch.setattr(storage, "apply_delta", paused_apply)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # Cancel twice while the commit worker is paused inside apply_delta.
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # The commit settled: new files/runtime/meta and the committed delta stand.
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "2.0.0"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(42)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert await redis_async.ttl("plugin:cand:data:temp") > 0
    assert (Path(settings.DATA_DIR) / "packages" / "cand.zip").read_bytes() == v2

    new_runtime = manager.runtime_for("cand")
    assert new_runtime is not None
    assert new_runtime.generation == old_runtime.generation + 1
    assert new_runtime.lock is old_runtime.lock
    assert new_runtime.sem is old_runtime.sem


async def test_replace_precommit_cancel_unloads_candidate_and_restores_old(
    app: Starlette,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await _import(life_client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
    ).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    old_runtime = manager.runtime_for("cand")
    assert old_runtime is not None
    host = app.state.plugin_host
    original_call = host.call
    ready = asyncio.Event()
    release = asyncio.Event()
    gate_used = {"v": False}

    async def gated(runtime: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "connect" and not gate_used["v"]:
            gate_used["v"] = True
            ready.set()
            await release.wait()
        return await original_call(runtime, method, *args, **kwargs)

    monkeypatch.setattr(host, "call", gated)

    v2 = make_runtime_zip("cand", {"plugin.py": _CANDIDATE_CLEANUP_SOURCE})
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(ready.wait(), timeout=5)
    module = sys.modules.get("mcp_plugin_cand.plugin")
    assert module is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # Candidate was shut down (bounded) before discard, then old restored.
    assert getattr(module, "MARKERS", None) == ["shutdown"]
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    restored = manager.runtime_for("cand")
    assert restored is not None and restored is not old_runtime


async def test_replace_late_constructor_write_stays_detached(
    settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    host = PluginHost(
        worker_threads=4,
        per_plugin_concurrency=2,
        call_timeout=5.0,
        tool_timeout=5.0,
        shutdown_timeout=1.0,
    )
    loader = PluginLoader(host, load_timeout=0.1)
    app = create_app(
        settings=settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
        plugin_host=host,
        plugin_loader=loader,
    )
    try:
        async with _running(app) as client:
            assert (
                await _import(client, make_runtime_zip("cand", {"plugin.py": _V1_SOURCE}))
            ).status_code == 201
            await redis_async.set("plugin:cand:data:counter", json.dumps(7))
            await redis_async.set("plugin:cand:secret:token", json.dumps("original"))
            manager = app.state.plugin_manager
            old_runtime = manager.runtime_for("cand")
            assert old_runtime is not None

            response = await _import(
                client,
                make_runtime_zip("cand", {"plugin.py": _CANDIDATE_LATE_SOURCE}),
                mode="replace",
            )
            assert response.status_code == 422

            # Wait for the late constructor worker to finish and write.
            assert await host.wait_for_idle("cand", timeout=5) is True

            # Its staged write never reached live storage; old state intact.
            assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
            assert await redis_async.get("plugin:cand:secret:token") == json.dumps(
                "original"
            )
            detail = await _detail(client, "cand")
            assert detail["load_state"] == "loaded"
            assert detail["manifest"]["version"] == "1.0.0"
            assert manager.runtime_for("cand") is not None
    finally:
        await host.aclose()


async def test_replace_forward_cancel_during_old_unload_restores_old(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    old_runtime = manager.runtime_for("cand")
    assert old_runtime is not None

    loader = app.state.plugin_loader
    original_unload = loader.unload
    entered = asyncio.Event()
    release = threading.Event()

    async def blocking_unload(runtime: Any) -> None:
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        await original_unload(runtime)

    monkeypatch.setattr(loader, "unload", blocking_unload)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # Cancel twice while the *forward* old-runtime unload is blocked.
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # The forward cancellation propagated: v2 was never committed, old intact.
    package_path = Path(settings.DATA_DIR) / "packages" / "cand.zip"
    assert package_path.read_bytes() == old_bytes
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    restored = manager.runtime_for("cand")
    assert restored is not None


async def test_reload_forward_cancel_during_old_unload_restores_old(
    app: Starlette,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))

    manager = app.state.plugin_manager
    loader = app.state.plugin_loader
    original_unload = loader.unload
    entered = asyncio.Event()
    release = threading.Event()

    async def blocking_unload(runtime: Any) -> None:
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        await original_unload(runtime)

    monkeypatch.setattr(loader, "unload", blocking_unload)

    task = asyncio.create_task(manager.reload("cand"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # No files changed; the old runtime is restored (slug stays loaded).
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert manager.runtime_for("cand") is not None


async def test_replace_forward_coincident_unload_cancel_restores_old(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    loader = app.state.plugin_loader
    original_unload = loader.unload
    loop = asyncio.get_running_loop()
    holder: dict[str, asyncio.Task[Any]] = {}

    async def coincident_unload(runtime: Any) -> None:
        await original_unload(runtime)
        # Schedule the caller's cancel exactly as the unload completes.
        loop.call_soon(holder["task"].cancel)

    monkeypatch.setattr(loader, "unload", coincident_unload)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    holder["task"] = task
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    # v2 must not have committed: old ZIP/state/runtime intact.
    package_path = Path(settings.DATA_DIR) / "packages" / "cand.zip"
    assert package_path.read_bytes() == old_bytes
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    assert manager.runtime_for("cand") is not None


async def test_reload_forward_coincident_unload_cancel_restores_old(
    app: Starlette,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))

    manager = app.state.plugin_manager
    loader = app.state.plugin_loader
    original_unload = loader.unload
    loop = asyncio.get_running_loop()
    holder: dict[str, asyncio.Task[Any]] = {}

    async def coincident_unload(runtime: Any) -> None:
        await original_unload(runtime)
        loop.call_soon(holder["task"].cancel)

    monkeypatch.setattr(loader, "unload", coincident_unload)

    task = asyncio.create_task(manager.reload("cand"))
    holder["task"] = task
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert manager.runtime_for("cand") is not None


async def test_lifespan_settle_records_coincident_cancel(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async_redis = _MemAsyncRedis()
    sync_redis = _MemSyncRedis()
    recorded = {"closed": 0}

    class Tracked(PluginHost):
        async def aclose(self) -> None:
            recorded["closed"] += 1
            await super().aclose()

    monkeypatch.setattr("custom_mcp.main.PluginHost", Tracked)
    monkeypatch.setattr("custom_mcp.main.create_async_redis", lambda url, **kw: async_redis)
    monkeypatch.setattr("custom_mcp.main.create_sync_redis", lambda url, **kw: sync_redis)

    app = create_app(settings=settings)
    manager = app.state.plugin_manager
    loop = asyncio.get_running_loop()
    holder: dict[str, asyncio.Task[Any]] = {}

    async def cleanup_coincident() -> None:
        # Inner coroutine returns immediately; schedule the parent's cancel so it
        # lands exactly as the task becomes ready (the flag-before-done case).
        loop.call_soon(holder["task"].cancel)

    monkeypatch.setattr(manager, "shutdown_all", cleanup_coincident)

    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    exit_task = asyncio.create_task(cm.__aexit__(None, None, None))
    holder["task"] = exit_task
    with pytest.raises(asyncio.CancelledError):
        await exit_task

    # Cancellation was recorded, and all owned cleanup still completed.
    assert recorded["closed"] == 1
    assert async_redis.closed is True
    assert sync_redis.closed is True


# --- receipt-specific unknown / reconciled commit outcomes ------------------

_CANDIDATE_UNKNOWN_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("counter", 999)

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [
            Tool(
                name="probe",
                description="Probe.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''


def _pending_dirs(settings: Settings) -> list[Path]:
    tmp = Path(settings.DATA_DIR) / "tmp"
    return sorted(tmp.glob("pending-*")) if tmp.is_dir() else []


async def test_replace_commit_unknown_preserves_both_and_blocks(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage

    def pending_apply(commit_id: str, sets: Any, deletes: Any) -> str:
        # Simulate a durable pending receipt plus a partial live write, then lose
        # the reply entirely (no readable outcome).
        redis_sync.set(
            StorageManager.receipt_key(commit_id), f"{commit_id}:pending", ex=600
        )
        redis_sync.set("plugin:cand:data:partial", "1")
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(storage, "apply_delta", pending_apply)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_UNKNOWN_SOURCE}, version="2.0.0"
    )
    response = await _import(life_client, v2, mode="replace")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "restored" not in response.text.lower()

    # New source stored; old bytes preserved in a no-secrets pending journal.
    package_path = Path(settings.DATA_DIR) / "packages" / "cand.zip"
    assert package_path.read_bytes() == v2
    pending = _pending_dirs(settings)
    assert len(pending) == 1
    assert (pending[0] / "old.zip").read_bytes() == old_bytes
    journal_raw = (pending[0] / "journal.json").read_text()
    assert set(json.loads(journal_raw)) == {"slug", "commit_id", "old_version"}
    assert "original" not in journal_raw

    # Candidate retained: loaded, status error, tools cached, ctx unpromoted.
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["status"] == "error"
    assert _tool_names(detail) == {"probe"}
    runtime = manager.runtime_for("cand")
    assert runtime is not None
    assert runtime.status.value == "error"
    runtime.context.storage.set("post_unknown_probe", 1)
    assert await redis_async.get("plugin:cand:data:post_unknown_probe") is None

    # Same-slug lifecycle is blocked (generic 500); other plugins unaffected.
    assert (
        await _import(life_client, old_bytes, mode="replace")
    ).status_code == 500
    assert (
        await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    ).status_code == 500
    assert (
        await life_client.delete("/api/plugins/cand", headers=AUTH)
    ).status_code == 500
    other = await _import(life_client, make_runtime_zip("other", {"plugin.py": _V1_SOURCE}))
    assert other.status_code == 201

    # Startup sees the pending journal before cleanup/load and blocks; old bytes
    # are retained.
    second = create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)
    with pytest.raises(RuntimeError):
        async with _running(second):
            pass
    assert (pending[0] / "old.zip").read_bytes() == old_bytes


async def test_replace_loss_reply_committed_reconciles_success(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))
    await redis_async.set("plugin:cand:data:temp", json.dumps("t"), ex=600)

    storage = app.state.plugin_storage
    original_apply = storage.apply_delta

    def loss_reply(commit_id: str, sets: Any, deletes: Any) -> str:
        original_apply(commit_id, sets, deletes)  # commits + receipt
        raise RuntimeError("reply lost after commit")

    monkeypatch.setattr(storage, "apply_delta", loss_reply)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    response = await _import(life_client, v2, mode="replace")
    assert response.status_code == 201
    assert response.json()["manifest"]["version"] == "2.0.0"
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(42)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert await redis_async.ttl("plugin:cand:data:temp") > 0
    assert _pending_dirs(settings) == []


async def test_replace_rolled_back_commit_keeps_old(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    storage = app.state.plugin_storage

    def rolled_back(commit_id: str, sets: Any, deletes: Any) -> str:
        return "rolled_back"

    monkeypatch.setattr(storage, "apply_delta", rolled_back)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    response = await _import(life_client, v2, mode="replace")
    assert response.status_code == 500
    package_path = Path(settings.DATA_DIR) / "packages" / "cand.zip"
    assert package_path.read_bytes() == old_bytes
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    assert _pending_dirs(settings) == []


# --- queued containment, reload generation, cancelled outcomes -------------


async def test_queued_remove_behind_uncertain_replace_is_blocked(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def pending_apply(commit_id: str, sets: Any, deletes: Any) -> str:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise RuntimeError("commit barrier was not released")
        redis_sync.set(
            StorageManager.receipt_key(commit_id), f"{commit_id}:pending", ex=600
        )
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(storage, "apply_delta", pending_apply)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_UNKNOWN_SOURCE}, version="2.0.0"
    )
    replace_task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # Queue a DELETE while the replacement holds the slug lock. ``prechecked``
    # proves the queued call passed its pre-lock check *before* the slug became
    # pending, so only the in-lock re-assertion can stop it.
    prechecked = asyncio.Event()
    original_require = manager._require_not_pending

    def tracked(slug: str) -> None:
        original_require(slug)
        prechecked.set()

    monkeypatch.setattr(manager, "_require_not_pending", tracked)
    remove_task = asyncio.create_task(
        life_client.delete("/api/plugins/cand", headers=AUTH)
    )
    await asyncio.wait_for(prechecked.wait(), timeout=5)

    release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(replace_task, timeout=5)

    response = await asyncio.wait_for(remove_task, timeout=5)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    # The preserved journal, both generations and live data survived.
    pending = _pending_dirs(settings)
    assert len(pending) == 1
    assert (pending[0] / "old.zip").read_bytes() == old_bytes
    assert (Path(settings.DATA_DIR) / "plugins" / "cand").is_dir()
    assert (Path(settings.DATA_DIR) / "packages" / "cand.zip").read_bytes() == v2
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")


async def test_queued_reload_behind_uncertain_replace_is_blocked(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def pending_apply(commit_id: str, sets: Any, deletes: Any) -> str:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise RuntimeError("commit barrier was not released")
        redis_sync.set(
            StorageManager.receipt_key(commit_id), f"{commit_id}:pending", ex=600
        )
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(storage, "apply_delta", pending_apply)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_UNKNOWN_SOURCE}, version="2.0.0"
    )
    replace_task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    prechecked = asyncio.Event()
    original_require = manager._require_not_pending

    def tracked(slug: str) -> None:
        original_require(slug)
        prechecked.set()

    monkeypatch.setattr(manager, "_require_not_pending", tracked)
    reload_task = asyncio.create_task(
        life_client.post("/api/plugins/cand/reload", headers=AUTH)
    )
    await asyncio.wait_for(prechecked.wait(), timeout=5)

    release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(replace_task, timeout=5)

    response = await asyncio.wait_for(reload_task, timeout=5)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    # The queued reload did not re-commit over the preserved pending state.
    pending = _pending_dirs(settings)
    assert len(pending) == 1
    assert (pending[0] / "old.zip").read_bytes() == old_bytes
    assert (Path(settings.DATA_DIR) / "plugins" / "cand").is_dir()
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")


async def test_reload_uncertain_preserves_old_extraction_and_blocks(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    storage = app.state.plugin_storage

    def pending_apply(commit_id: str, sets: Any, deletes: Any) -> str:
        redis_sync.set(
            StorageManager.receipt_key(commit_id), f"{commit_id}:pending", ex=600
        )
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(storage, "apply_delta", pending_apply)

    response = await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "restored" not in response.text.lower()

    pending = _pending_dirs(settings)
    assert len(pending) == 1
    old_dir = pending[0] / "old_dir"
    assert old_dir.is_dir()
    assert (old_dir / "plugin.py").is_file()
    journal_raw = (pending[0] / "journal.json").read_text()
    journal = json.loads(journal_raw)
    assert set(journal) == {"slug", "commit_id", "old_version"}
    assert journal["old_version"] == "1.0.0"
    assert "original" not in journal_raw

    # Both generations survive: the new extraction stays live and the old
    # extraction lives on inside the pending journal.
    assert (Path(settings.DATA_DIR) / "plugins" / "cand" / "plugin.py").is_file()
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")

    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["status"] == "error"

    # Every same-slug lifecycle change is blocked with the generic error.
    assert (
        await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    ).status_code == 500
    assert (
        await life_client.delete("/api/plugins/cand", headers=AUTH)
    ).status_code == 500


async def test_reload_confirmed_rollback_restores_old_runtime(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage

    def rolled_back(commit_id: str, sets: Any, deletes: Any) -> str:
        return "rolled_back"

    monkeypatch.setattr(storage, "apply_delta", rolled_back)

    response = await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    assert response.status_code == 500

    # The old generation is live again, never "loaded" without a runtime.
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert manager.runtime_for("cand") is not None
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert (Path(settings.DATA_DIR) / "packages" / "cand.zip").read_bytes() == old_bytes
    assert _pending_dirs(settings) == []


async def test_cancelled_replace_rolled_back_raises_cancelled_and_keeps_old(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def blocking_rolled_back(commit_id: str, sets: Any, deletes: Any) -> str:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise RuntimeError("commit barrier was not released")
        return "rolled_back"

    monkeypatch.setattr(storage, "apply_delta", blocking_rolled_back)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_COMMIT_SOURCE}, version="2.0.0"
    )
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert task.cancelled()

    # Rollback preserved the old generation even though the caller cancelled.
    assert (Path(settings.DATA_DIR) / "packages" / "cand.zip").read_bytes() == old_bytes
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    detail = await _detail(life_client, "cand")
    assert detail["load_state"] == "loaded"
    assert detail["manifest"]["version"] == "1.0.0"
    assert manager.runtime_for("cand") is not None
    assert _pending_dirs(settings) == []


async def test_cancelled_replace_unknown_raises_cancelled_and_preserves_pending(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_bytes = make_runtime_zip("cand", {"plugin.py": _V1_SOURCE})
    assert (await _import(life_client, old_bytes)).status_code == 201
    await redis_async.set("plugin:cand:data:counter", json.dumps(7))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original"))

    manager = app.state.plugin_manager
    storage = app.state.plugin_storage
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def blocking_unknown(commit_id: str, sets: Any, deletes: Any) -> str:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=10):
            raise RuntimeError("commit barrier was not released")
        redis_sync.set(
            StorageManager.receipt_key(commit_id), f"{commit_id}:pending", ex=600
        )
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(storage, "apply_delta", blocking_unknown)

    v2 = make_runtime_zip(
        "cand", {"plugin.py": _CANDIDATE_UNKNOWN_SOURCE}, version="2.0.0"
    )
    task = asyncio.create_task(manager.install(v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert task.cancelled()

    # The pending journal and in-memory block were still established.
    pending = _pending_dirs(settings)
    assert len(pending) == 1
    assert (pending[0] / "old.zip").read_bytes() == old_bytes
    journal_raw = (pending[0] / "journal.json").read_text()
    assert set(json.loads(journal_raw)) == {"slug", "commit_id", "old_version"}
    assert "original" not in journal_raw
    assert "cand" in manager._pending
    assert (Path(settings.DATA_DIR) / "packages" / "cand.zip").read_bytes() == v2
    assert await redis_async.get("plugin:cand:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps("original")
    assert (
        await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    ).status_code == 500


_RELOAD_V1_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

# reload-v1-marker


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("version_marker", "v1")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''

_RELOAD_V2_DESTRUCTIVE_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

# reload-v2-marker


class FixturePlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.ctx.storage.set("version_marker", "v2")
        self.ctx.storage.delete("important")
        self.ctx.secrets.set("token", "v2-overwritten")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown", "unknown")
'''


async def test_reload_rollback_restores_old_extraction_before_reloading(
    app: Starlette,
    settings: Settings,
    life_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = app.state.plugin_manager
    storage = app.state.plugin_storage
    data_dir = Path(settings.DATA_DIR)

    v1 = make_runtime_zip("cand", {"plugin.py": _RELOAD_V1_SOURCE}, version="1.0.0")
    assert (await _import(life_client, v1)).status_code == 201
    await redis_async.set("plugin:cand:data:important", json.dumps("keep-me"))
    await redis_async.set("plugin:cand:secret:token", json.dumps("original-token"))

    # An operator replaces the stored source with destructive v2 code, then a
    # reload's commit is confirmed rolled back.
    v2 = make_runtime_zip(
        "cand", {"plugin.py": _RELOAD_V2_DESTRUCTIVE_SOURCE}, version="2.0.0"
    )
    (data_dir / "packages" / "cand.zip").write_bytes(v2)

    def rolled_back(commit_id: str, sets: Any, deletes: Any) -> str:
        return "rolled_back"

    monkeypatch.setattr(storage, "apply_delta", rolled_back)

    response = await life_client.post("/api/plugins/cand/reload", headers=AUTH)
    assert response.status_code == 500

    # v2 never ran against live storage: data/secrets are untouched and the
    # live generation is still v1.
    assert await redis_async.get("plugin:cand:data:important") == json.dumps("keep-me")
    assert await redis_async.get("plugin:cand:secret:token") == json.dumps(
        "original-token"
    )
    assert await redis_async.get("plugin:cand:data:version_marker") == json.dumps("v1")

    runtime = manager.runtime_for("cand")
    assert runtime is not None
    assert runtime.manifest.version == "1.0.0"

    # The extraction on disk is the retained old code, not the candidate.
    plugin_source = (data_dir / "plugins" / "cand" / "plugin.py").read_text()
    assert "reload-v1-marker" in plugin_source
    assert "reload-v2-marker" not in plugin_source

    # Metadata was restored with the old generation.
    meta_raw = await redis_async.get("core:plugin:cand:meta")
    assert meta_raw is not None
    assert json.loads(meta_raw)["version"] == "1.0.0"
    detail = await _detail(life_client, "cand")
    assert detail["manifest"]["version"] == "1.0.0"
    assert detail["load_state"] == "loaded"

    # No unresolved outcome: the retry boundary is clean.
    assert _pending_dirs(settings) == []
