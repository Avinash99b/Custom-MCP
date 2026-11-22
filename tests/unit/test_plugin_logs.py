"""Per-plugin log capture, capping and secret redaction."""

from __future__ import annotations

import json
import logging

import pytest

from custom_mcp.plugins.logs import DEFAULT_RECENT_LIMIT, LOG_CAP, LogError, PluginLogs
from custom_mcp.storage.manager import StorageManager
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis

SECRET = "supersecretvalue"


@pytest.fixture
def logs(redis_sync: SyncRedis, redis_async: AsyncRedis) -> PluginLogs:
    return PluginLogs(redis_sync, redis_async)


@pytest.fixture
def manager(redis_sync: SyncRedis, redis_async: AsyncRedis) -> StorageManager:
    return StorageManager(redis_sync, redis_async)


def seed_secret(manager: StorageManager, slug: str, key: str, value: str) -> None:
    manager.scoped(slug, secret=True, allow_reserved=True).set(key, value)


# --- basic capture ----------------------------------------------------------


async def test_write_sync_and_recent(logs: PluginLogs) -> None:
    logs.write_sync("demo", "info", "hello")
    entries = await logs.recent("demo")
    assert entries[0]["message"] == "hello"
    assert entries[0]["level"] == "info"
    assert "timestamp" in entries[0]


async def test_callback_logs(logs: PluginLogs) -> None:
    callback = logs.callback("demo")
    callback("warning", "warned")
    entries = await logs.recent("demo")
    assert entries[0]["level"] == "warning"
    assert entries[0]["message"] == "warned"


async def test_async_write(logs: PluginLogs) -> None:
    await logs.write("demo", "error", "boom")
    entries = await logs.recent("demo")
    assert entries[0]["level"] == "error"


async def test_newest_first(logs: PluginLogs) -> None:
    logs.write_sync("demo", "info", "first")
    logs.write_sync("demo", "info", "second")
    entries = await logs.recent("demo")
    assert [entry["message"] for entry in entries] == ["second", "first"]


def test_invalid_level_rejected(logs: PluginLogs) -> None:
    with pytest.raises(LogError):
        logs.write_sync("demo", "trace", "x")


def test_non_string_message_rejected(logs: PluginLogs) -> None:
    with pytest.raises(LogError):
        logs.write_sync("demo", "info", 123)  # type: ignore[arg-type]


def test_invalid_slug_rejected(logs: PluginLogs) -> None:
    with pytest.raises(ValueError):
        logs.write_sync("Bad Slug", "info", "x")


# --- cap and limit ----------------------------------------------------------


async def test_log_cap(logs: PluginLogs, redis_sync: SyncRedis) -> None:
    for index in range(LOG_CAP + 20):
        logs.write_sync("demo", "info", f"m{index}")
    raw = redis_sync.lrange("core:logs:demo", 0, -1)
    assert len(raw) == LOG_CAP
    entries = await logs.recent("demo", 1)
    assert entries[0]["message"] == f"m{LOG_CAP + 19}"


async def test_recent_default_limit(logs: PluginLogs) -> None:
    for index in range(DEFAULT_RECENT_LIMIT + 10):
        logs.write_sync("demo", "info", f"m{index}")
    assert len(await logs.recent("demo")) == DEFAULT_RECENT_LIMIT


async def test_recent_skips_malformed(logs: PluginLogs, redis_sync: SyncRedis) -> None:
    redis_sync.lpush("core:logs:demo", "not json")
    assert await logs.recent("demo") == []


@pytest.mark.parametrize("limit", [0, -1])
async def test_recent_non_positive_returns_empty(logs: PluginLogs, limit: int) -> None:
    logs.write_sync("demo", "info", "m")
    assert await logs.recent("demo", limit) == []


async def test_recent_limit_capped(logs: PluginLogs) -> None:
    for index in range(LOG_CAP + 20):
        logs.write_sync("demo", "info", f"m{index}")
    assert len(await logs.recent("demo", 300)) == LOG_CAP


@pytest.mark.parametrize("limit", [2.5, True, None])
async def test_recent_invalid_limit_rejected(logs: PluginLogs, limit: object) -> None:
    with pytest.raises(LogError):
        await logs.recent("demo", limit)  # type: ignore[arg-type]


async def test_slug_isolation(logs: PluginLogs, redis_sync: SyncRedis) -> None:
    logs.write_sync("foo", "info", "foo-log")
    logs.write_sync("foobar", "info", "foobar-log")
    assert [entry["message"] for entry in await logs.recent("foo")] == ["foo-log"]
    assert [entry["message"] for entry in await logs.recent("foobar")] == ["foobar-log"]
    assert redis_sync.lrange("core:logs:foo", 0, -1) != redis_sync.lrange(
        "core:logs:foobar", 0, -1
    )


