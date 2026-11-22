"""Phase 4 containment regressions added after the second gate re-review.

Three reproduced defects:

1. reconnect after disconnect re-enabled a retired generation's leaked worker
   (the old ``seal.clear()``);
2. a reload/replace-promoted generation had no seal, so a timed-out credential
   hook's leaked worker could recreate a wiped secret;
3. sealing before ``shutdown`` silently dropped a plugin's legitimate
   checkpoint write.

Each drives the real app (fake Redis, real host/loader/manager, bearer auth).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from mcp import ClientSession
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_env, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}

# api_key plugin whose ``on_api_key_set`` can block (so the host times it out and
# leaks a worker) and writes a derived secret + a credential-bearing log line.
_HOOK_PLUGIN = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

HOOK_ENTERED = threading.Event()
HOOK_RELEASE = threading.Event()
HOOK_DONE = threading.Event()
BLOCK_HOOK = False


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="whoami",
                description="meta",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        if BLOCK_HOOK:
            HOOK_ENTERED.set()
            HOOK_RELEASE.wait(30)
        self.ctx.secrets.set("derived", api_key)
        self.ctx.log("info", "hook used " + api_key)
        HOOK_DONE.set()

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"slug": self.ctx.slug})
'''

# none-auth plugin that buffers a counter and checkpoints it on shutdown.
_CHECKPOINT_PLUGIN = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        stored = self.ctx.storage.get("checkpoint")
        self.counter = int(stored) if isinstance(stored, int) else 0

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="bump",
                description="bump",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.counter += 1
        return ToolResult.success({"counter": self.counter})

    def shutdown(self) -> None:
        self.ctx.storage.set("checkpoint", self.counter)
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=TEST_BASE_URL,
            timeout=httpx.Timeout(60.0),
        ) as client:
            yield client


def _client_factory(transport: httpx.ASGITransport) -> Any:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "transport": transport,
            "base_url": TEST_BASE_URL,
            "timeout": httpx.Timeout(30.0),
        }
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
    async with streamablehttp_client(
        url, headers=AUTH, httpx_client_factory=_client_factory(transport)
    ) as (read, write, _), ClientSession(read, write) as session:
        yield session


async def _import(
    client: httpx.AsyncClient, slug: str, source: str, *, auth_modes: tuple[str, ...]
) -> None:
    response = await client.post(
        "/api/plugins/import",
        files={
            "file": (
                "plugin.zip",
                build_plugin_zip(
                    slug=slug, auth_modes=auth_modes, plugin_source=source
                ),
                "application/zip",
            )
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text


def _module(slug: str) -> Any:
    return sys.modules[f"mcp_plugin_{slug.replace('-', '_')}.plugin"]


async def _connect(client: httpx.AsyncClient, slug: str, key: str) -> httpx.Response:
    return await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "api_key", "credentials": {"api_key": key}},
        headers=AUTH,
    )


async def _secret_keys(redis_async: AsyncRedis, slug: str) -> list[str]:
    return [key async for key in redis_async.scan_iter(match=f"plugin:{slug}:secret:*")]


def _text(result: Any) -> str:
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    raise AssertionError("tool result had no text content block")


# --- Finding 1: reconnect must not revive a leaked worker -------------------


async def test_reconnect_after_disconnect_contains_leaked_worker(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "reconnectcontain"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _HOOK_PLUGIN, auth_modes=("api_key",))
        old_module = _module(slug)

        # 1. connect with key A times out; A's worker leaks and stays blocked.
        old_module.BLOCK_HOOK = True
        assert (await _connect(client, slug, "key-A-leak")).status_code == 200
        await asyncio.to_thread(old_module.HOOK_ENTERED.wait, 5)

        # 2. disconnect seals the generation and wipes secrets.
        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200
        assert await _secret_keys(redis_async, slug) == []

        # 3. reconnect with key B: the sealed generation is rebuilt, not unsealed.
        response = await _connect(client, slug, "key-B-new")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "connected"

        # A fresh module was imported for the rebuilt generation.
        new_module = _module(slug)
        assert new_module is not old_module

        assert await redis_async.get(
            f"plugin:{slug}:secret:core.api_key"
        ) == json.dumps("key-B-new")
        assert await redis_async.get(f"plugin:{slug}:secret:derived") == json.dumps(
            "key-B-new"
        )

        # 4. release A's leaked worker: it must not write.
        old_module.HOOK_RELEASE.set()
        await asyncio.to_thread(old_module.HOOK_DONE.wait, 5)
        await asyncio.sleep(0.1)

        assert await redis_async.get(
            f"plugin:{slug}:secret:core.api_key"
        ) == json.dumps("key-B-new")
        assert await redis_async.get(f"plugin:{slug}:secret:derived") == json.dumps(
            "key-B-new"
        )
        persisted = json.dumps(await app.state.plugin_manager.logs(slug))
        assert "key-A-leak" not in persisted
        assert "key-B-new" not in persisted


# --- Finding 2: a promoted generation must be sealed too --------------------


async def test_reloaded_generation_late_worker_is_contained(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "reloadcontain"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _HOOK_PLUGIN, auth_modes=("api_key",))
        assert (await _connect(client, slug, "reload-key")).status_code == 200

        # Promote a new generation through reload.
        reload_response = await client.post(f"/api/plugins/{slug}/reload", headers=AUTH)
        assert reload_response.status_code == 200

        # Block a credential hook on the promoted generation past the timeout.
        module = _module(slug)
        module.BLOCK_HOOK = True
        response = await _connect(client, slug, "reload-key-2")
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        await asyncio.to_thread(module.HOOK_ENTERED.wait, 5)

        # Disconnect seals and wipes; releasing the worker must not recreate it.
        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200
        assert await _secret_keys(redis_async, slug) == []

        module.HOOK_RELEASE.set()
        await asyncio.to_thread(module.HOOK_DONE.wait, 5)
        await asyncio.sleep(0.1)

        assert await _secret_keys(redis_async, slug) == []
        persisted = json.dumps(await app.state.plugin_manager.logs(slug))
        assert "reload-key" not in persisted


# --- Finding 3: shutdown checkpoint writes survive a reload -----------------


async def test_shutdown_checkpoint_survives_reload(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "checkpoint"
    app = create_app(
        settings=Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data"))),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _CHECKPOINT_PLUGIN, auth_modes=("none",))

        # Bump the in-memory counter through the real MCP wire.
        async with _wire_session(app, slug) as session:
            first = await session.call_tool("bump", {})
        assert "counter" in _text(first)

        # Reload: the old generation's ``shutdown`` must persist the checkpoint.
        reload_response = await client.post(f"/api/plugins/{slug}/reload", headers=AUTH)
        assert reload_response.status_code == 200, reload_response.text
        assert reload_response.json()["load_state"] == "loaded"

        runtime = app.state.plugin_manager.runtime_for(slug)
        assert runtime is not None and runtime.instance is not None
        # The fresh instance read the checkpoint written by ``shutdown``.
        assert runtime.instance.counter == 1
        assert await redis_async.get(f"plugin:{slug}:data:checkpoint") == "1"

        # And the next bump continues from the restored checkpoint.
        async with _wire_session(app, slug) as session:
            second = await session.call_tool("bump", {})
        assert json.loads(_text(second))["counter"] == 2


__all__: list[str] = []


# --- Finding 2b: a *replaced* generation must be sealed too -----------------


async def test_replaced_generation_late_worker_is_contained(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "replacecontain"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _HOOK_PLUGIN, auth_modes=("api_key",))
        assert (await _connect(client, slug, "replace-key")).status_code == 200

        # Promote a new generation through a replace-mode install.
        replace_response = await client.post(
            "/api/plugins/import?mode=replace",
            files={
                "file": (
                    "plugin.zip",
                    build_plugin_zip(
                        slug=slug,
                        version="2.0.0",
                        auth_modes=("api_key",),
                        plugin_source=_HOOK_PLUGIN,
                    ),
                    "application/zip",
                )
            },
            headers=AUTH,
        )
        assert replace_response.status_code == 201, replace_response.text

        module = _module(slug)
        module.BLOCK_HOOK = True
        response = await _connect(client, slug, "replace-key-2")
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        await asyncio.to_thread(module.HOOK_ENTERED.wait, 5)

        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200
        assert await _secret_keys(redis_async, slug) == []

        module.HOOK_RELEASE.set()
        await asyncio.to_thread(module.HOOK_DONE.wait, 5)
        await asyncio.sleep(0.1)

        assert await _secret_keys(redis_async, slug) == []
        persisted = json.dumps(await app.state.plugin_manager.logs(slug))
        assert "replace-key" not in persisted


# --- Shutdown must never re-persist a wiped credential ----------------------


_SHUTDOWN_SECRET_PLUGIN = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

HOOK_ENTERED = threading.Event()
HOOK_RELEASE = threading.Event()
HOOK_DONE = threading.Event()
BLOCK_HOOK = False


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="whoami",
                description="meta",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        if BLOCK_HOOK:
            HOOK_ENTERED.set()
            HOOK_RELEASE.wait(30)
        self.ctx.secrets.set("derived", api_key)
        HOOK_DONE.set()

    def shutdown(self) -> None:
        # A retired generation (or a leaked worker) must not resurrect a
        # credential through a teardown write.
        self.ctx.secrets.set("shutdown_secret", "shutdown-leak-value")

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"slug": self.ctx.slug})
'''


async def test_shutdown_cannot_repersist_wiped_secret(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "shutdownsecret"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _SHUTDOWN_SECRET_PLUGIN, auth_modes=("api_key",))
        old_module = _module(slug)
        old_module.BLOCK_HOOK = True
        assert (await _connect(client, slug, "shutdown-key-A")).status_code == 200
        await asyncio.to_thread(old_module.HOOK_ENTERED.wait, 5)

        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200
        assert await _secret_keys(redis_async, slug) == []

        # Reconnect rebuilds the generation; the retired instance's shutdown hook
        # runs and tries to write a secret, which must be blocked.
        response = await _connect(client, slug, "shutdown-key-B")
        assert response.status_code == 200, response.text
        await asyncio.sleep(0.1)

        assert await redis_async.get(f"plugin:{slug}:secret:shutdown_secret") is None
        assert set(await _secret_keys(redis_async, slug)) <= {
            f"plugin:{slug}:secret:core.api_key",
            f"plugin:{slug}:secret:derived",
        }

        old_module.HOOK_RELEASE.set()
        await asyncio.to_thread(old_module.HOOK_DONE.wait, 5)
        await asyncio.sleep(0.1)
        assert await redis_async.get(f"plugin:{slug}:secret:shutdown_secret") is None


# --- Rejected connect must not disturb a loaded, disconnected endpoint -----


@pytest.mark.parametrize(
    "credentials",
    [{"api_key": "   "}, {}, {"api_key": 7}, {"api_key": None}],
    ids=["whitespace", "missing", "integer", "null"],
)
async def test_rejected_connect_keeps_disconnected_endpoint_mounted(
    credentials: dict[str, Any],
    tmp_path: Any,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
) -> None:
    slug = "rejectconnect"
    app = create_app(
        settings=Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data"))),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _HOOK_PLUGIN, auth_modes=("api_key",))
        assert (await _connect(client, slug, "reject-key-A")).status_code == 200
        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200

        manager = app.state.plugin_manager
        generation_before = manager.generation(slug)
        assert generation_before is not None

        # The loaded-but-disconnected endpoint is still mounted and lists tools.
        async with _wire_session(app, slug) as session:
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                "whoami"
            }

        response = await client.post(
            f"/api/plugins/{slug}/connect",
            json={"mode": "api_key", "credentials": credentials},
            headers=AUTH,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

        # No rebuild, no unmount: same generation, still loaded and reachable.
        assert manager.generation(slug) == generation_before
        detail = await client.get(f"/api/plugins/{slug}", headers=AUTH)
        assert detail.status_code == 200
        body = detail.json()
        assert body["load_state"] == "loaded"
        assert body["status"] == "not_connected"
        assert body["auth_mode"] is None

        async with _wire_session(app, slug) as session:
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                "whoami"
            }


# --- An abandoned (timed-out) shutdown worker must lose its permit ----------


_BLOCKING_SHUTDOWN_PLUGIN = '''\
from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

SHUTDOWN_ENTERED = threading.Event()
SHUTDOWN_RELEASE = threading.Event()
SHUTDOWN_DONE = threading.Event()
BLOCK_SHUTDOWN = False


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        stored = self.ctx.storage.get("checkpoint")
        self.counter = int(stored) if isinstance(stored, int) else 0

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="bump",
                description="bump",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.counter += 1
        return ToolResult.success({"counter": self.counter})

    def shutdown(self) -> None:
        if BLOCK_SHUTDOWN:
            SHUTDOWN_ENTERED.set()
            SHUTDOWN_RELEASE.wait(30)
        self.ctx.storage.set("checkpoint", self.counter)
        SHUTDOWN_DONE.set()
'''


async def test_timed_out_shutdown_worker_cannot_recreate_removed_state(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "shutdownabandon"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _BLOCKING_SHUTDOWN_PLUGIN, auth_modes=("none",))
        module = _module(slug)
        async with _wire_session(app, slug) as session:
            await session.call_tool("bump", {})
        module.BLOCK_SHUTDOWN = True

        # Remove seals the runtime and times out the blocked shutdown hook.
        remove_response = await client.delete(f"/api/plugins/{slug}", headers=AUTH)
        assert remove_response.status_code == 204, remove_response.text
        await asyncio.to_thread(module.SHUTDOWN_ENTERED.wait, 5)

        # Releasing the abandoned worker must not recreate deleted state.
        module.SHUTDOWN_RELEASE.set()
        await asyncio.to_thread(module.SHUTDOWN_DONE.wait, 5)
        await asyncio.sleep(0.1)

        assert await redis_async.get(f"plugin:{slug}:data:checkpoint") is None
        data_keys = [k async for k in redis_async.scan_iter(match=f"plugin:{slug}:*")]
        assert data_keys == []


async def test_timed_out_shutdown_worker_cannot_overwrite_replacement(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "shutdownreplace"
    app = create_app(
        settings=Settings.from_env(
            build_env(DATA_DIR=str(tmp_path / "data"), PLUGIN_CALL_TIMEOUT_SECONDS="1")
        ),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _BLOCKING_SHUTDOWN_PLUGIN, auth_modes=("none",))
        module = _module(slug)
        async with _wire_session(app, slug) as session:
            await session.call_tool("bump", {})
        module.BLOCK_SHUTDOWN = True

        replace_response = await client.post(
            "/api/plugins/import?mode=replace",
            files={
                "file": (
                    "plugin.zip",
                    build_plugin_zip(
                        slug=slug,
                        version="2.0.0",
                        auth_modes=("none",),
                        plugin_source=_BLOCKING_SHUTDOWN_PLUGIN,
                    ),
                    "application/zip",
                )
            },
            headers=AUTH,
        )
        assert replace_response.status_code == 201, replace_response.text
        await asyncio.to_thread(module.SHUTDOWN_ENTERED.wait, 5)

        module.SHUTDOWN_RELEASE.set()
        await asyncio.to_thread(module.SHUTDOWN_DONE.wait, 5)
        await asyncio.sleep(0.1)

        # The abandoned old shutdown write must not appear on the new generation.
        assert await redis_async.get(f"plugin:{slug}:data:checkpoint") is None


# --- A retired generation's shutdown log must not leak a cached credential ---


_CACHED_CREDENTIAL_LOG_PLUGIN = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="whoami",
                description="meta",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        self.key = api_key
        self.ctx.secrets.set("derived", api_key)

    def shutdown(self) -> None:
        self.ctx.storage.set("shutdown_checkpoint", 1)
        self.ctx.log("info", "closing upstream credential " + getattr(self, "key", ""))

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"slug": self.ctx.slug})
'''


async def test_sealed_shutdown_log_cannot_leak_cached_credential(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis, json_logs: Any
) -> None:
    slug = "shutdownlog"
    app = create_app(
        settings=Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data"))),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(
            client, slug, _CACHED_CREDENTIAL_LOG_PLUGIN, auth_modes=("api_key",)
        )
        key_a = "phase4-cached-key-A-987654"
        assert (await _connect(client, slug, key_a)).status_code == 200

        # Disconnect wipes every scoped secret; the instance still caches ``key_a``.
        assert (
            await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        ).status_code == 200

        # Reconnect runs the retired instance's shutdown, which logs ``key_a``.
        response = await _connect(client, slug, "phase4-new-key-B-123456")
        assert response.status_code == 200, response.text
        await asyncio.sleep(0.1)

        assert key_a not in response.text
        detail = await client.get(f"/api/plugins/{slug}", headers=AUTH)
        assert detail.status_code == 200
        assert key_a not in detail.text

        persisted = json.dumps(await app.state.plugin_manager.logs(slug))
        assert key_a not in persisted
        assert key_a not in json.dumps(json_logs.payloads())

        # The shutdown's legitimate *data* checkpoint still persisted.
        assert (
            await redis_async.get(f"plugin:{slug}:data:shutdown_checkpoint") == "1"
        )


# --- Disconnect must redact a cached credential in tool descriptions --------


_CACHED_DESCRIPTION_PLUGIN = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        stored = self.ctx.secrets.get("core.api_key")
        self.key = stored if isinstance(stored, str) else ""

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="whoami",
                description="Upstream credential diagnostic: " + self.key,
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def on_api_key_set(self, api_key: str) -> None:
        self.key = api_key
        self.ctx.secrets.set("derived", api_key)

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"slug": self.ctx.slug})
'''


@pytest.mark.parametrize("change", ["fresh", "reload", "replace"])
async def test_disconnect_redacts_cached_credential_in_descriptions(
    change: str, tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = f"cacheddesc{change}"
    key = f"phase4-cached-description-{change}-987654"
    app = create_app(
        settings=Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data"))),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _CACHED_DESCRIPTION_PLUGIN, auth_modes=("api_key",))
        assert (await _connect(client, slug, key)).status_code == 200

        if change == "reload":
            assert (
                await client.post(f"/api/plugins/{slug}/reload", headers=AUTH)
            ).status_code == 200
        elif change == "replace":
            replace_response = await client.post(
                "/api/plugins/import?mode=replace",
                files={
                    "file": (
                        "plugin.zip",
                        build_plugin_zip(
                            slug=slug,
                            version="2.0.0",
                            auth_modes=("api_key",),
                            plugin_source=_CACHED_DESCRIPTION_PLUGIN,
                        ),
                        "application/zip",
                    )
                },
                headers=AUTH,
            )
            assert replace_response.status_code == 201, replace_response.text

        disconnect = await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
        assert disconnect.status_code == 200
        assert await _secret_keys(redis_async, slug) == []
        assert key not in disconnect.text

        body = disconnect.json()
        description = {t["name"]: t["description"] for t in body["tools"]}["whoami"]
        assert key not in description
        assert description == "Upstream credential diagnostic: ***"

        detail = await client.get(f"/api/plugins/{slug}", headers=AUTH)
        assert detail.status_code == 200
        assert key not in detail.text
        detail_description = {
            t["name"]: t["description"] for t in detail.json()["tools"]
        }["whoami"]
        assert key not in detail_description

        async with _wire_session(app, slug) as session:
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert key not in (listed["whoami"].description or "")

        # A later valid reconnect still works and redacts against the new key.
        assert (
            await _connect(client, slug, "phase4-fresh-key-B-123456")
        ).status_code == 200


async def test_repeated_disconnect_keeps_cached_description_redacted(
    tmp_path: Any, redis_async: AsyncRedis, redis_sync: SyncRedis
) -> None:
    slug = "repeateddisconnect"
    key = "phase4-repeat-disconnect-key-987654"
    app = create_app(
        settings=Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data"))),
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        await _import(client, slug, _CACHED_DESCRIPTION_PLUGIN, auth_modes=("api_key",))
        assert (await _connect(client, slug, key)).status_code == 200

        for _ in range(2):
            response = await client.post(
                f"/api/plugins/{slug}/disconnect", headers=AUTH
            )
            assert response.status_code == 200
            description = {
                t["name"]: t["description"] for t in response.json()["tools"]
            }["whoami"]
            assert key not in description
            assert description == "Upstream credential diagnostic: ***"

        detail = await client.get(f"/api/plugins/{slug}", headers=AUTH)
        assert key not in detail.text
