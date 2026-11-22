"""Unit tests for immutable per-slug runtime state."""

from __future__ import annotations

import asyncio

from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import AuthMode, PluginContext, Status


class _Storage:
    def get(self, key: str, default: object = None) -> object:
        return default

    def set(self, key: str, value: object, ttl: int | None = None) -> None:
        return None

    def delete(self, key: str) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return []


def _manifest(slug: str = "demo") -> Manifest:
    return Manifest(
        name=slug,
        version="1.0.0",
        entry="plugin:Plugin",
        auth_modes=(AuthMode.NONE,),
        display_name=slug,
        description="",
    )


def _context(slug: str = "demo") -> PluginContext:
    return PluginContext(
        slug=slug,
        storage=_Storage(),
        secrets=_Storage(),
        log=lambda level, message: None,
        callback_url=f"https://mcp.example.com/mcp/{slug}/oauth/callback",
    )


def test_load_state_values() -> None:
    assert LoadState.LOADING.value == "loading"
    assert LoadState.LOADED.value == "loaded"
    assert LoadState.LOAD_FAILED.value == "load_failed"
    assert LoadState.UNLOADING.value == "unloading"
    assert isinstance(LoadState.LOADING, str)


def test_runtime_accepts_stable_lock_and_semaphore() -> None:
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(4)
    runtime = PluginRuntime(
        slug="demo",
        manifest=_manifest(),
        context=_context(),
        lock=lock,
        sem=sem,
        generation=7,
    )
    assert runtime.lock is lock
    assert runtime.sem is sem
    assert runtime.generation == 7
    assert runtime.runtime_id
    assert runtime.runtimeid == runtime.runtime_id
    assert runtime.load_state is LoadState.LOADING
    assert runtime.load_error is None
    assert runtime.instance is None
    assert runtime.tools == []
    assert runtime.auth_methods == []
    assert runtime.mounted is None
    assert runtime.status is Status.NOT_CONNECTED
    assert runtime.meta == {}
    assert runtime.fail_count == 0
    assert runtime.transient_error_count == 0
    assert runtime.leaked_workers == 0


def test_runtime_generations_are_isolated() -> None:
    first = PluginRuntime(
        slug="demo",
        manifest=_manifest(),
        context=_context(),
        lock=asyncio.Lock(),
        sem=asyncio.Semaphore(4),
        generation=1,
    )
    second = PluginRuntime(
        slug="demo",
        manifest=_manifest(),
        context=_context(),
        lock=first.lock,
        sem=first.sem,
        generation=2,
    )
    assert first.runtime_id != second.runtime_id
    assert first.generation != second.generation
    assert first.context is not second.context
    assert first.tools is not second.tools
