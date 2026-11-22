"""Scoped storage, persistence and log integration against a real Redis.

Every test here runs against the dedicated server from ``conftest.py`` (private
unix socket, owned temp dir, AOF enabled). No external Redis is touched. The
server has no password and only dummy values are ever stored, so no real
credentials appear in code, args or logs.

Managed tests are ``async`` only so the async Redis client fixture can be closed
deterministically; the storage calls themselves are synchronous SDK calls.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import Any

import pytest

from custom_mcp.plugins.logs import LOG_CAP, PluginLogs
from custom_mcp.storage.manager import (
    RECEIPT_TTL_SECONDS,
    StorageManager,
    TransactionStateError,
    validate_slug,
)
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.real_redis.conftest import RealRedisServer

pytestmark = pytest.mark.skipif(
    shutil.which("redis-server") is None,
    reason="redis-server binary is not available",
)

#: Exact dataset slug used by this suite (validated against the slug pattern).
SLUG = "rredisexample"
OTHER_SLUG = "rr-other"

#: Deterministic dummy dataset; values are never real credentials.
DATA_DATASET: dict[str, Any] = {
    "greeting": {"text": "hello", "count": 1},
    "flags": [True, False, None],
    "note": "dummy-note-value",
}
SECRET_DATASET: dict[str, Any] = {
    "token": "dummy-token-value",
    "core.api_key": "dummy-api-key-value",
}


def _data_key(key: str) -> str:
    return f"plugin:{SLUG}:data:{key}"


def _seed_dataset(manager: StorageManager) -> None:
    data = manager.scoped(SLUG)
    for key, value in DATA_DATASET.items():
        data.set(key, value)
    secrets = manager.scoped(SLUG, secret=True, allow_reserved=True)
    for key, value in SECRET_DATASET.items():
        secrets.set(key, value)


def _assert_dataset(manager: StorageManager) -> None:
    data = manager.scoped(SLUG)
    for key, value in DATA_DATASET.items():
        assert data.get(key) == value
    secrets = manager.scoped(SLUG, secret=True)
    for key, value in SECRET_DATASET.items():
        assert secrets.get(key) == value


def test_slug_is_exactly_expected() -> None:
    assert SLUG == "rredisexample"
    assert validate_slug(SLUG) == SLUG


def test_server_version_supports_required_features(redis_server: RealRedisServer) -> None:
    parts = redis_server.version().split(".")
    assert (int(parts[0]), int(parts[1])) >= (6, 2)


def test_lua_eval_and_pxat_supported(redis_sync: SyncRedis) -> None:
    """A real Redis >= 6.2 exposes generic EVAL and absolute PXAT deadlines."""
    assert redis_sync.eval("return 1", 0) == 1
    key = _data_key("pxat-probe")
    deadline_ms = int(time.time() * 1000) + 60_000
    redis_sync.eval(
        "redis.call('SET', KEYS[1], ARGV[1], 'PXAT', ARGV[2]) return 1",
        1,
        key,
        "value",
        str(deadline_ms),
    )
    assert 0 < redis_sync.pttl(key) <= 60_000


async def test_scoped_round_trip(manager: StorageManager) -> None:
    storage = manager.scoped(SLUG)
    storage.set("greeting", DATA_DATASET["greeting"])
    storage.set("note", DATA_DATASET["note"])
    assert storage.get("greeting") == DATA_DATASET["greeting"]
    assert storage.list_keys() == ["greeting", "note"]
    assert storage.list_keys("gre") == ["greeting"]
    assert storage.delete("note") is True
    assert storage.delete("note") is False
    assert storage.get("note", "gone") == "gone"


async def test_keys_are_scoped_to_the_exact_slug(manager: StorageManager) -> None:
    manager.scoped(SLUG).set("k", "mine")
    manager.scoped(OTHER_SLUG).set("k", "theirs")
    assert manager.scoped(SLUG).get("k") == "mine"
    assert manager.scoped(OTHER_SLUG).get("k") == "theirs"
    # A longer slug must not bleed into the exact slug via prefix matching.
    manager.scoped(SLUG + "-long").set("k", "long")
    assert manager.scoped(SLUG).get("k") == "mine"
    assert manager.scoped(SLUG).list_keys() == ["k"]


async def test_core_secret_round_trip_and_reserved_guard(manager: StorageManager) -> None:
    await manager.set_core_secret(SLUG, "core.api_key", "dummy-api-key-value")
    assert await manager.get_core_secret(SLUG, "core.api_key") == "dummy-api-key-value"
    assert await manager.secret_names(SLUG) == ["core.api_key"]

    secrets = manager.scoped(SLUG, secret=True)
    assert secrets.get("core.api_key") == "dummy-api-key-value"
    with pytest.raises(PermissionError):
        secrets.set("core.api_key", "blocked")
    with pytest.raises(PermissionError):
        secrets.delete("core.api_key")

    assert await manager.delete_core_secret(SLUG, "core.api_key") is True
    assert await manager.get_core_secret(SLUG, "core.api_key") is None


async def test_ttl_is_bounded_without_sleeping(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("session", "temporary", ttl=60)
    pttl = redis_sync.pttl(_data_key("session"))
    assert 0 < pttl <= 60_000


async def test_context_scopes_data_and_secrets(manager: StorageManager) -> None:
    context = manager.context(
        SLUG,
        callback_url=f"https://mcp.example.com/mcp/{SLUG}/",
        log=lambda _level, _message: None,
    )
    assert context.slug == SLUG
    assert context.callback_url == f"https://mcp.example.com/mcp/{SLUG}/"
    context.storage.set("k", "data-value")
    assert context.storage.get("k") == "data-value"
    assert context.secrets.get("k", "absent") == "absent"


async def test_aof_restart_persists_data_secrets_and_ttl(
    redis_server: RealRedisServer,
) -> None:
    sync = redis_server.sync_client()
    async_client = redis_server.async_client()
    try:
        manager = StorageManager(sync, async_client)
        _seed_dataset(manager)
        manager.scoped(SLUG).set("transient", "bounded", ttl=300)
    finally:
        sync.close()
        await async_client.aclose()

    redis_server.restart()

    verify_sync = redis_server.sync_client()
    verify_async = redis_server.async_client()
    try:
        manager = StorageManager(verify_sync, verify_async)
        _assert_dataset(manager)
        pttl = verify_sync.pttl(_data_key("transient"))
        assert 0 < pttl <= 300_000
    finally:
        verify_sync.close()
        await verify_async.aclose()


async def test_plugin_logs_callback_redacts_current_secret(
    manager: StorageManager, redis_sync: SyncRedis, redis_async: AsyncRedis
) -> None:
    secret = "dummy-secret-token"
    manager.scoped(SLUG, secret=True).set("token", secret)
    logs = PluginLogs(redis_sync, redis_async)
    callback = logs.callback(SLUG)
    callback("info", f"connecting with {secret} for {SLUG}")
    entries = await logs.recent(SLUG)
    assert entries
    message = entries[0]["message"]
    assert secret not in message
    assert "***" in message


async def test_plugin_logs_are_capped_at_200(
    redis_sync: SyncRedis, redis_async: AsyncRedis
) -> None:
    logs = PluginLogs(redis_sync, redis_async)
    for index in range(LOG_CAP + 5):
        logs.write_sync(SLUG, "info", f"entry-{index}")
    entries = await logs.recent(SLUG, limit=LOG_CAP + 25)
    assert len(entries) == LOG_CAP
    assert entries[0]["message"] == f"entry-{LOG_CAP + 4}"


# ---------------------------------------------------------------------------
# Candidate storage transactions (stable public API) on the real dedicated Redis
# ---------------------------------------------------------------------------


def _begin_args() -> dict[str, Any]:
    return {
        "callback_url": f"https://mcp.example.com/mcp/{SLUG}/oauth/callback",
        "log": lambda _level, _message: None,
    }


def _server_now_ms(redis_sync: SyncRedis) -> int:
    """Current server wall-clock as Unix milliseconds (authoritative TIME)."""
    now = redis_sync.eval("return redis.call('TIME')", 0)
    assert isinstance(now, list) and len(now) == 2
    return int(now[0]) * 1000 + int(now[1]) // 1000


async def test_candidate_reads_live_then_discard_keeps_original(
    manager: StorageManager,
) -> None:
    manager.scoped(SLUG).set("counter", 1)
    manager.scoped(SLUG, secret=True).set("token", "old-token")

    txn = await manager.begin_transaction(SLUG, **_begin_args())
    assert txn.state == "open"
    assert txn.commit_id

    # A fresh candidate reads live values; nothing is staged yet.
    ctx = txn.context
    assert ctx.slug == SLUG
    assert ctx.storage.get("counter") == 1
    assert ctx.secrets.get("token") == "old-token"
    assert ctx.storage.get("derived", "absent") == "absent"

    # Staged data/secret writes never reach live Redis while the txn is open.
    ctx.storage.set("counter", 999)
    ctx.storage.set("derived", "candidate")
    ctx.secrets.set("token", "new-token")
    assert manager.scoped(SLUG).get("counter") == 1
    assert manager.scoped(SLUG).get("derived", "absent") == "absent"
    assert manager.scoped(SLUG, secret=True).get("token") == "old-token"

    txn.discard()
    assert txn.state == "discarded"

    # Late writes after discard stay in the detached staging backend.
    ctx.storage.set("late", "staged-only")
    assert manager.scoped(SLUG).get("counter") == 1
    assert manager.scoped(SLUG).get("derived", "absent") == "absent"
    assert manager.scoped(SLUG).get("late", "absent") == "absent"
    assert manager.scoped(SLUG, secret=True).get("token") == "old-token"

    # A discarded transaction cannot be committed.
    with pytest.raises(TransactionStateError):
        await txn.commit()
    assert txn.state == "discarded"


async def test_commit_applies_data_and_secret_delta_then_proxy_writes_live(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("counter", 1)
    manager.scoped(SLUG, secret=True).set("token", "old-token")

    txn = await manager.begin_transaction(SLUG, **_begin_args())
    ctx = txn.context
    ctx.storage.set("counter", 999)
    ctx.storage.set("derived", {"n": 1})
    ctx.secrets.set("token", "new-token")

    await txn.commit()
    assert txn.state == "committed"

    assert manager.scoped(SLUG).get("counter") == 999
    assert manager.scoped(SLUG).get("derived") == {"n": 1}
    assert manager.scoped(SLUG, secret=True).get("token") == "new-token"

    # The context proxy is rebound to live storage after a successful commit.
    ctx.storage.set("after-commit", "live-write")
    assert manager.scoped(SLUG).get("after-commit") == "live-write"

    # A transient commit receipt records the idempotent outcome.
    assert manager.read_commit_outcome(txn.commit_id) == "committed"
    receipt = manager.read_commit_receipt(txn.commit_id)
    assert receipt is not None and receipt.endswith(":committed")
    pttl = redis_sync.pttl(manager.receipt_key(txn.commit_id))
    assert 0 < pttl <= RECEIPT_TTL_SECONDS * 1000


async def test_commit_preserves_untouched_ttl_and_bounds_staged_ttl(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("keeper", "keep", ttl=120)
    prior = redis_sync.pttl(_data_key("keeper"))
    assert prior > 0

    txn = await manager.begin_transaction(SLUG, **_begin_args())
    txn.context.storage.set("fresh", "value", ttl=60)
    await txn.commit()

    after = redis_sync.pttl(_data_key("keeper"))
    assert 0 < after <= prior
    assert 0 < redis_sync.pttl(_data_key("fresh")) <= 60_000


async def test_staged_ttl_expiry_is_not_revived_on_commit(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    txn = await manager.begin_transaction(SLUG, **_begin_args())
    txn.context.storage.set("short", "value", ttl=1)
    staged_at_ms = _server_now_ms(redis_sync)

    # Bounded wait until the server clock passes the staged 1s deadline.
    bounded = time.monotonic() + 5.0
    while _server_now_ms(redis_sync) < staged_at_ms + 1100:
        if time.monotonic() > bounded:
            pytest.fail("server clock did not advance past the staged TTL")
        await asyncio.sleep(0.02)

    await txn.commit()
    assert txn.state == "committed"
    # The expired staged key must be deleted, never resurrected.
    assert redis_sync.pttl(_data_key("short")) <= 0
    assert manager.scoped(SLUG).get("short", "absent") == "absent"
