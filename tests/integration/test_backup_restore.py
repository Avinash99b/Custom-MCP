"""End-to-end backup/restore tests (Phase 6, slice 6a).

These drive the real app (fake Redis, real host/loader/manager, bearer auth)
through ``GET /api/backup`` and ``POST /api/restore`` so snapshotting under the
lifecycle lock, phase-A validation, exact-wipe replacement and the reconnect
policy are all exercised together.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from custom_mcp.config import Settings
from custom_mcp.storage.redis_client import AsyncRedis
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
API_KEY = "restore-api-key-value-1234"

_APIKEY_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

CONNECT_CALLS: list[str] = []
RESTORE_OBSERVED: list[int] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        key = self.ctx.secrets.get("core.api_key")
        tools = [
            Tool(
                name="whoami",
                description="Return identity.",
                input_schema={"type": "object", "properties": {}},
            )
        ]
        if isinstance(key, str) and key:
            tools.append(
                Tool(
                    name="secret",
                    description="Present only with a key.",
                    input_schema={"type": "object", "properties": {}},
                )
            )
        return tools

    def connect(self):
        CONNECT_CALLS.append("connect")
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def on_restore(self):
        RESTORE_OBSERVED.append(len(CONNECT_CALLS))

    def invoke_tool(self, name, args):
        return ToolResult.success({"ok": True})
'''

_ONRESTORE_RAISES_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

RESTORE_ATTEMPTED: list[bool] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def on_restore(self):
        RESTORE_ATTEMPTED.append(True)
        raise RuntimeError("restore boom")

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown_tool", "unknown tool")
'''

_CRASHING_CONSTRUCTOR_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, PluginContext, Status, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: PluginContext) -> None:
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
        return ToolResult.failure("unknown_tool", "unknown tool")
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=TEST_BASE_URL
        ) as client:
            yield client


@pytest_asyncio.fixture
async def plugin_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with _running(app) as client:
        yield client


# --- helpers ----------------------------------------------------------------


def _open_zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _descriptor(*slugs: str, encrypted: Any = False) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "core_version": "0.1.0",
            "encrypted": encrypted,
            "plugins": [
                {"slug": slug, "version": "1.0.0", "has_secrets": False} for slug in slugs
            ],
        }
    ).encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _restore_zip(
    *,
    descriptor: bytes | None = None,
    package: bytes | None = None,
    include_package: bool = True,
    data: bytes = b"{}",
    secrets: bytes = b"{}",
    meta: bytes | None = None,
) -> bytes:
    entries: dict[str, bytes] = {"backup.json": descriptor or _descriptor("demo")}
    if include_package:
        entries["plugins/demo/plugin.zip"] = (
            package if package is not None else build_plugin_zip(slug="demo")
        )
    entries["plugins/demo/data.json"] = data
    entries["plugins/demo/secrets.json"] = secrets
    entries["plugins/demo/meta.json"] = meta or json.dumps(
        {"auth_mode": None, "version": "1.0.0"}
    ).encode()
    return _zip(entries)


async def _import(
    client: httpx.AsyncClient, zip_bytes: bytes
) -> httpx.Response:
    return await client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", zip_bytes, "application/zip")},
        headers=AUTH,
    )


async def _backup(
    client: httpx.AsyncClient, *, plugins: str | None = None
) -> httpx.Response:
    params = {"plugins": plugins} if plugins is not None else None
    return await client.get("/api/backup", params=params, headers=AUTH)


async def _restore(
    client: httpx.AsyncClient,
    data: bytes,
    *,
    mode: str = "skip",
    slugs: str | None = None,
) -> httpx.Response:
    params: dict[str, str] = {"mode": mode}
    if slugs is not None:
        params["slugs"] = slugs
    return await client.post(
        "/api/restore",
        params=params,
        files={"file": ("backup.zip", data, "application/zip")},
        headers=AUTH,
    )


async def _connect_api_key(client: httpx.AsyncClient, slug: str) -> httpx.Response:
    return await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "api_key", "credentials": {"api_key": API_KEY}},
        headers=AUTH,
    )


async def _state(redis: AsyncRedis, settings: Settings) -> dict[str, Any]:
    data_dir = Path(settings.DATA_DIR)
    keys = sorted([key async for key in redis.scan_iter(match="*")])
    packages = data_dir / "packages"
    plugins = data_dir / "plugins"
    return {
        "keys": keys,
        "members": sorted(await redis.smembers("core:plugins")),
        "meta": await redis.get("core:plugin:demo:meta"),
        "data": await redis.get("plugin:demo:data:keep"),
        "packages": sorted(p.name for p in packages.iterdir()) if packages.is_dir() else [],
        "plugins": sorted(p.name for p in plugins.iterdir()) if plugins.is_dir() else [],
    }


# --- 1. byte-identical package export ---------------------------------------


async def test_export_package_bytes_are_identical(
    plugin_client: httpx.AsyncClient, settings: Settings
) -> None:
    zip_bytes = build_plugin_zip(slug="demo", version="1.2.3")
    assert (await _import(plugin_client, zip_bytes)).status_code == 201

    response = await _backup(plugin_client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="custom-mcp-backup-'
    )

    with _open_zip(response.content) as archive:
        assert archive.read("plugins/demo/plugin.zip") == zip_bytes
    assert (
        Path(settings.DATA_DIR) / "packages" / "demo.zip"
    ).read_bytes() == zip_bytes


# --- 2. round trip of data, secrets, auth mode and version ------------------


async def test_round_trip_data_secrets_mode_version(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(
            slug="roundtrip", auth_modes=("api_key",), plugin_source=_APIKEY_SOURCE
        ),
    )
    assert imported.status_code == 201
    assert (await _connect_api_key(plugin_client, "roundtrip")).status_code == 200
    await redis_async.set("plugin:roundtrip:data:counter", json.dumps(7))
    await redis_async.set("plugin:roundtrip:secret:token", json.dumps("tok"))

    backup = await _backup(plugin_client)
    assert backup.status_code == 200
    assert (
        await plugin_client.delete("/api/plugins/roundtrip", headers=AUTH)
    ).status_code == 204
    assert await redis_async.get("core:plugin:roundtrip:meta") is None

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    body = response.json()
    assert body["restored"] == ["roundtrip"]
    assert body["results"][0]["status"] == "connected"

    assert await redis_async.get("plugin:roundtrip:data:counter") == json.dumps(7)
    assert await redis_async.get("plugin:roundtrip:secret:core.api_key") == json.dumps(
        API_KEY
    )
    assert await redis_async.get("plugin:roundtrip:secret:token") == json.dumps("tok")
    meta = json.loads(await redis_async.get("core:plugin:roundtrip:meta") or "{}")
    assert meta["auth_mode"] == "api_key"
    assert meta["version"] == "1.0.0"


# --- 3. TTL keys excluded ---------------------------------------------------


async def test_ttl_keys_are_excluded(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    await redis_async.set("plugin:demo:data:keep", json.dumps(1))
    await redis_async.set("plugin:demo:data:temp", json.dumps(2), ex=3600)
    await redis_async.set("plugin:demo:secret:keep", json.dumps("k"))
    await redis_async.set("plugin:demo:secret:temp", json.dumps("t"), ex=3600)

    backup = await _backup(plugin_client)
    assert backup.status_code == 200
    with _open_zip(backup.content) as archive:
        data = json.loads(archive.read("plugins/demo/data.json"))
        secrets = json.loads(archive.read("plugins/demo/secrets.json"))
    assert data == {"keep": 1}
    assert secrets == {"keep": "k"}


# --- 4. plaintext archive ---------------------------------------------------


async def test_backup_is_plaintext_and_declares_unencrypted(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (
        await _import(
            plugin_client,
            build_plugin_zip(
                slug="plaintext", auth_modes=("api_key",), plugin_source=_APIKEY_SOURCE
            ),
        )
    ).status_code == 201
    assert (await _connect_api_key(plugin_client, "plaintext")).status_code == 200

    backup = await _backup(plugin_client)
    with _open_zip(backup.content) as archive:
        descriptor = json.loads(archive.read("backup.json"))
        raw_secrets = archive.read("plugins/plaintext/secrets.json")
    assert descriptor["format_version"] == 1
    assert descriptor["encrypted"] is False
    # The credential is readable plaintext in the archive.
    assert API_KEY.encode() in raw_secrets


# --- 5. subset export and unknown slug --------------------------------------


async def test_subset_export_and_unknown_slug(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="hello"))).status_code == 201
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201

    subset = await _backup(plugin_client, plugins="hello")
    assert subset.status_code == 200
    with _open_zip(subset.content) as archive:
        names = set(archive.namelist())
        descriptor = json.loads(archive.read("backup.json"))
    assert [entry["slug"] for entry in descriptor["plugins"]] == ["hello"]
    assert "plugins/hello/plugin.zip" in names
    assert not any(name.startswith("plugins/demo/") for name in names)

    unknown = await _backup(plugin_client, plugins="missing")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    assert unknown.headers["cache-control"] == "no-store"
    assert not unknown.content.startswith(b"PK")


# --- 6. Phase A writes nothing ----------------------------------------------


async def test_invalid_archives_write_nothing(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis, settings: Settings
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    await redis_async.set("plugin:demo:data:keep", json.dumps(1))
    before = await _state(redis_async, settings)

    variants = [
        b"not a zip archive",
        _zip({"backup.json": b"{not json"}),
        _restore_zip(include_package=False),
        _restore_zip(descriptor=_descriptor("demo", encrypted=True)),
        _restore_zip(data=json.dumps({"bad key": 1}).encode()),
        _restore_zip(package=b"not a zip"),
        _restore_zip(meta=json.dumps({"auth_mode": 5, "version": "1.0.0"}).encode()),
        _zip({"backup.json": _descriptor("demo"), "plugins/demo/plugin.zip": b"x"}),
    ]

    for data in variants:
        response = await _restore(plugin_client, data, mode="replace")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_plugin"
        assert response.headers["cache-control"] == "no-store"
        assert await _state(redis_async, settings) == before


# --- 7. encrypted rejected --------------------------------------------------


async def test_encrypted_backup_is_rejected(plugin_client: httpx.AsyncClient) -> None:
    data = _restore_zip(descriptor=_descriptor("demo", encrypted=True))
    response = await _restore(plugin_client, data, mode="replace")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin"


# --- 8. skip vs replace vs selective ----------------------------------------


async def test_skip_replace_and_selective_restore(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="alpha"))).status_code == 201
    assert (await _import(plugin_client, build_plugin_zip(slug="beta"))).status_code == 201
    await redis_async.set("plugin:alpha:data:k", json.dumps("v1"))
    await redis_async.set("plugin:beta:data:k", json.dumps("v1"))
    backup = await _backup(plugin_client)
    assert backup.status_code == 200

    await redis_async.set("plugin:alpha:data:k", json.dumps("changed"))
    await redis_async.set("plugin:beta:data:k", json.dumps("changed"))

    skipped = await _restore(plugin_client, backup.content, mode="skip")
    assert skipped.status_code == 200
    assert sorted(skipped.json()["skipped"]) == ["alpha", "beta"]
    assert skipped.json()["restored"] == []
    assert await redis_async.get("plugin:alpha:data:k") == json.dumps("changed")
    assert await redis_async.get("plugin:beta:data:k") == json.dumps("changed")

    replaced = await _restore(plugin_client, backup.content, mode="replace")
    assert replaced.status_code == 200
    assert sorted(replaced.json()["restored"]) == ["alpha", "beta"]
    assert await redis_async.get("plugin:alpha:data:k") == json.dumps("v1")
    assert await redis_async.get("plugin:beta:data:k") == json.dumps("v1")

    await redis_async.set("plugin:alpha:data:k", json.dumps("changed2"))
    await redis_async.set("plugin:beta:data:k", json.dumps("changed2"))
    selective = await _restore(
        plugin_client, backup.content, mode="replace", slugs="alpha"
    )
    assert selective.status_code == 200
    assert selective.json()["restored"] == ["alpha"]
    assert [entry["slug"] for entry in selective.json()["results"]] == ["alpha"]
    assert await redis_async.get("plugin:alpha:data:k") == json.dumps("v1")
    assert await redis_async.get("plugin:beta:data:k") == json.dumps("changed2")


async def test_restore_unknown_filter_slug_is_404(
    plugin_client: httpx.AsyncClient,
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    backup = await _backup(plugin_client)
    response = await _restore(plugin_client, backup.content, mode="replace", slugs="missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- 9. exact wipe on replace -----------------------------------------------


async def test_replace_wipes_absent_keys(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    await redis_async.set("plugin:demo:data:keep", json.dumps(1))
    backup = await _backup(plugin_client)

    await redis_async.set("plugin:demo:data:keep", json.dumps("changed"))
    await redis_async.set("plugin:demo:data:extra", json.dumps(2))

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    assert response.json()["restored"] == ["demo"]
    assert await redis_async.get("plugin:demo:data:keep") == json.dumps(1)
    assert await redis_async.get("plugin:demo:data:extra") is None


# --- 10. per-plugin isolation -----------------------------------------------


async def test_per_plugin_failure_is_isolated(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="good"))).status_code == 201
    bad = build_plugin_zip(
        slug="bad", plugin_source=_CRASHING_CONSTRUCTOR_SOURCE
    )
    assert (await _import(plugin_client, bad)).status_code == 201
    detail = await plugin_client.get("/api/plugins/bad", headers=AUTH)
    assert detail.json()["load_state"] == "load_failed"

    backup = await _backup(plugin_client)
    assert backup.status_code == 200
    assert (await plugin_client.delete("/api/plugins/good", headers=AUTH)).status_code == 204
    assert (await plugin_client.delete("/api/plugins/bad", headers=AUTH)).status_code == 204

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    body = response.json()
    assert body["failed"] == ["bad"]
    assert body["restored"] == ["good"]
    outcomes = {entry["slug"]: entry["outcome"] for entry in body["results"]}
    assert outcomes == {"bad": "failed", "good": "restored"}

    good_detail = await plugin_client.get("/api/plugins/good", headers=AUTH)
    assert good_detail.status_code == 200
    assert good_detail.json()["load_state"] == "loaded"


# --- 11. on_restore failure is non-fatal ------------------------------------


async def test_on_restore_failure_is_non_fatal(
    plugin_client: httpx.AsyncClient,
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(slug="resilient", plugin_source=_ONRESTORE_RAISES_SOURCE),
    )
    assert imported.status_code == 201
    backup = await _backup(plugin_client)
    assert (await plugin_client.delete("/api/plugins/resilient", headers=AUTH)).status_code == 204

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    body = response.json()
    assert body["restored"] == ["resilient"]
    assert body["failed"] == []
    assert body["results"][0]["status"] == "connected"

    module = sys.modules["mcp_plugin_resilient.plugin"]
    assert module.RESTORE_ATTEMPTED == [True]


# --- 12. no TTL, disconnected first, then reconnect -------------------------


async def test_restore_no_ttl_starts_disconnected_then_reconnects(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(
            slug="reconnect", auth_modes=("api_key",), plugin_source=_APIKEY_SOURCE
        ),
    )
    assert imported.status_code == 201
    assert (await _connect_api_key(plugin_client, "reconnect")).status_code == 200
    await redis_async.set("plugin:reconnect:data:counter", json.dumps(3))

    backup = await _backup(plugin_client)
    assert (await plugin_client.delete("/api/plugins/reconnect", headers=AUTH)).status_code == 204

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    body = response.json()
    assert body["restored"] == ["reconnect"]
    assert body["results"][0]["status"] == "connected"

    # Restored values carry no TTL.
    assert await redis_async.ttl("plugin:reconnect:data:counter") == -1
    assert await redis_async.ttl("plugin:reconnect:secret:core.api_key") == -1

    module = sys.modules["mcp_plugin_reconnect.plugin"]
    # ``on_restore`` observed zero connects: the plugin was installed without
    # auto-connect and its status started ``not_connected``.
    assert module.RESTORE_OBSERVED == [0]
    # It was reconnected exactly once afterwards, with the stored auth mode.
    assert module.CONNECT_CALLS == ["connect"]
    meta = json.loads(await redis_async.get("core:plugin:reconnect:meta") or "{}")
    assert meta["status"] == "connected"
    assert meta["auth_mode"] == "api_key"


# --- request validation -----------------------------------------------------


async def test_missing_file_is_422(plugin_client: httpx.AsyncClient) -> None:
    response = await plugin_client.post("/api/restore", headers=AUTH)
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] == "invalid_request"


async def test_invalid_mode_is_422(plugin_client: httpx.AsyncClient) -> None:
    backup = await _backup(plugin_client)
    assert backup.status_code == 200
    response = await _restore(plugin_client, backup.content, mode="sideways")
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] == "invalid_request"


async def test_backup_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/backup")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


__all__ = ["plugin_client"]


# --- transactional replace and exact auth-mode restore ----------------------


_NONE_SOURCE = '''\
from __future__ import annotations

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult

CONNECT_CALLS: list[str] = []
RESTORE_OBSERVED: list[int] = []


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        CONNECT_CALLS.append("connect")
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def on_restore(self):
        RESTORE_OBSERVED.append(len(CONNECT_CALLS))

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown_tool", "unknown tool")
'''


async def test_failed_replace_retains_the_working_plugin(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    # A working plugin with state ...
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    await redis_async.set("plugin:demo:data:keep", json.dumps("original"))

    # ... replaced by a valid-looking package whose constructor always crashes.
    broken = _restore_zip(
        descriptor=_descriptor("demo"),
        package=build_plugin_zip(
            slug="demo", plugin_source=_CRASHING_CONSTRUCTOR_SOURCE
        ),
        data=json.dumps({"keep": "restored"}).encode(),
    )
    response = await _restore(plugin_client, broken, mode="replace")
    assert response.status_code == 200
    assert response.json()["failed"] == ["demo"]
    assert response.json()["restored"] == []

    # The original code and data must survive a failed replace.
    detail = await plugin_client.get("/api/plugins/demo", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["load_state"] == "loaded"
    assert await redis_async.get("plugin:demo:data:keep") == json.dumps("original")


async def test_restore_resets_auth_mode_to_the_snapshot(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(
            slug="modekeep", auth_modes=("api_key",), plugin_source=_APIKEY_SOURCE
        ),
    )
    assert imported.status_code == 201
    # Snapshot with no selected mode.
    backup = await _backup(plugin_client)
    assert backup.status_code == 200

    # The live plugin is connected after the snapshot.
    assert (await _connect_api_key(plugin_client, "modekeep")).status_code == 200
    meta = json.loads(await redis_async.get("core:plugin:modekeep:meta") or "{}")
    assert meta["auth_mode"] == "api_key"

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    assert response.json()["restored"] == ["modekeep"]

    meta = json.loads(await redis_async.get("core:plugin:modekeep:meta") or "{}")
    assert meta["auth_mode"] is None
    assert meta["status"] == "not_connected"
    assert await redis_async.get("plugin:modekeep:secret:core.api_key") is None


async def test_none_auth_installs_without_auto_connect_then_connects(
    plugin_client: httpx.AsyncClient,
) -> None:
    imported = await _import(
        plugin_client, build_plugin_zip(slug="noautoconnect", plugin_source=_NONE_SOURCE)
    )
    assert imported.status_code == 201
    backup = await _backup(plugin_client)
    removed = await plugin_client.delete("/api/plugins/noautoconnect", headers=AUTH)
    assert removed.status_code == 204

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    assert response.json()["restored"] == ["noautoconnect"]
    assert response.json()["results"][0]["status"] == "connected"

    module = sys.modules["mcp_plugin_noautoconnect.plugin"]
    # ``on_restore`` ran before any connect, so it observed zero connects.
    assert module.RESTORE_OBSERVED == [0]
    assert module.CONNECT_CALLS == ["connect"]


# --- 13. restore over live secrets does not leak a cached credential --------


_CACHED_DESC_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        stored = self.ctx.secrets.get("core.api_key")
        self.key = stored if isinstance(stored, str) else ""

    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        return [
            Tool(
                name="whoami",
                description="cached: " + self.key,
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def on_api_key_set(self, api_key):
        # Deliberately does NOT refresh ``self.key``: the constructor-cached value
        # (the pre-restore live secret) is what a later description would expose.
        self.ctx.secrets.set("derived", api_key)

    def invoke_tool(self, name, args):
        return ToolResult.success({"ok": True})
'''


async def _connect_key(client: httpx.AsyncClient, slug: str, key: str) -> httpx.Response:
    return await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "api_key", "credentials": {"api_key": key}},
        headers=AUTH,
    )


async def test_restore_over_secrets_does_not_leak_cached_credential(
    plugin_client: httpx.AsyncClient,
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(
            slug="cacheddesc", auth_modes=("api_key",), plugin_source=_CACHED_DESC_SOURCE
        ),
    )
    assert imported.status_code == 201
    # Snapshot the plugin while connected with key A.
    first = await _connect_key(plugin_client, "cacheddesc", "key-A-snapshot-111111")
    assert first.status_code == 200
    backup = await _backup(plugin_client)
    assert backup.status_code == 200
    # The live credential changes to B; the replacement constructor will cache B.
    assert (await _connect_key(plugin_client, "cacheddesc", "key-B-live-222222")).status_code == 200

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    assert response.json()["restored"] == ["cacheddesc"]
    # A later connect with a brand-new key must not resurface B either.
    assert (await _connect_key(plugin_client, "cacheddesc", "key-C-next-333333")).status_code == 200

    detail = await plugin_client.get("/api/plugins/cacheddesc", headers=AUTH)
    assert detail.status_code == 200
    assert "key-B-live-222222" not in detail.text
    assert "key-C-next-333333" not in detail.text


# --- 14. complete outer-archive validation ----------------------------------


def _raw_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def _valid_members(slug: str = "demo") -> list[tuple[str, bytes]]:
    return [
        ("backup.json", _descriptor(slug)),
        (f"plugins/{slug}/plugin.zip", build_plugin_zip(slug=slug)),
        (f"plugins/{slug}/data.json", b"{}"),
        (f"plugins/{slug}/secrets.json", b"{}"),
        (f"plugins/{slug}/meta.json", b'{"auth_mode":null,"version":"1.0.0"}'),
    ]


async def test_outer_archive_validation_rejects_extras_dupes_and_corruption(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="demo"))).status_code == 201
    await redis_async.set("plugin:demo:data:keep", json.dumps(1))
    before = await redis_async.get("plugin:demo:data:keep")

    # An unexpected extra member.
    extra = _raw_zip([*_valid_members(), ("unexpected.txt", b"garbage")])
    # A duplicate required member.
    duplicate = _raw_zip([*_valid_members(), ("plugins/demo/data.json", b"{}")])
    # Corrupt STORED bytes in a required member (CRC mismatch on read).
    corrupted = bytearray(_raw_zip(_valid_members()))
    offset = corrupted.find(b'{"auth_mode":null,"version":"1.0.0"}')
    assert offset != -1
    corrupted[offset] ^= 0xFF
    corrupt = bytes(corrupted)

    for data in (extra, duplicate, corrupt):
        response = await _restore(plugin_client, data, mode="replace")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_plugin"
        assert await redis_async.get("plugin:demo:data:keep") == before


# --- 15. concurrent restores of one plugin are serialized -------------------


def _archive(slug: str, version: str, marker: str, key: str) -> bytes:
    descriptor = json.dumps(
        {
            "format_version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "core_version": "0.1.0",
            "encrypted": False,
            "plugins": [{"slug": slug, "version": version, "has_secrets": True}],
        }
    ).encode()
    return _zip(
        {
            "backup.json": descriptor,
            f"plugins/{slug}/plugin.zip": build_plugin_zip(
                slug=slug, version=version, auth_modes=("api_key",), plugin_source=_APIKEY_SOURCE
            ),
            f"plugins/{slug}/data.json": json.dumps({"marker": marker}).encode(),
            f"plugins/{slug}/secrets.json": json.dumps({"core.api_key": key}).encode(),
            f"plugins/{slug}/meta.json": json.dumps(
                {"auth_mode": "api_key", "version": version}
            ).encode(),
        }
    )


async def test_concurrent_restores_of_one_plugin_are_serialized(
    app: Starlette,
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_v2 = _archive("conc", "2.0.0", "marker-A", "key-A")
    archive_v3 = _archive("conc", "3.0.0", "marker-B", "key-B")

    service = app.state.restore
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service._call_on_restore
    calls = {"n": 0}

    async def blocking_on_restore(slug: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await release.wait()
        await original(slug)

    monkeypatch.setattr(service, "_call_on_restore", blocking_on_restore)

    task_a = asyncio.create_task(_restore(plugin_client, archive_v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), 5)
    task_b = asyncio.create_task(_restore(plugin_client, archive_v3, mode="replace"))
    # Give B a chance to run: it must be blocked behind A's per-slug restore lock,
    # so the installed version is still A's.
    await asyncio.sleep(0.1)
    paused = await plugin_client.get("/api/plugins/conc", headers=AUTH)
    assert paused.json()["manifest"]["version"] == "2.0.0"

    release.set()
    responses = await asyncio.gather(task_a, task_b)
    assert [r.status_code for r in responses] == [200, 200]

    final = await plugin_client.get("/api/plugins/conc", headers=AUTH)
    assert final.json()["manifest"]["version"] == "3.0.0"
    marker = await redis_async.get("plugin:conc:data:marker")
    stored_key = await redis_async.get("plugin:conc:secret:core.api_key")
    assert json.loads(marker or '""') == "marker-B"
    assert json.loads(stored_key or '""') == "key-B"


# --- 16. constructor secret rotation must not leak the cached original ------


_ROTATING_SOURCE = '''\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult


class Plugin(MCPPlugin):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        cached = self.ctx.secrets.get("token", "")
        self.cached = cached if isinstance(cached, str) else ""
        if self.cached:
            # Rotate the live secret; the constructor still holds the original.
            self.ctx.secrets.set("token", "dummy-constructor-replacement")

    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return [
            Tool(
                name="whoami",
                description="cached: " + self.cached,
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def connect(self):
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({"ok": True})
'''


async def test_constructor_rotation_does_not_leak_cached_secret(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    imported = await _import(
        plugin_client,
        build_plugin_zip(slug="rotating", plugin_source=_ROTATING_SOURCE),
    )
    assert imported.status_code == 201
    backup = await _backup(plugin_client)
    assert backup.status_code == 200

    original = "dummy-constructor-cached-prewipe-token"
    await redis_async.set("plugin:rotating:secret:token", json.dumps(original))

    response = await _restore(plugin_client, backup.content, mode="replace")
    assert response.status_code == 200
    assert response.json()["restored"] == ["rotating"]
    assert await redis_async.get("plugin:rotating:secret:token") != json.dumps(original)

    detail = await plugin_client.get("/api/plugins/rotating", headers=AUTH)
    assert detail.status_code == 200
    assert original not in detail.text


# --- 17. restore is serialized with ordinary lifecycle operations -----------


async def test_restore_serializes_with_concurrent_import(
    app: Starlette,
    plugin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_v2 = _archive("life", "2.0.0", "marker-A", "key-A")
    service = app.state.restore
    entered = asyncio.Event()
    release = asyncio.Event()
    original = service._call_on_restore
    seen = {"n": 0}

    async def blocking_on_restore(slug: str) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            entered.set()
            await release.wait()
        await original(slug)

    monkeypatch.setattr(service, "_call_on_restore", blocking_on_restore)

    task_a = asyncio.create_task(_restore(plugin_client, archive_v2, mode="replace"))
    await asyncio.wait_for(entered.wait(), 5)

    # A concurrent import of the same slug must block behind A's held lock.
    import_task = asyncio.create_task(
        plugin_client.post(
            "/api/plugins/import?mode=replace",
            files={
                "file": (
                    "plugin.zip",
                    build_plugin_zip(
                        slug="life",
                        version="3.0.0",
                        auth_modes=("api_key",),
                        plugin_source=_APIKEY_SOURCE,
                    ),
                    "application/zip",
                )
            },
            headers=AUTH,
        )
    )
    await asyncio.sleep(0.1)
    assert not import_task.done()

    release.set()
    restore_response, import_response = await asyncio.gather(task_a, import_task)
    assert restore_response.status_code == 200
    assert import_response.status_code == 201
    # The import ran strictly after the restore, so v3 is the final generation.
    detail = await plugin_client.get("/api/plugins/life", headers=AUTH)
    assert detail.json()["manifest"]["version"] == "3.0.0"


# --- 18. a cancelled restore settles its state-write worker under the lock --


async def test_cancelled_restore_settles_write_worker_under_lock(
    app: Starlette,
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = app.state.restore
    started = threading.Event()
    release = threading.Event()
    original_write = service._write_state
    calls = {"n": 0}

    def blocking_write(slug: str, data: Any, secrets: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            release.wait(10)
        original_write(slug, data, secrets)

    monkeypatch.setattr(service, "_write_state", blocking_write)

    archive_a = _archive("cancelrace", "2.0.0", "data-A", "key-A")
    archive_b = _archive("cancelrace", "3.0.0", "data-B", "key-B")

    task_a = asyncio.create_task(_restore(plugin_client, archive_a, mode="replace"))
    # Wait until A's worker is blocked inside its state write.
    assert await asyncio.to_thread(started.wait, 5)

    task_a.cancel()
    await asyncio.sleep(0.05)
    # The cancelled restore must not finish until its worker settles (the lock is
    # still held), so a later restore cannot run and then be overwritten.
    assert not task_a.done()

    task_b = asyncio.create_task(_restore(plugin_client, archive_b, mode="replace"))
    await asyncio.sleep(0.1)
    assert not task_b.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task_a
    response_b = await task_b
    assert response_b.status_code == 200

    detail = await plugin_client.get("/api/plugins/cancelrace", headers=AUTH)
    assert detail.json()["manifest"]["version"] == "3.0.0"
    marker = await redis_async.get("plugin:cancelrace:data:marker")
    key = await redis_async.get("plugin:cancelrace:secret:core.api_key")
    assert json.loads(marker or '""') == "data-B"
    assert json.loads(key or '""') == "key-B"


# --- 19. a post-commit install cancellation still retains pre-secrets -------


_ROTATING_ARCHIVE_DESCRIPTOR = json.dumps(
    {
        "format_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "core_version": "0.1.0",
        "encrypted": False,
        "plugins": [{"slug": "rotcancel", "version": "1.0.0", "has_secrets": True}],
    }
).encode()


async def test_cancelled_install_retains_pre_constructor_secrets(
    app: Starlette,
    plugin_client: httpx.AsyncClient,
    redis_async: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = app.state.plugin_manager
    service = app.state.restore
    live = "dummy-live-token-to-be-replaced"
    archive = _zip(
        {
            "backup.json": _ROTATING_ARCHIVE_DESCRIPTOR,
            "plugins/rotcancel/plugin.zip": build_plugin_zip(
                slug="rotcancel", plugin_source=_ROTATING_SOURCE
            ),
            "plugins/rotcancel/data.json": b"{}",
            "plugins/rotcancel/secrets.json": json.dumps({"token": "snapshot-token"}).encode(),
            "plugins/rotcancel/meta.json": json.dumps(
                {"auth_mode": None, "version": "1.0.0"}
            ).encode(),
        }
    )
    # The replacement constructor will cache this live token and then rotate it.
    await redis_async.set("plugin:rotcancel:secret:token", json.dumps(live))

    real_install = manager.install

    async def cancelling_install(
        zip_bytes: bytes, *, mode: str = "reject", auto_connect: bool = True
    ) -> Any:
        await real_install(zip_bytes, mode=mode, auto_connect=auto_connect)
        # Simulate a cancellation delivered after the install committed.
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "install", cancelling_install)

    with pytest.raises(asyncio.CancelledError):
        await service.restore(archive, mode="replace")

    runtime = manager.runtime_for("rotcancel")
    assert runtime is not None
    # The pre-constructor credential the surviving runtime cached must be retained
    # so a later connect cannot expose it in a tool description.
    assert live in runtime.retained_secrets
