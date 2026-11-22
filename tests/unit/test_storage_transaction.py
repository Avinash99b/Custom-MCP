"""Candidate storage transactions: isolation, commit, discard, TTL and redaction.

These tests prove the replacement-safety property from the Phase 2 lifecycle
review: a candidate plugin constructor can read live data/secrets and stage new
values, but nothing reaches live Redis until an explicit commit. Failed,
cancelled or discarded candidates — including their late worker threads — can
never mutate live credentials.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from custom_mcp.storage.manager import (
    DATA_NAMESPACE,
    SECRET_NAMESPACE,
    CommitOutcomeUnknown,
    InvalidTtlError,
    StorageError,
    StorageManager,
    TransactionStateError,
    validate_representable_ttl,
)
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis

SLUG = "demo"


@pytest.fixture
def manager(redis_sync: SyncRedis, redis_async: AsyncRedis) -> StorageManager:
    return StorageManager(redis_sync, redis_async)


def live(namespace: str, key: str, slug: str = SLUG) -> str:
    return f"plugin:{slug}:{namespace}:{key}"


def read_json(redis_sync: SyncRedis, full_key: str) -> Any:
    raw = redis_sync.get(full_key)
    assert raw is not None
    return json.loads(raw)


async def _begin(manager: StorageManager) -> Any:
    return await manager.begin_transaction(
        SLUG, callback_url="https://x/mcp/demo/oauth/callback", log=lambda _l, _m: None
    )


# --- isolation and discard --------------------------------------------------


async def test_staged_writes_are_detached_then_discard_keeps_late_writes_off_live(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("counter", 1)
    manager.scoped(SLUG, secret=True).set("token", "old-token")
    await manager.set_core_secret(SLUG, "core.api_key", "live-api-key")

    txn = await _begin(manager)
    ctx = txn.context
    assert ctx.slug == SLUG
    assert ctx.storage.get("counter") == 1
    assert ctx.secrets.get("token") == "old-token"
    assert ctx.secrets.get("core.api_key") == "live-api-key"

    ctx.storage.set("counter", 999)
    ctx.secrets.set("token", "new-token")
    ctx.secrets.set("derived", {"access": "candidate"})

    # Nothing has reached live Redis while the candidate is staged.
    assert read_json(redis_sync, live(DATA_NAMESPACE, "counter")) == 1
    assert read_json(redis_sync, live(SECRET_NAMESPACE, "token")) == "old-token"
    assert redis_sync.get(live(SECRET_NAMESPACE, "derived")) is None

    txn.discard()

    # Late worker writes after discard remain detached, never live.
    ctx.storage.set("late", 5)
    ctx.secrets.set("token", "late-token")
    assert redis_sync.get(live(DATA_NAMESPACE, "late")) is None
    assert read_json(redis_sync, live(SECRET_NAMESPACE, "token")) == "old-token"
    # The detached view is still readable so a late constructor can finish.
    assert ctx.storage.get("counter") == 999


async def test_begin_transaction_rejects_invalid_slug(manager: StorageManager) -> None:
    with pytest.raises(StorageError):
        await manager.begin_transaction(
            "Bad Slug", callback_url="x", log=lambda _l, _m: None
        )


async def test_begin_and_commit_on_empty_state(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    txn = await _begin(manager)
    assert txn.context.storage.get("missing", "absent") == "absent"
    assert txn.context.secrets.list_keys() == []
    await txn.commit()  # no-op delta on empty state is valid

    txn2 = await _begin(manager)
    txn2.context.storage.set("first", 1)
    await txn2.commit()
    assert read_json(redis_sync, live(DATA_NAMESPACE, "first")) == 1


# --- commit -----------------------------------------------------------------


async def test_commit_applies_delta_and_rebinds_to_live(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("keep", {"v": 1})
    manager.scoped(SLUG).set("remove", {"v": 2})
    manager.scoped(SLUG, secret=True).set("token", "old-token")
    await manager.set_core_secret(SLUG, "core.api_key", "api")

    txn = await _begin(manager)
    ctx = txn.context
    ctx.storage.set("keep", {"v": 10})
    ctx.storage.delete("remove")
    ctx.storage.set("fresh", [1, 2, 3])
    ctx.secrets.set("token", "new-token")

    await txn.commit()
    assert txn.state == "committed"

    assert read_json(redis_sync, live(DATA_NAMESPACE, "keep")) == {"v": 10}
    assert redis_sync.get(live(DATA_NAMESPACE, "remove")) is None
    assert read_json(redis_sync, live(DATA_NAMESPACE, "fresh")) == [1, 2, 3]
    assert read_json(redis_sync, live(SECRET_NAMESPACE, "token")) == "new-token"
    assert read_json(redis_sync, live(SECRET_NAMESPACE, "core.api_key")) == "api"

    # After promotion the same frozen context writes live storage.
    ctx.storage.set("after", {"live": True})
    assert read_json(redis_sync, live(DATA_NAMESPACE, "after")) == {"live": True}
    ctx.secrets.set("token2", "live-secret")
    assert read_json(redis_sync, live(SECRET_NAMESPACE, "token2")) == "live-secret"

    with pytest.raises(TransactionStateError):
        await txn.commit()


async def test_commit_after_discard_raises_and_discard_is_idempotent(
    manager: StorageManager,
) -> None:
    txn = await _begin(manager)
    txn.discard()
    txn.discard()
    assert txn.state == "discarded"
    with pytest.raises(TransactionStateError):
        await txn.commit()


async def test_commit_does_not_touch_other_keys(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    redis_sync.set("core:other:key", "keep")
    manager.scoped("other").set("k", "keep")
    txn = await _begin(manager)
    txn.context.storage.set("mine", "v")
    await txn.commit()
    assert redis_sync.get("core:other:key") == "keep"
    assert read_json(redis_sync, "plugin:other:data:k") == "keep"


# --- listing / TTL ----------------------------------------------------------


async def test_staging_list_keys_merges_base_and_staged(manager: StorageManager) -> None:
    manager.scoped(SLUG).set("a", 1)
    manager.scoped(SLUG).set("b", 2)
    txn = await _begin(manager)
    ctx = txn.context
    ctx.storage.set("c", 3)
    ctx.storage.delete("a")
    assert ctx.storage.list_keys() == ["b", "c"]


async def test_commit_preserves_staged_ttl(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    txn = await _begin(manager)
    txn.context.storage.set("temp", {"k": 1}, ttl=60)
    await txn.commit()
    pttl = redis_sync.pttl(live(DATA_NAMESPACE, "temp"))
    assert 0 < pttl <= 60_000


async def test_commit_leaves_unchanged_key_ttl_intact(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("kept", "v", ttl=120)
    txn = await _begin(manager)
    txn.context.storage.set("added", "x")
    await txn.commit()
    assert read_json(redis_sync, live(DATA_NAMESPACE, "kept")) == "v"
    assert 0 < redis_sync.pttl(live(DATA_NAMESPACE, "kept")) <= 120_000


async def test_staged_set_clears_existing_ttl(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("k", "v", ttl=100)
    txn = await _begin(manager)
    txn.context.storage.set("k", "w")
    await txn.commit()
    assert redis_sync.pttl(live(DATA_NAMESPACE, "k")) == -1


async def test_candidate_reads_respect_snapshot_ttl(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("expiring", {"v": 1}, ttl=5)
    fake_now = [1000.0]
    manager._clock = lambda: fake_now[0]
    txn = await _begin(manager)
    assert txn.context.storage.get("expiring") == {"v": 1}
    fake_now[0] += 6
    assert txn.context.storage.get("expiring", "gone") == "gone"


async def test_commit_removes_staged_overwrite_that_already_expired(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("k", "old")
    fake_now = [1000.0]
    manager._clock = lambda: fake_now[0]
    txn = await _begin(manager)
    txn.context.storage.set("k", "new", ttl=10)
    fake_now[0] += 11
    await txn.commit()
    assert redis_sync.get(live(DATA_NAMESPACE, "k")) is None


# --- reserved credentials ---------------------------------------------------


async def test_candidate_reserved_read_but_not_write(manager: StorageManager) -> None:
    await manager.set_core_secret(SLUG, "core.api_key", "api-value")
    txn = await _begin(manager)
    assert txn.context.secrets.get("core.api_key") == "api-value"
    with pytest.raises(PermissionError):
        txn.context.secrets.set("core.api_key", "x")
    with pytest.raises(PermissionError):
        txn.context.secrets.delete("core.api_key")


# --- log redaction ----------------------------------------------------------


async def test_transaction_log_redacts_live_and_staged_secrets(
    manager: StorageManager,
) -> None:
    await manager.set_core_secret(SLUG, "core.api_key", "live-secret-value")
    captured: list[str] = []
    txn = await manager.begin_transaction(
        SLUG, callback_url="x", log=lambda _l, message: captured.append(message)
    )
    ctx = txn.context
    ctx.log("info", "using live-secret-value now")
    ctx.secrets.set("token", "candidate-secret-value")
    ctx.log("info", "using candidate-secret-value now")

    assert "live-secret-value" not in captured[0]
    assert "candidate-secret-value" not in captured[1]
    assert "***" in captured[0]
    assert "***" in captured[1]


# --- concurrency / failure --------------------------------------------------


async def test_commit_is_serialized_with_staged_mutation(manager: StorageManager) -> None:
    txn = await _begin(manager)
    entered = threading.Event()
    finished = threading.Event()

    def worker() -> None:
        entered.set()
        txn.context.storage.set("blocked", "v")
        finished.set()

    with txn._lock:
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(timeout=5)
        # The worker cannot mutate staging while the transaction lock is held.
        assert not finished.wait(timeout=0.2)
    thread.join(timeout=5)
    assert finished.is_set()
    assert txn.context.storage.get("blocked") == "v"


async def test_transport_failure_without_receipt_is_uncertain(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_sync, "eval", boom)
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    # A transport failure with no receipt cannot prove a safe pre-write state.
    with pytest.raises(CommitOutcomeUnknown):
        await txn.commit()

    assert txn.state == "uncertain"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "old"
    with pytest.raises(CommitOutcomeUnknown):
        txn.discard()


# --- commit/discard linearization and cancellation --------------------------


def _pause_apply_delta(
    manager: StorageManager,
    monkeypatch: pytest.MonkeyPatch,
    entered: threading.Event,
    release: threading.Event,
) -> None:
    """Block the commit worker inside ``apply_delta``, then run the real write."""
    original = manager.apply_delta

    def paused(
        commit_id: str,
        sets: dict[str, tuple[str, int | None]],
        deletes: set[str],
    ) -> str:
        entered.set()
        release.wait(5.0)
        return original(commit_id, sets, deletes)

    monkeypatch.setattr(manager, "apply_delta", paused)


async def test_commit_cancel_settles_worker_before_raising(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    entered = threading.Event()
    release = threading.Event()
    _pause_apply_delta(manager, monkeypatch, entered, release)

    commit_task = asyncio.ensure_future(txn.commit())
    assert await asyncio.to_thread(entered.wait, 5.0)
    commit_task.cancel()
    await asyncio.sleep(0)
    commit_task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await commit_task
    # The worker settled before the cancellation propagated; no live write can
    # happen after this return, and the state reflects the settled outcome.
    assert txn.state == "committed"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "new"


async def test_discard_during_inflight_commit_raises(
    manager: StorageManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    entered = threading.Event()
    release = threading.Event()
    _pause_apply_delta(manager, monkeypatch, entered, release)

    commit_task = asyncio.ensure_future(txn.commit())
    assert await asyncio.to_thread(entered.wait, 5.0)
    assert txn.state == "committing"
    with pytest.raises(TransactionStateError):
        txn.discard()
    assert txn.state == "committing"
    release.set()

    await commit_task
    assert txn.state == "committed"
    txn.discard()  # a settled commit cannot be changed
    assert txn.state == "committed"


async def test_staged_write_blocked_by_commit_lands_on_live(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = await _begin(manager)
    entered = threading.Event()
    release = threading.Event()
    _pause_apply_delta(manager, monkeypatch, entered, release)
    commit_task = asyncio.ensure_future(txn.commit())
    assert await asyncio.to_thread(entered.wait, 5.0)

    done = threading.Event()

    def writer() -> None:
        txn.context.storage.set("late", {"v": 1})
        done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        # The write cannot complete while the commit holds the transaction lock.
        assert not done.wait(0.2)
    finally:
        release.set()
    await commit_task
    thread.join(timeout=5)
    assert done.is_set()
    # It resolved the rebound backend, so it landed on live storage.
    assert read_json(redis_sync, live(DATA_NAMESPACE, "late")) == {"v": 1}
    assert txn.context.storage.get("late") == {"v": 1}


async def test_redact_after_commit_uses_live_secrets(manager: StorageManager) -> None:
    txn = await _begin(manager)
    txn.context.secrets.set("token", "candidate-secret-value")
    await txn.commit()
    assert txn.redact("x candidate-secret-value y") == "x *** y"


# --- atomic commit, receipt reconciliation and absolute TTL -----------------


async def test_unrepresentable_ttl_rejected_before_any_write(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    assert validate_representable_ttl(60) == 60
    huge = 2**62
    with pytest.raises(InvalidTtlError):
        manager.scoped(SLUG).set("k", "v", ttl=huge)
    assert redis_sync.get(live(DATA_NAMESPACE, "k")) is None

    txn = await _begin(manager)
    with pytest.raises(InvalidTtlError):
        txn.context.storage.set("k", "v", ttl=huge)
    with pytest.raises(InvalidTtlError):
        await manager.set_core_secret(SLUG, "core.x", "v", ttl=huge)
    assert redis_sync.get(live(DATA_NAMESPACE, "k")) is None


async def test_server_rejected_commit_leaves_live_unchanged(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    monkeypatch.setattr(redis_sync, "eval", lambda *_a, **_k: "prewrite_failed")
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    with pytest.raises(StorageError):
        await txn.commit()
    assert txn.state == "open"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "old"
    txn.discard()  # a trusted pre-write rejection is safe to discard
    assert txn.state == "discarded"


async def test_lost_reply_after_applied_script_is_reconciled(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    original_eval = redis_sync.eval

    def apply_then_raise(*args: Any, **kwargs: Any) -> Any:
        original_eval(*args, **kwargs)
        raise RuntimeError("connection lost")

    monkeypatch.setattr(redis_sync, "eval", apply_then_raise)
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    await txn.commit()  # reconciled against the receipt, not blindly reopened
    assert txn.state == "committed"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "new"


async def test_unreconcilable_outcome_is_uncertain(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")

    def boom_eval(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("redis down")

    def boom_get(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_sync, "eval", boom_eval)
    monkeypatch.setattr(redis_sync, "get", boom_get)
    with pytest.raises(CommitOutcomeUnknown):
        await txn.commit()
    assert txn.state == "uncertain"
    with pytest.raises(CommitOutcomeUnknown):
        txn.discard()


async def test_commit_does_not_resurrect_expired_staged_ttl(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    txn = await _begin(manager)
    txn.context.storage.set("k", "new", ttl=1)
    entered = threading.Event()
    release = threading.Event()
    _pause_apply_delta(manager, monkeypatch, entered, release)

    commit_task = asyncio.ensure_future(txn.commit())
    assert await asyncio.to_thread(entered.wait, 5.0)
    await asyncio.sleep(1.2)  # the absolute deadline passes while paused
    release.set()
    await commit_task
    assert redis_sync.get(live(DATA_NAMESPACE, "k")) is None


async def test_commit_receipt_is_transient_and_idempotent(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    txn = await _begin(manager)
    txn.context.storage.set("k", 1)
    await txn.commit()
    key = f"core:storage_commit:{txn.commit_id}"
    assert redis_sync.get(key) == f"{txn.commit_id}:committed"
    assert manager.read_commit_outcome(txn.commit_id) == "committed"
    assert 0 < redis_sync.ttl(key) <= 600
    manager.apply_delta(txn.commit_id, {}, set())  # replay is idempotent


# --- combined redaction -----------------------------------------------------


async def test_redact_combines_initial_staged_and_live(
    manager: StorageManager,
) -> None:
    manager.scoped(SLUG, secret=True).set("token", "initial-secret-value")
    txn = await _begin(manager)
    txn.context.secrets.set("token", "staged-secret-value")

    # The staged overwrite hides the initial value from the staging view, but the
    # initial snapshot is still redacted.
    assert txn.redact("a initial-secret-value b") == "a *** b"
    assert txn.redact("a staged-secret-value b") == "a *** b"

    # A live token rotated after the snapshot is redacted too.
    manager.scoped(SLUG, secret=True).set("rotated", "rotated-secret-value")
    assert txn.redact("rotated-secret-value") == "***"

    # Nested JSON string values count.
    txn.context.secrets.set("creds", {"access": "nested-secret-value"})
    assert txn.redact("nested-secret-value") == "***"

    await txn.commit()
    assert txn.redact("initial-secret-value") == "***"
    assert txn.redact("staged-secret-value") == "***"
    assert txn.redact("rotated-secret-value") == "***"


# --- receipt semantics under command-level faults ---------------------------

_COMMITTED_RECEIPT = "redis.call('SET', KEYS[1], prefix .. 'committed', 'EX', 600)"


async def test_receipt_write_failure_after_apply_rolls_back(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    original_eval = redis_sync.eval

    def wrapper(script: str, numkeys: int, *keys_and_args: str) -> Any:
        faulted = script.replace(_COMMITTED_RECEIPT, "error('inject-receipt-failure')")
        return original_eval(faulted, numkeys, *keys_and_args)

    monkeypatch.setattr(redis_sync, "eval", wrapper)
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    with pytest.raises(StorageError):
        await txn.commit()
    # Compensation restored the original and recorded the rollback.
    assert txn.state == "open"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "old"
    assert manager.read_commit_outcome(txn.commit_id) == "rolled_back"


async def test_apply_and_compensation_failure_is_uncertain(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")
    fault = (
        "redis.call('SET', ARGV[3], ARGV[4])\n"
        "redis.call('SET', KEYS[1], ARGV[1] .. ':pending', 'EX', 600)\n"
        "error('inject-apply-and-restore-failure')\n"
    )
    original_eval = redis_sync.eval

    def wrapper(script: str, numkeys: int, *keys_and_args: str) -> Any:
        return original_eval(fault, numkeys, *keys_and_args)

    monkeypatch.setattr(redis_sync, "eval", wrapper)
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    with pytest.raises(CommitOutcomeUnknown):
        await txn.commit()
    # The candidate value may be live, so the outcome is uncertain, not "open".
    assert txn.state == "uncertain"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "new"
    assert manager.read_commit_outcome(txn.commit_id) == "pending"
    with pytest.raises(CommitOutcomeUnknown):
        txn.discard()


async def test_rolled_back_receipt_on_lost_reply_marks_open(
    manager: StorageManager, redis_sync: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager.scoped(SLUG).set("k", "old")

    def wrapper(script: str, numkeys: int, *keys_and_args: str) -> Any:
        redis_sync.set(
            keys_and_args[0], f"{keys_and_args[1]}:rolled_back", ex=600
        )
        raise RuntimeError("connection lost")

    monkeypatch.setattr(redis_sync, "eval", wrapper)
    txn = await _begin(manager)
    txn.context.storage.set("k", "new")
    with pytest.raises(StorageError):
        await txn.commit()
    assert txn.state == "open"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "old"


async def test_pending_receipt_replay_is_uncertain_without_reapply(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    txn = await _begin(manager)
    redis_sync.set(
        f"core:storage_commit:{txn.commit_id}", f"{txn.commit_id}:pending", ex=600
    )
    txn.context.storage.set("k", "new")
    with pytest.raises(CommitOutcomeUnknown):
        await txn.commit()
    assert txn.state == "uncertain"
    # A pending receipt must not be replayed blindly.
    assert redis_sync.get(live(DATA_NAMESPACE, "k")) is None


async def test_conflicting_receipt_is_rejected(
    manager: StorageManager, redis_sync: SyncRedis
) -> None:
    manager.scoped(SLUG).set("k", "old")
    txn = await _begin(manager)
    redis_sync.set(
        f"core:storage_commit:{txn.commit_id}", "other-id:committed", ex=600
    )
    txn.context.storage.set("k", "new")
    with pytest.raises(StorageError):
        await txn.commit()
    assert txn.state == "open"
    assert read_json(redis_sync, live(DATA_NAMESPACE, "k")) == "old"
