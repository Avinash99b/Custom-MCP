"""ASGI integration tests for the Phase 1 plugin registry API."""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
import starlette.formparsers as formparsers
from starlette.applications import Starlette
from starlette.requests import Request

from custom_mcp.api import plugins as plugins_api
from custom_mcp.config import Settings
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.helpers import (
    PLUGIN_SOURCE,
    TEST_BASE_URL,
    TEST_SECRET,
    build_env,
    build_plugin_zip,
)

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
META_KEY = "core:plugin:demo:meta"


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


def _zip_from_entries(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _traversal_zip() -> bytes:
    return _zip_from_entries(
        {
            "manifest.json": json.dumps(
                {
                    "name": "demo",
                    "version": "1.0.0",
                    "entry": "plugin:Plugin",
                    "auth_modes": ["none"],
                }
            ),
            "plugin.py": PLUGIN_SOURCE,
            "../evil.py": "print('escape')\n",
        }
    )


async def _import(
    client: httpx.AsyncClient,
    data: bytes,
    *,
    mode: str | None = None,
    query_mode: str | None = None,
    headers: dict[str, str] | None = None,
    filename: str = "plugin.zip",
) -> httpx.Response:
    payload: dict[str, str] = {}
    if mode is not None:
        payload["mode"] = mode
    params = {"mode": query_mode} if query_mode is not None else None
    return await client.post(
        "/api/plugins/import",
        files={"file": (filename, data, "application/zip")},
        data=payload,
        params=params,
        headers=AUTH if headers is None else headers,
    )


# --- import, list and detail ------------------------------------------------


async def test_import_list_and_detail(
    plugin_client: httpx.AsyncClient,
    settings: Settings,
    redis_async: AsyncRedis,
) -> None:
    zip_bytes = build_plugin_zip(
        slug="demo", version="1.2.3", display_name="Demo Plugin", description="Demo"
    )
    response = await _import(plugin_client, zip_bytes)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["slug"] == "demo"
    assert body["manifest"]["name"] == "demo"
    assert body["manifest"]["version"] == "1.2.3"
    assert body["load_state"] == "loaded"
    assert body["load_error"] is None
    assert body["status"] == "not_connected"
    assert body["auth_methods"] == ["none"]
    assert body["auth_mode"] is None
    assert body["tools"] == []
    assert body["endpoint"] == f"{TEST_BASE_URL}/mcp/demo/"
    assert body["endpoint_url"] == f"{TEST_BASE_URL}/mcp/demo/"
    assert body["mcp_endpoint_url"] == f"{TEST_BASE_URL}/mcp/demo/"
    assert body["callback_url"] == f"{TEST_BASE_URL}/mcp/demo/oauth/callback"
    assert body["warnings"] == []
    assert body["logs"] == []

    data_dir = Path(settings.DATA_DIR)
    assert (data_dir / "packages" / "demo.zip").read_bytes() == zip_bytes
    assert (data_dir / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE

    meta = json.loads(await redis_async.get(META_KEY) or "{}")
    assert meta["version"] == "1.2.3"
    assert meta["auth_mode"] is None
    assert meta["status"] == "not_connected"
    assert meta["fail_count"] == 0
    assert meta["transient_error_count"] == 0
    assert meta["last_ping_at"] is None
    assert meta["last_error"] is None
    assert await redis_async.smembers("core:plugins") == {"demo"}

    listing = await plugin_client.get("/api/plugins", headers=AUTH)
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    rows = listing.json()["plugins"]
    assert len(rows) == 1
    assert rows[0]["slug"] == "demo"
    assert rows[0]["display_name"] == "Demo Plugin"
    assert rows[0]["version"] == "1.2.3"
    assert rows[0]["auth_modes"] == ["none"]
    assert rows[0]["tool_names"] == []
    assert rows[0]["status"] == "not_connected"
    assert rows[0]["load_state"] == "loaded"

    detail = await plugin_client.get("/api/plugins/demo", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["endpoint"] == f"{TEST_BASE_URL}/mcp/demo/"


async def test_nested_zip_import(plugin_client: httpx.AsyncClient) -> None:
    response = await _import(
        plugin_client, build_plugin_zip(slug="nested", nested=True)
    )
    assert response.status_code == 201
    assert response.json()["slug"] == "nested"
    assert response.json()["manifest"]["name"] == "nested"


async def test_import_exposes_warnings_but_not_secret_values(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    response = await _import(
        plugin_client, build_plugin_zip(extra_files={"extra.py": "import numpy\n"})
    )
    assert response.status_code == 201
    assert response.json()["warnings"]
    assert "numpy" in " ".join(response.json()["warnings"])

    await redis_async.set("plugin:demo:secret:api_token", "do-not-leak-this-value")
    detail = await plugin_client.get("/api/plugins/demo", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["secret_names"] == ["api_token"]
    assert "do-not-leak-this-value" not in detail.text


async def test_duplicate_import_conflicts(plugin_client: httpx.AsyncClient) -> None:
    assert (await _import(plugin_client, build_plugin_zip())).status_code == 201
    duplicate = await _import(plugin_client, build_plugin_zip())
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "plugin_exists"
    assert duplicate.headers["cache-control"] == "no-store"


async def test_query_mode_replace_updates_package(
    plugin_client: httpx.AsyncClient, settings: Settings
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(version="1.0.0"))).status_code == 201
    replacement = build_plugin_zip(version="2.0.0")
    response = await _import(plugin_client, replacement, query_mode="replace")
    assert response.status_code == 201
    assert response.json()["manifest"]["version"] == "2.0.0"
    assert (Path(settings.DATA_DIR) / "packages" / "demo.zip").read_bytes() == replacement


async def test_query_mode_invalid_returns_422_and_writes_nothing(
    plugin_client: httpx.AsyncClient, settings: Settings, redis_async: AsyncRedis
) -> None:
    response = await _import(plugin_client, build_plugin_zip(), query_mode="sideways")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert list((Path(settings.DATA_DIR) / "packages").iterdir()) == []
    assert await redis_async.smembers("core:plugins") == set()


async def test_query_mode_takes_precedence_over_form(
    plugin_client: httpx.AsyncClient, settings: Settings
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(version="1.0.0"))).status_code == 201
    replacement = build_plugin_zip(version="2.0.0")
    response = await _import(
        plugin_client, replacement, mode="reject", query_mode="replace"
    )
    assert response.status_code == 201
    assert response.json()["manifest"]["version"] == "2.0.0"
    assert (Path(settings.DATA_DIR) / "packages" / "demo.zip").read_bytes() == replacement


async def test_replace_preserves_data_and_secrets(
    plugin_client: httpx.AsyncClient, redis_async: AsyncRedis
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(version="1.0.0"))).status_code == 201
    meta = json.loads(await redis_async.get(META_KEY) or "{}")
    meta["auth_mode"] = "api_key"
    meta["installed_at"] = "2020-01-01T00:00:00+00:00"
    await redis_async.set(META_KEY, json.dumps(meta))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "keep-this-secret")

    replacement = build_plugin_zip(
        version="2.0.0", plugin_source=PLUGIN_SOURCE + "\n# version two\n"
    )
    response = await _import(plugin_client, replacement, mode="replace")
    assert response.status_code == 201
    assert response.json()["manifest"]["version"] == "2.0.0"
    assert response.json()["auth_mode"] == "api_key"

    updated = json.loads(await redis_async.get(META_KEY) or "{}")
    assert updated["version"] == "2.0.0"
    assert updated["auth_mode"] == "api_key"
    assert updated["installed_at"] == "2020-01-01T00:00:00+00:00"
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "keep-this-secret"


# --- invalid requests -------------------------------------------------------


async def test_invalid_archive_returns_422_and_writes_nothing(
    plugin_client: httpx.AsyncClient, settings: Settings, redis_async: AsyncRedis
) -> None:
    response = await _import(plugin_client, b"not a zip archive")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin"
    assert response.headers["cache-control"] == "no-store"

    data_dir = Path(settings.DATA_DIR)
    assert list((data_dir / "packages").iterdir()) == []
    assert list((data_dir / "plugins").iterdir()) == []
    assert await redis_async.smembers("core:plugins") == set()
    assert await redis_async.get(META_KEY) is None


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (build_plugin_zip(slug="Bad"), "slug"),
        (build_plugin_zip(manifest_extra={"version": "1.0"}), "version"),
        (
            build_plugin_zip(manifest_extra={"min_core_version": "9.9.9"}),
            "requires core version",
        ),
        (_traversal_zip(), "unsafe path"),
    ],
)
async def test_invalid_plugin_reasons_are_precise(
    plugin_client: httpx.AsyncClient,
    settings: Settings,
    redis_async: AsyncRedis,
    data: bytes,
    fragment: str,
) -> None:
    response = await _import(plugin_client, data)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_plugin"
    assert fragment.lower() in error["message"].lower()
    assert list((Path(settings.DATA_DIR) / "packages").iterdir()) == []
    assert await redis_async.smembers("core:plugins") == set()


