"""Unit tests for the store-only Phase 1 plugin registry."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from custom_mcp.plugins.manager import (
    PluginExistsError,
    PluginManager,
)
from custom_mcp.plugins.packaging import InvalidPluginError
from custom_mcp.storage.redis_client import AsyncRedis
from tests.helpers import PLUGIN_SOURCE, TEST_BASE_URL, build_plugin_zip

META_KEY = "core:plugin:demo:meta"


def _manager(redis: AsyncRedis, tmp_path: Path) -> PluginManager:
    return PluginManager(
        redis,
        data_dir=tmp_path / "data",
        max_plugin_zip_bytes=2 * 1024 * 1024,
        base_url=TEST_BASE_URL,
    )


async def _meta(redis: AsyncRedis, slug: str = "demo") -> dict[str, Any]:
    raw = await redis.get(f"core:plugin:{slug}:meta")
    assert raw is not None
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


class _FaultyRedis:
    """Delegates to a real fake client and can fail one metadata SET."""

    def __init__(self, inner: AsyncRedis) -> None:
        self.inner = inner
        self.fail_meta_set = False
        self.fail_sadd = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def set(self, name: str, value: str, *, ex: int | None = None) -> object:
        if self.fail_meta_set and name.endswith(":meta"):
            self.fail_meta_set = False
            raise RuntimeError("injected storage fault")
        return await self.inner.set(name, value, ex=ex)

    async def sadd(self, name: str, *values: str) -> int:
        if self.fail_sadd:
            self.fail_sadd = False
            raise RuntimeError("injected membership fault")
        return await self.inner.sadd(name, *values)


async def test_install_stores_original_bytes_extracts_and_registers(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    zip_bytes = build_plugin_zip(slug="demo", version="1.2.3", display_name="Demo")
    record = await manager.install(zip_bytes)

    data_dir = tmp_path / "data"
    assert (data_dir / "packages" / "demo.zip").read_bytes() == zip_bytes
    assert (data_dir / "plugins" / "demo" / "manifest.json").is_file()
    assert (data_dir / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE

    assert record.slug == "demo"
    assert record.load_state == "loading"
    assert record.load_error is None
    assert record.manifest is not None
    assert record.manifest.version == "1.2.3"

    meta = await _meta(redis_async)
    assert meta == {
        "version": "1.2.3",
        "installed_at": meta["installed_at"],
        "auth_mode": None,
        "status": "not_connected",
        "fail_count": 0,
        "transient_error_count": 0,
        "last_ping_at": None,
        "last_error": None,
    }
    assert isinstance(meta["installed_at"], str) and meta["installed_at"]
    assert "demo" in await redis_async.smembers("core:plugins")

    detail = await manager.detail("demo")
    assert detail is not None
    assert detail["endpoint"] == f"{TEST_BASE_URL}/mcp/demo/"
    assert detail["callback_url"] == f"{TEST_BASE_URL}/mcp/demo/oauth/callback"
    assert detail["auth_methods"] == ["none"]
    assert detail["auth_mode"] is None
    assert detail["tools"] == []
    assert detail["secret_names"] == []
    assert detail["logs"] == []


async def test_install_keeps_validator_warnings(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    zip_bytes = build_plugin_zip(extra_files={"extra.py": "import numpy\n"})
    record = await manager.install(zip_bytes)

    assert record.warnings
    assert "numpy" in " ".join(record.warnings)


async def test_duplicate_install_is_rejected(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip())

    with pytest.raises(PluginExistsError):
        await manager.install(build_plugin_zip())


async def test_validation_failure_leaves_no_state(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    with pytest.raises(InvalidPluginError):
        await manager.install(b"this is not a zip archive")

    data_dir = tmp_path / "data"
    assert list((data_dir / "packages").iterdir()) == []
    assert list((data_dir / "plugins").iterdir()) == []
    assert await redis_async.smembers("core:plugins") == set()
    assert await redis_async.get(META_KEY) is None
    assert await manager.detail("demo") is None


async def test_replace_preserves_metadata_and_scoped_state(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))

    meta = await _meta(redis_async)
    meta["auth_mode"] = "api_key"
    meta["installed_at"] = "2020-01-01T00:00:00+00:00"
    await redis_async.set(META_KEY, json.dumps(meta))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "super-secret-token")

    replacement = build_plugin_zip(
        version="2.0.0", plugin_source=PLUGIN_SOURCE + "\n# version two\n"
    )
    await manager.install(replacement, mode="replace")

    data_dir = tmp_path / "data"
    assert (data_dir / "packages" / "demo.zip").read_bytes() == replacement
    assert "# version two" in (data_dir / "plugins" / "demo" / "plugin.py").read_text()

    updated = await _meta(redis_async)
    assert updated["version"] == "2.0.0"
    assert updated["installed_at"] == "2020-01-01T00:00:00+00:00"
    assert updated["auth_mode"] == "api_key"
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "super-secret-token"

    detail = await manager.detail("demo")
    assert detail is not None
    assert detail["secret_names"] == ["token"]
    assert detail["manifest"]["version"] == "2.0.0"


async def test_replace_rolls_back_on_storage_failure(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    faulty = _FaultyRedis(redis_async)
    manager = _manager(cast("AsyncRedis", faulty), tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "super-secret-token")
    meta_before = await redis_async.get(META_KEY)
    package_before = (tmp_path / "data" / "packages" / "demo.zip").read_bytes()

    faulty.fail_meta_set = True
    with pytest.raises(RuntimeError):
        await manager.install(
            build_plugin_zip(version="2.0.0", plugin_source=PLUGIN_SOURCE + "\n# v2\n"),
            mode="replace",
        )

    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == package_before
    extracted = (tmp_path / "data" / "plugins" / "demo" / "plugin.py").read_text()
    assert extracted == PLUGIN_SOURCE
    assert await redis_async.get(META_KEY) == meta_before
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "super-secret-token"
    detail = await manager.detail("demo")
    assert detail is not None
    assert detail["manifest"]["version"] == "1.0.0"


async def test_replace_rolls_back_when_membership_write_fails(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    faulty = _FaultyRedis(redis_async)
    manager = _manager(cast("AsyncRedis", faulty), tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "keep-me")
    meta_before = await redis_async.get(META_KEY)
    package_before = (tmp_path / "data" / "packages" / "demo.zip").read_bytes()

    faulty.fail_sadd = True
    with pytest.raises(RuntimeError):
        await manager.install(
            build_plugin_zip(version="2.0.0"), mode="replace"
        )

    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == package_before
    assert (tmp_path / "data" / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE
    assert await redis_async.get(META_KEY) == meta_before
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "keep-me"
    assert await redis_async.smembers("core:plugins") == {"demo"}
    detail = await manager.detail("demo")
    assert detail is not None
    assert detail["manifest"]["version"] == "1.0.0"


async def test_replace_rolls_back_package_on_disk_failure(
    redis_async: AsyncRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    package_before = (tmp_path / "data" / "packages" / "demo.zip").read_bytes()
    meta_before = await redis_async.get(META_KEY)

    def _boom(state: Any, live_dir: Path, scratch_dir: Path, backup_dir: Path) -> None:
        raise OSError("injected disk fault")

    monkeypatch.setattr(manager, "_publish_directory_state", _boom)
    with pytest.raises(OSError):
        await manager.install(build_plugin_zip(version="2.0.0"), mode="replace")

    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == package_before
    assert (tmp_path / "data" / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE
    assert await redis_async.get(META_KEY) == meta_before
    assert "demo" in await redis_async.smembers("core:plugins")


async def test_remove_only_the_selected_slug_namespace(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(slug="foo"))
    await manager.install(build_plugin_zip(slug="foobar"))

    await redis_async.set("plugin:foo:data:key", "foo-data")
    await redis_async.set("plugin:foo:secret:token", "foo-secret")
    await redis_async.set("plugin:foo:future:key", "foo-future")
    await redis_async.set("plugin:foobar:data:key", "foobar-data")
    await redis_async.set("plugin:foobar:secret:token", "foobar-secret")
    await redis_async.set("plugin:foobar:future:key", "foobar-future")
    await redis_async.set(
        "core:oauth_state:one", json.dumps({"slug": "foo", "created_at": 1})
    )
    await redis_async.set(
        "core:oauth_state:two", json.dumps({"slug": "foobar", "created_at": 1})
    )
    await redis_async.set("core:session:abc", "{}")
    await cast("Any", redis_async).lpush("core:logs:foo", "{}")

    assert await manager.remove("foo") is True

    data_dir = tmp_path / "data"
    assert not (data_dir / "packages" / "foo.zip").exists()
    assert not (data_dir / "plugins" / "foo").exists()
    assert (data_dir / "packages" / "foobar.zip").exists()
    assert (data_dir / "plugins" / "foobar").exists()

    assert await redis_async.get("plugin:foo:data:key") is None
    assert await redis_async.get("plugin:foo:secret:token") is None
    assert await redis_async.get("plugin:foo:future:key") is None
    assert await redis_async.get("core:plugin:foo:meta") is None
    assert await redis_async.lrange("core:logs:foo", 0, -1) == []
    assert await redis_async.get("core:oauth_state:one") is None
    assert await redis_async.get("plugin:foobar:data:key") == "foobar-data"
    assert await redis_async.get("plugin:foobar:secret:token") == "foobar-secret"
    assert await redis_async.get("plugin:foobar:future:key") == "foobar-future"
    assert await redis_async.get("core:oauth_state:two") is not None
    assert await redis_async.get("core:session:abc") is not None
    assert await redis_async.smembers("core:plugins") == {"foobar"}
    assert await manager.detail("foo") is None
    assert await manager.detail("foobar") is not None


@pytest.mark.parametrize("blocked_step", ["directory", "package_zip"])
async def test_cancelled_remove_holds_lock_until_delete_worker_settles(
    redis_async: AsyncRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_step: str,
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    data_dir = tmp_path / "data"
    live_dir = data_dir / "plugins" / "demo"
    package_path = data_dir / "packages" / "demo.zip"
    replacement = build_plugin_zip(
        version="2.0.0", plugin_source=PLUGIN_SOURCE + "\n# version two\n"
    )

    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    mutation_finished = asyncio.Event()
    replacement_lock_requested = asyncio.Event()
    publish_entered = asyncio.Event()
    publish_after_mutation: bool | None = None
    release_worker = threading.Event()

    original_lock_for = manager._lock_for

    async def tracked_lock_for(slug: str) -> asyncio.Lock:
        if worker_started.is_set():
            replacement_lock_requested.set()
        return await original_lock_for(slug)

    monkeypatch.setattr(manager, "_lock_for", tracked_lock_for)

    original_publish = manager._publish

    async def tracked_publish(
        slug: str, validated: Any, zip_bytes: bytes, **kwargs: Any
    ) -> Any:
        nonlocal publish_after_mutation
        publish_entered.set()
        publish_after_mutation = mutation_finished.is_set()
        return await original_publish(slug, validated, zip_bytes, **kwargs)

    monkeypatch.setattr(manager, "_publish", tracked_publish)

    def pause_worker() -> None:
        loop.call_soon_threadsafe(worker_started.set)
        if not release_worker.wait(timeout=10):
            raise RuntimeError("test did not release the delete worker")

    if blocked_step == "directory":
        original_remove_tree = manager._remove_tree

        def blocking_remove_tree(path: Path) -> None:
            if path == live_dir:
                pause_worker()
            original_remove_tree(path)
            if path == live_dir:
                loop.call_soon_threadsafe(mutation_finished.set)

        monkeypatch.setattr(manager, "_remove_tree", blocking_remove_tree)
    else:
        original_unlink = manager._unlink_if_exists

        def blocking_unlink(path: Path) -> None:
            if path == package_path:
                pause_worker()
            original_unlink(path)
            if path == package_path:
                loop.call_soon_threadsafe(mutation_finished.set)

        monkeypatch.setattr(manager, "_unlink_if_exists", blocking_unlink)

    remove_task = asyncio.create_task(manager.remove("demo"))
    await asyncio.wait_for(worker_started.wait(), timeout=5)
    remove_task.cancel()
    remove_task.cancel()

    replace_task = asyncio.create_task(manager.install(replacement, mode="replace"))
    await asyncio.wait_for(replacement_lock_requested.wait(), timeout=5)
    assert not publish_entered.is_set()
    assert not replace_task.done()

    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(remove_task, timeout=5)
    await asyncio.wait_for(replace_task, timeout=5)

    assert publish_after_mutation is True
    assert package_path.read_bytes() == replacement
    assert "# version two" in (live_dir / "plugin.py").read_text()
    meta = await _meta(redis_async)
    assert meta["version"] == "2.0.0"
    assert await redis_async.smembers("core:plugins") == {"demo"}


@pytest.mark.parametrize("slug", ["Bad", "-demo", "demo_", "a" * 41, "../demo"])
async def test_invalid_slugs_are_rejected_at_entrypoints(
    redis_async: AsyncRedis, tmp_path: Path, slug: str
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    assert await manager.detail(slug) is None
    assert await manager.remove(slug) is False


async def test_startup_reextracts_original_package_bytes(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    zip_bytes = build_plugin_zip(version="3.0.0")
    first = _manager(redis_async, tmp_path)
    await first.startup()
    await first.install(zip_bytes)
    meta = await _meta(redis_async)
    meta["auth_mode"] = "oauth"
    meta["installed_at"] = "2021-02-03T04:05:06+00:00"
    await redis_async.set(META_KEY, json.dumps(meta))

    extracted = tmp_path / "data" / "plugins" / "demo" / "plugin.py"
    extracted.write_text("# tampered on disk\n")

    second = _manager(redis_async, tmp_path)
    await second.startup()

    assert extracted.read_text() == PLUGIN_SOURCE
    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == zip_bytes
    detail = await second.detail("demo")
    assert detail is not None
    assert detail["load_state"] == "loading"
    assert detail["manifest"]["version"] == "3.0.0"
    meta_after = await _meta(redis_async)
    assert meta_after["auth_mode"] == "oauth"
    assert meta_after["installed_at"] == "2021-02-03T04:05:06+00:00"


async def test_startup_isolates_invalid_packages(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    packages = data_dir / "packages"
    packages.mkdir(parents=True)
    (packages / "broken.zip").write_bytes(b"not a zip archive")
    await redis_async.sadd("core:plugins", "broken", "ghost")

    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    detail = await manager.detail("broken")
    assert detail is not None
    assert detail["load_state"] == "load_failed"
    assert detail["manifest"] is None
    assert "valid ZIP" in (detail["load_error"] or "")
    assert not (data_dir / "plugins" / "broken").exists()
    # Load-failed packages stay installed/removable; stale "ghost" is cleared.
    assert await redis_async.smembers("core:plugins") == {"broken"}


async def test_startup_clears_membership_when_packages_are_gone(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip())
    assert await redis_async.smembers("core:plugins") == {"demo"}

    (tmp_path / "data" / "packages" / "demo.zip").unlink()

    restarted = _manager(redis_async, tmp_path)
    await restarted.startup()
    assert await restarted.detail("demo") is None
    assert await redis_async.smembers("core:plugins") == set()


async def test_startup_filename_must_match_manifest_slug(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    packages = data_dir / "packages"
    packages.mkdir(parents=True)
    (packages / "wrong.zip").write_bytes(build_plugin_zip(slug="demo"))

    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    detail = await manager.detail("wrong")
    assert detail is not None
    assert detail["load_state"] == "load_failed"
    assert detail["manifest"] is None
    assert not (data_dir / "plugins" / "wrong").exists()


async def test_scratch_cleanup_only_removes_owned_entries(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    tmp_dir = data_dir / "tmp"
    tmp_dir.mkdir(parents=True)
    owned = tmp_dir / f"extract-{'a' * 32}"
    owned.mkdir()
    (owned / "leftover").write_text("x")
    unrelated = tmp_dir / "notes.txt"
    unrelated.write_text("keep me")

    manager = _manager(redis_async, tmp_path)
    await manager.startup()

    assert not owned.exists()
    assert unrelated.read_text() == "keep me"


@pytest.mark.parametrize("child", ["packages", "plugins", "tmp"])
async def test_unsafe_data_subdirectory_symlink_fails_without_external_touch(
    redis_async: AsyncRedis, tmp_path: Path, child: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("keep me")
    (data_dir / child).symlink_to(external, target_is_directory=True)

    manager = _manager(redis_async, tmp_path)
    with pytest.raises(RuntimeError, match="symlink"):
        await manager.startup()

    assert marker.read_text() == "keep me"
    assert [entry.name for entry in external.iterdir()] == ["keep.txt"]


async def test_cancelled_replace_after_package_publish_restores_old(
    redis_async: AsyncRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "keep-me")
    meta_before = await redis_async.get(META_KEY)
    package_before = (tmp_path / "data" / "packages" / "demo.zip").read_bytes()

    started = asyncio.Event()

    async def _block_before_metadata(
        slug: str, manifest: Any, old_meta_raw: str | None
    ) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_register_metadata", _block_before_metadata)
    task = asyncio.create_task(
        manager.install(build_plugin_zip(version="2.0.0"), mode="replace")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == package_before
    assert (tmp_path / "data" / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE
    assert await redis_async.get(META_KEY) == meta_before
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "keep-me"
    assert await redis_async.smembers("core:plugins") == {"demo"}
    detail = await manager.detail("demo")
    assert detail is not None
    assert detail["manifest"]["version"] == "1.0.0"


async def test_cancelled_replace_after_metadata_write_restores_old(
    redis_async: AsyncRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(version="1.0.0"))
    meta = await _meta(redis_async)
    meta["auth_mode"] = "api_key"
    meta["installed_at"] = "2020-01-01T00:00:00+00:00"
    await redis_async.set(META_KEY, json.dumps(meta))
    await redis_async.set("plugin:demo:data:counter", "7")
    await redis_async.set("plugin:demo:secret:token", "keep-me")
    meta_before = await redis_async.get(META_KEY)
    package_before = (tmp_path / "data" / "packages" / "demo.zip").read_bytes()

    started = asyncio.Event()
    original = manager._register_metadata

    async def _block_after_metadata(
        slug: str, manifest: Any, old_meta_raw: str | None
    ) -> None:
        await original(slug, manifest, old_meta_raw)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_register_metadata", _block_after_metadata)
    task = asyncio.create_task(
        manager.install(build_plugin_zip(version="2.0.0"), mode="replace")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (tmp_path / "data" / "packages" / "demo.zip").read_bytes() == package_before
    assert (tmp_path / "data" / "plugins" / "demo" / "plugin.py").read_text() == PLUGIN_SOURCE
    assert await redis_async.get(META_KEY) == meta_before
    assert await redis_async.get("plugin:demo:data:counter") == "7"
    assert await redis_async.get("plugin:demo:secret:token") == "keep-me"
    assert await redis_async.smembers("core:plugins") == {"demo"}


async def test_cancelled_new_install_leaves_no_corruption(
    redis_async: AsyncRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    started = asyncio.Event()

    async def _block(slug: str, manifest: Any, old_meta_raw: str | None) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_register_metadata", _block)
    task = asyncio.create_task(manager.install(build_plugin_zip()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    data_dir = tmp_path / "data"
    assert list((data_dir / "packages").iterdir()) == []
    assert list((data_dir / "plugins").iterdir()) == []
    assert list((data_dir / "tmp").iterdir()) == []
    assert await redis_async.get(META_KEY) is None
    assert await redis_async.smembers("core:plugins") == set()
    assert await manager.detail("demo") is None


async def test_summaries_include_standard_fields(
    redis_async: AsyncRedis, tmp_path: Path
) -> None:
    manager = _manager(redis_async, tmp_path)
    await manager.startup()
    await manager.install(build_plugin_zip(slug="demo", display_name="Demo Plugin"))
    meta = await _meta(redis_async)
    meta.update(
        {
            "auth_mode": "api_key",
            "last_ping_at": "2024-01-02T03:04:05+00:00",
            "last_error": "transient upstream failure",
        }
    )
    await redis_async.set(META_KEY, json.dumps(meta))

    rows = await manager.summaries()
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "demo"
    assert row["display_name"] == "Demo Plugin"
    assert row["version"] == "1.0.0"
    assert row["auth_methods"] == ["none"]
    assert row["auth_modes"] == ["none"]
    assert row["auth_mode"] == "api_key"
    assert row["status"] == "not_connected"
    assert row["load_state"] == "loading"
    assert row["tool_names"] == []
    assert row["last_ping_at"] == "2024-01-02T03:04:05+00:00"
    assert row["last_error"] == "transient upstream failure"