# --- redaction --------------------------------------------------------------


async def test_redaction_before_redis(
    logs: PluginLogs, manager: StorageManager, redis_sync: SyncRedis
) -> None:
    seed_secret(manager, "demo", "core.api_key", SECRET)
    logs.write_sync("demo", "info", f"token={SECRET}")
    raw = redis_sync.lrange("core:logs:demo", 0, -1)[0]
    assert SECRET not in raw
    entry = json.loads(raw)
    assert entry["message"] == "token=***"


async def test_redact_sync_and_async(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "k", SECRET)
    assert logs.redact_sync("demo", f"x {SECRET} y") == "x *** y"
    assert await logs.redact("demo", f"x {SECRET} y") == "x *** y"


async def test_longer_secret_replaced_first(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "a", "abcdef")
    seed_secret(manager, "demo", "b", "abcdefgh")
    logs.write_sync("demo", "info", "abcdefgh")
    entries = await logs.recent("demo")
    assert entries[0]["message"] == "***"


async def test_nested_metadata_redacted(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "k", SECRET)
    logs.write_sync("demo", "info", "safe", metadata={"outer": [SECRET]})
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"outer": ["***"]}


async def test_tuple_and_dict_key_redacted(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "k", SECRET)
    logs.write_sync("demo", "info", "safe", metadata={"outer": ("x", SECRET), SECRET: "v"})
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"outer": ["x", "***"], "***": "v"}


async def test_short_secrets_not_redacted(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "k", "abc")
    logs.write_sync("demo", "info", "abc")
    entries = await logs.recent("demo")
    assert entries[0]["message"] == "abc"


async def test_numeric_key_and_value_redacted_sync(
    logs: PluginLogs, manager: StorageManager, redis_sync: SyncRedis
) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456")
    logs.write_sync("demo", "info", "safe", metadata={123456: "safe", "token": 123456})
    raw = redis_sync.lrange("core:logs:demo", 0, -1)[0]
    assert "123456" not in raw
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"***": "safe", "token": "***"}


async def test_numeric_key_and_value_redacted_async(
    logs: PluginLogs, manager: StorageManager, redis_sync: SyncRedis
) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456")
    await logs.write("demo", "info", "safe", metadata={123456: "safe", "token": 123456})
    raw = redis_sync.lrange("core:logs:demo", 0, -1)[0]
    assert "123456" not in raw
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"***": "safe", "token": "***"}


async def test_float_secret_representation_redacted(
    logs: PluginLogs, manager: StorageManager
) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456.5")
    logs.write_sync("demo", "info", "safe", metadata={123456.5: "v", "n": 123456.5})
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"***": "v", "n": "***"}


async def test_numeric_looking_string_partial_redacted(
    logs: PluginLogs, manager: StorageManager
) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456")
    logs.write_sync("demo", "info", "safe", metadata={"token": "1234567"})
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == {"token": "***7"}


async def test_safe_scalar_types_preserved(logs: PluginLogs, manager: StorageManager) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456")
    metadata = {"count": 5, "ratio": 1.5, "ok": True, "nothing": None, "list": [1, "x"]}
    logs.write_sync("demo", "info", "safe", metadata=metadata)
    entries = await logs.recent("demo")
    assert entries[0]["metadata"] == metadata


@pytest.mark.parametrize(
    "metadata",
    [{(1, 2): "x"}, {"k": float("inf")}, {"k": object()}],
)
def test_unsupported_metadata_rejected(logs: PluginLogs, metadata: object) -> None:
    with pytest.raises(LogError):
        logs.write_sync("demo", "info", "safe", metadata=metadata)


def test_cyclic_metadata_rejected(logs: PluginLogs) -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(LogError):
        logs.write_sync("demo", "info", "safe", metadata={"c": cyclic})


def test_metadata_leak_not_in_stdout(
    logs: PluginLogs, manager: StorageManager, caplog: pytest.LogCaptureFixture
) -> None:
    seed_secret(manager, "demo", "core.api_key", "123456")
    with caplog.at_level(logging.INFO):
        logs.write_sync("demo", "info", "safe", metadata={123456: 123456})
    assert "123456" not in caplog.text


def test_stdout_is_redacted(
    logs: PluginLogs, manager: StorageManager, caplog: pytest.LogCaptureFixture
) -> None:
    seed_secret(manager, "demo", "k", SECRET)
    with caplog.at_level(logging.INFO):
        logs.write_sync("demo", "info", f"leak {SECRET}")
    assert SECRET not in caplog.text
    assert "***" in caplog.text