async def test_missing_file_returns_422(plugin_client: httpx.AsyncClient) -> None:
    response = await plugin_client.post(
        "/api/plugins/import", data={"mode": "reject"}, headers=AUTH
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_too_many_files_returns_422(plugin_client: httpx.AsyncClient) -> None:
    response = await plugin_client.post(
        "/api/plugins/import",
        files={
            "file": ("one.zip", build_plugin_zip(), "application/zip"),
            "other": ("two.zip", build_plugin_zip(), "application/zip"),
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_unknown_mode_returns_422_and_writes_nothing(
    plugin_client: httpx.AsyncClient, settings: Settings, redis_async: AsyncRedis
) -> None:
    response = await _import(plugin_client, build_plugin_zip(), mode="sideways")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert list((Path(settings.DATA_DIR) / "packages").iterdir()) == []
    assert await redis_async.smembers("core:plugins") == set()


async def test_oversized_upload_returns_413(
    settings: Settings,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
    tmp_path: Path,
) -> None:
    small_settings = Settings.from_env(
        build_env(
            DATA_DIR=str(tmp_path / "small-data"),
            MAX_PLUGIN_ZIP_MB="1",
        )
    )
    app = create_app(
        settings=small_settings,
        redis_async=redis_async,
        redis_sync=redis_sync,
    )
    async with _running(app) as client:
        response = await _import(client, b"x" * 1_500_000)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_body_oversize_closes_multipart_spooled_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []

    class _TrackingSpooled:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inner = tempfile.SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115
            self.closed = False
            created.append(self)

        def write(self, data: bytes) -> Any:
            return self._inner.write(data)

        def seek(self, *args: Any) -> Any:
            return self._inner.seek(*args)

        def close(self) -> None:
            self.closed = True
            self._inner.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", _TrackingSpooled)
    boundary = "----testboundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    chunks = [prefix + b"x" * 100, b"y" * 4000]

    async def receive() -> dict[str, Any]:
        if chunks:
            body = chunks.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/plugins/import",
        "raw_path": b"/api/plugins/import",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (
                b"content-type",
                f"multipart/form-data; boundary={boundary}".encode(),
            )
        ],
        "server": ("mcp.example.com", 443),
        "client": ("127.0.0.1", 12345),
        "state": {},
    }
    request = Request(scope, receive=plugins_api._BoundedReceive(receive, 2000))

    with pytest.raises(plugins_api._BodyTooLargeError):
        await plugins_api._parse_multipart(
            request,
            max_files=1,
            max_fields=8,
            max_part_size=64 * 1024,
        )

    assert created
    assert all(item.closed for item in created)


async def test_bad_slug_paths_are_not_found(plugin_client: httpx.AsyncClient) -> None:
    for slug in ["Bad", "demo_", "-demo", f"{'a' * 41}"]:
        response = await plugin_client.get(f"/api/plugins/{slug}", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        deleted = await plugin_client.delete(f"/api/plugins/{slug}", headers=AUTH)
        assert deleted.status_code == 404


async def test_unknown_slug_detail_is_404(plugin_client: httpx.AsyncClient) -> None:
    response = await plugin_client.get("/api/plugins/missing", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- removal ----------------------------------------------------------------


async def test_delete_removes_only_target_state(
    plugin_client: httpx.AsyncClient,
    settings: Settings,
    redis_async: AsyncRedis,
) -> None:
    assert (await _import(plugin_client, build_plugin_zip(slug="foo"))).status_code == 201
    assert (
        await _import(plugin_client, build_plugin_zip(slug="foobar"))
    ).status_code == 201
    await redis_async.set("plugin:foo:data:key", "foo")
    await redis_async.set("plugin:foo:secret:token", "foo-secret")
    await redis_async.set("plugin:foobar:data:key", "foobar")
    await redis_async.set("core:session:keep", "{}")

    response = await plugin_client.delete("/api/plugins/foo", headers=AUTH)
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"

    data_dir = Path(settings.DATA_DIR)
    assert not (data_dir / "packages" / "foo.zip").exists()
    assert not (data_dir / "plugins" / "foo").exists()
    assert (data_dir / "packages" / "foobar.zip").exists()
    assert await redis_async.get("plugin:foo:data:key") is None
    assert await redis_async.get("plugin:foo:secret:token") is None
    assert await redis_async.get("core:plugin:foo:meta") is None
    assert await redis_async.get("plugin:foobar:data:key") == "foobar"
    assert await redis_async.get("core:session:keep") is not None
    assert await redis_async.smembers("core:plugins") == {"foobar"}

    assert (
        await plugin_client.get("/api/plugins/foo", headers=AUTH)
    ).status_code == 404


# --- authentication and no-store --------------------------------------------


async def test_api_requires_authentication(plugin_client: httpx.AsyncClient) -> None:
    listing = await plugin_client.get("/api/plugins")
    assert listing.status_code == 401
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()["error"]["code"] == "unauthorized"

    uploaded = await plugin_client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", build_plugin_zip(), "application/zip")},
    )
    assert uploaded.status_code == 401
    assert uploaded.json()["error"]["code"] == "unauthorized"


# --- startup ----------------------------------------------------------------


async def test_lifespan_restart_reextracts_original_bytes(
    app: Starlette,
    settings: Settings,
    redis_async: AsyncRedis,
    redis_sync: SyncRedis,
) -> None:
    zip_bytes = build_plugin_zip(version="4.0.0")
    async with _running(app) as client:
        assert (await _import(client, zip_bytes)).status_code == 201

    extracted = Path(settings.DATA_DIR) / "plugins" / "demo" / "plugin.py"
    extracted.write_text("# tampered outside the registry\n")

    # A real restart builds a fresh app (a new host/executor) over the same
    # Redis and data directory.
    restarted = create_app(
        settings=settings, redis_async=redis_async, redis_sync=redis_sync
    )
    async with _running(restarted) as client:
        detail = await client.get("/api/plugins/demo", headers=AUTH)
        assert detail.status_code == 200
        assert detail.json()["manifest"]["version"] == "4.0.0"
        assert detail.json()["load_state"] == "loaded"

    assert extracted.read_text() == PLUGIN_SOURCE
    assert (Path(settings.DATA_DIR) / "packages" / "demo.zip").read_bytes() == zip_bytes


async def test_startup_shows_invalid_packages_as_load_failed(
    app: Starlette, settings: Settings
) -> None:
    packages = Path(settings.DATA_DIR) / "packages"
    packages.mkdir(parents=True)
    (packages / "broken.zip").write_bytes(b"not a zip archive")

    async with _running(app) as client:
        listing = await client.get("/api/plugins", headers=AUTH)
        assert listing.status_code == 200
        rows = cast("list[dict[str, Any]]", listing.json()["plugins"])
        assert len(rows) == 1
        assert rows[0]["slug"] == "broken"
        assert rows[0]["load_state"] == "load_failed"
        assert rows[0]["load_error"]


__all__ = ["plugin_client"]
