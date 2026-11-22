"""Scoped plugin storage, core reserved-secret access and candidate transactions.

Plugins are handed only scoped ``Storage`` views; they are never passed a Redis
client through the public API (the private client reference is an owner-trusted
implementation detail, not part of the contract). Keys beginning ``core.`` are
readable but not writable by plugin code; the core writes them through the
explicit :meth:`StorageManager.set_core_secret` interface.

Values are strict JSON (no NaN/Infinity), serialized to at most 1 MiB. Key
listing uses ``SCAN`` (never ``KEYS``) and re-filters results to the expected
namespace prefix.

Candidate transactions let a replacing plugin's constructor run against a
detached copy-on-write snapshot: its writes never touch live Redis, so a failed,
cancelled or timed-out candidate cannot mutate live credentials even when late
worker threads keep running. A successful :meth:`StorageTransaction.commit`
applies only the changed-key delta through one server-side Lua script that
prevalidates every operation, writes a transient commit receipt and applies
``SET``/``DEL`` atomically. A lost reply is reconciled against the receipt, and
an unqueryable outcome is reported as ``uncertain`` rather than assumed
unchanged.

This module is owned by the Phase 2 storage/logs unit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event, Lock, RLock
from typing import Any, Protocol

from custom_mcp.plugins.manifest import SLUG_RE
from custom_mcp.plugins.seal import retired_writes_allowed
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from mcp_plugin_sdk import PluginContext

KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
PREFIX_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{0,128}")
MAX_VALUE_BYTES = 1024 * 1024
RESERVED_PREFIX = "core."
DATA_NAMESPACE = "data"
SECRET_NAMESPACE = "secret"
_NAMESPACES = frozenset({DATA_NAMESPACE, SECRET_NAMESPACE})
_REDACT_MIN_LENGTH = 6
_REDACTION = "***"

#: Redis integer fields are signed 64-bit; an absolute expiry must fit below this.
MAX_REDIS_DEADLINE_MS = 2**63 - 1
#: Transient commit receipt key prefix and lifetime (excluded from backups).
RECEIPT_PREFIX = "core:storage_commit:"
RECEIPT_TTL_SECONDS = 600

# Redis >= 6.2 Lua: idempotent receipt, eager deadline preflight, a durable
# pending marker before any write, and an atomic apply + receipt decision with
# per-target compensation. The returned outcome is authoritative: an absent
# receipt on a transport failure is treated as uncertain, never as "open".
# ARGV layout:
#   ARGV[1] = commit id
#   ARGV[2] = number of SET ops, then (key, value, expiry_ms|-1) triples
#   next    = number of DEL keys, then the keys
# Expiry is an absolute Unix-millisecond deadline compared against server TIME,
# so a key whose deadline already passed is deleted rather than resurrected.
_COMMIT_SCRIPT = """
local prefix = ARGV[1] .. ':'
local receipt = redis.call('GET', KEYS[1])
if receipt then
  if string.sub(receipt, 1, #prefix) == prefix then
    local status = string.sub(receipt, #prefix + 1)
    if status == 'committed' then
      return 'committed'
    end
    if status == 'rolled_back' then
      return 'rolled_back'
    end
    return 'pending'
  end
  return 'conflict'
end

local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)

local i = 2
local nsets = tonumber(ARGV[i]); i = i + 1
local sets = {}
for n = 1, nsets do
  sets[n] = { k = ARGV[i], v = ARGV[i + 1], x = tonumber(ARGV[i + 2]) }
  i = i + 3
end
local ndel = tonumber(ARGV[i]); i = i + 1
local deletes = {}
for n = 1, ndel do
  deletes[n] = ARGV[i]; i = i + 1
end

for n = 1, nsets do
  local x = sets[n].x
  if x ~= nil and x ~= -1 and x <= 0 then
    return 'prewrite_failed'
  end
end

local keys = {}
for n = 1, nsets do keys[#keys + 1] = sets[n].k end
for n = 1, ndel do keys[#keys + 1] = deletes[n] end

if not pcall(function()
  redis.call('SET', KEYS[1], prefix .. 'pending', 'EX', 600)
end) then
  return 'prewrite_failed'
end

local snapshot = {}
if not pcall(function()
  for n = 1, #keys do
    local key = keys[n]
    if snapshot[key] == nil then
      snapshot[key] = { v = redis.call('GET', key), p = redis.call('PTTL', key) }
    end
  end
end) then
  pcall(function()
    redis.call('SET', KEYS[1], prefix .. 'rolled_back', 'EX', 600)
  end)
  return 'prewrite_failed'
end

local applied = pcall(function()
  for n = 1, nsets do
    local op = sets[n]
    if op.x ~= nil and op.x ~= -1 and op.x <= now_ms then
      redis.call('DEL', op.k)
    elseif op.x ~= nil and op.x ~= -1 then
      redis.call('SET', op.k, op.v, 'PXAT', op.x)
    else
      redis.call('SET', op.k, op.v)
    end
  end
  for n = 1, ndel do
    redis.call('DEL', deletes[n])
  end
  redis.call('SET', KEYS[1], prefix .. 'committed', 'EX', 600)
end)

if applied then
  return 'committed'
end

local all_restored = true
for key, s in pairs(snapshot) do
  if not pcall(function()
    if s.v == false then
      redis.call('DEL', key)
    elseif s.p ~= nil and s.p >= 0 then
      local deadline = now_ms + s.p
      if deadline <= now_ms then
        redis.call('DEL', key)
      else
        redis.call('SET', key, s.v, 'PXAT', deadline)
      end
    else
      redis.call('SET', key, s.v)
    end
  end) then
    all_restored = false
  end
end

if not all_restored then
  return 'unknown'
end

pcall(function()
  redis.call('SET', KEYS[1], prefix .. 'rolled_back', 'EX', 600)
end)
return 'rolled_back'
"""

#: Every outcome the commit script may return to the caller.
_COMMIT_OUTCOMES = frozenset(
    {"committed", "rolled_back", "prewrite_failed", "conflict", "pending", "unknown"}
)


class StorageError(ValueError):
    """Base class for scoped-storage validation errors."""


class InvalidSlugError(StorageError):
    """The slug is not a valid plugin identifier."""


class InvalidKeyError(StorageError):
    """The storage key does not match the permitted pattern."""


class InvalidPrefixError(StorageError):
    """The key prefix is not permitted for listing."""


class InvalidValueError(StorageError):
    """The value is not strict JSON or exceeds the size limit."""


class InvalidTtlError(StorageError):
    """The TTL is not a positive, Redis-representable integer."""


class StorageDecodeError(StorageError):
    """A stored value is not valid JSON."""


class TransactionStateError(StorageError):
    """A storage transaction was committed or discarded more than once."""


class CommitOutcomeUnknown(StorageError):
    """A commit's server-side outcome could not be reconciled with the receipt."""


def validate_slug(slug: str) -> str:
    """Return ``slug`` if it is a valid plugin slug, else raise."""
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
        raise InvalidSlugError(f"invalid plugin slug: {slug!r}")
    return slug


def validate_key(key: str) -> str:
    """Return ``key`` if it matches the storage key pattern, else raise."""
    if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
        raise InvalidKeyError(f"invalid storage key: {key!r}")
    return key


def validate_prefix(prefix: str) -> str:
    """Return ``prefix`` if permitted for listing (no glob metacharacters)."""
    if not isinstance(prefix, str) or PREFIX_PATTERN.fullmatch(prefix) is None:
        raise InvalidPrefixError(f"invalid key prefix: {prefix!r}")
    return prefix


def validate_ttl(ttl: int | None) -> int | None:
    """Return ``ttl`` if ``None`` or a positive integer (bools excluded)."""
    if ttl is None:
        return None
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise InvalidTtlError(f"ttl must be a positive integer, got {ttl!r}")
    return ttl


def validate_representable_ttl(ttl: int, now_ms: int | None = None) -> int:
    """Reject a TTL whose absolute expiry cannot fit a Redis 64-bit deadline.

    The bound is derived from the current wall clock, so ordinary positive
    integer TTLs always pass and only genuinely unrepresentable values fail.
    """
    checked = validate_ttl(ttl)
    assert checked is not None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if checked > (MAX_REDIS_DEADLINE_MS - now_ms) // 1000:
        raise InvalidTtlError("ttl is not representable as a Redis expiry")
    return checked


def encode_value(value: Any) -> str:
    """Serialize ``value`` to strict JSON, enforcing the size limit."""
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        encoded = text.encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        # Keep the message generic: a value or key may itself be a secret.
        raise InvalidValueError("value is not JSON-serializable") from exc
    if len(encoded) > MAX_VALUE_BYTES:
        raise InvalidValueError("value exceeds the 1 MiB JSON limit")
    return text


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def decode_value(raw: str) -> Any:
    """Parse a stored JSON string strictly, raising a safe error when malformed."""
    try:
        return json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise StorageDecodeError("stored value is not valid JSON") from exc


def _decode_or_raw(raw: str) -> Any:
    try:
        return decode_value(raw)
    except StorageDecodeError:
        return raw


def _string_leaves(value: Any) -> list[str]:
    """Collect every string leaf from a decoded JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        leaves: list[str] = []
        for item in value.values():
            leaves.extend(_string_leaves(item))
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = []
        for item in value:
            leaves.extend(_string_leaves(item))
        return leaves
    return []


def _redact_text(text: str, secrets: Iterable[str]) -> str:
    """Replace every current secret (>=6 chars) in ``text`` with ``***``."""
    ordered = sorted(
        {secret for secret in secrets if len(secret) >= _REDACT_MIN_LENGTH},
        key=len,
        reverse=True,
    )
    for secret in ordered:
        if secret in text:
            text = text.replace(secret, _REDACTION)
    return text


# ---------------------------------------------------------------------------
# Backends: live Redis and detached copy-on-write staging
# ---------------------------------------------------------------------------


class _StorageBackend(Protocol):
    """Plugin-local storage operations (keys are not namespace-prefixed)."""

    def get_raw(self, key: str) -> str | None: ...

    def set_raw(self, key: str, payload: str, ttl: int | None) -> None: ...

    def delete_raw(self, key: str) -> bool: ...

    def list_raw(self, prefix: str) -> list[str]: ...

    def current_string_values(self) -> list[str]: ...


class _RedisBackend:
    """Live backend over one slug's namespace in Redis."""

    def __init__(self, redis: SyncRedis, slug: str, namespace: str) -> None:
        self._redis = redis
        self._prefix = f"plugin:{slug}:{namespace}:"

    def get_raw(self, key: str) -> str | None:
        return self._redis.get(self._prefix + key)

    def set_raw(self, key: str, payload: str, ttl: int | None) -> None:
        if ttl is not None:
            validate_representable_ttl(ttl)
        self._redis.set(self._prefix + key, payload, ex=ttl)

    def delete_raw(self, key: str) -> bool:
        return bool(self._redis.delete(self._prefix + key))

    def list_raw(self, prefix: str) -> list[str]:
        return sorted(
            {
                full[len(self._prefix) :]
                for full in self._redis.scan_iter(match=self._prefix + prefix + "*")
                if full.startswith(self._prefix)
            }
        )

    def current_string_values(self) -> list[str]:
        """Every string leaf currently stored in this namespace."""
        leaves: list[str] = []
        for full in self._redis.scan_iter(match=self._prefix + "*"):
            if not full.startswith(self._prefix):
                continue
            raw = self._redis.get(full)
            if raw is None:
                continue
            leaves.extend(_string_leaves(_decode_or_raw(raw)))
        return leaves


@dataclass(frozen=True, slots=True)
class _NamespaceSnapshot:
    """Raw values and deadlines captured at transaction start.

    Deadlines are stored both as monotonic seconds (for local reads) and as
    absolute Unix milliseconds (for the server-side ``PXAT`` commit).
    """

    values: dict[str, str]
    monotonic_deadlines: dict[str, float | None]
    unix_deadlines_ms: dict[str, int | None]


def _snapshot_string_leaves(snapshot: _NamespaceSnapshot) -> tuple[str, ...]:
    """Initial secret string leaves, retained for the whole transaction."""
    leaves: list[str] = []
    for raw in snapshot.values.values():
        leaves.extend(_string_leaves(_decode_or_raw(raw)))
    return tuple(leaves)


class _StagingBackend:
    """Detached copy-on-write backend; never writes live Redis."""

    def __init__(
        self,
        snapshot: _NamespaceSnapshot,
        lock: RLock,
        clock: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        self._lock = lock
        self._clock = clock
        self._wall_clock = wall_clock
        self._base_values = dict(snapshot.values)
        self._base_monotonic = dict(snapshot.monotonic_deadlines)
        self._base_unix_ms = dict(snapshot.unix_deadlines_ms)
        self._values: dict[str, str] = {}
        self._monotonic: dict[str, float | None] = {}
        self._unix_ms: dict[str, int | None] = {}
        self._deleted: set[str] = set()

    def get_raw(self, key: str) -> str | None:
        with self._lock:
            current = self._current(key)
            return None if current is None else current[0]

    def set_raw(self, key: str, payload: str, ttl: int | None) -> None:
        with self._lock:
            now_wall_ms = int(self._wall_clock() * 1000)
            if ttl is None:
                deadline_monotonic: float | None = None
                deadline_ms: int | None = None
            else:
                validate_representable_ttl(ttl, now_wall_ms)
                deadline_monotonic = self._clock() + ttl
                deadline_ms = now_wall_ms + ttl * 1000
            self._deleted.discard(key)
            self._values[key] = payload
            self._monotonic[key] = deadline_monotonic
            self._unix_ms[key] = deadline_ms

    def delete_raw(self, key: str) -> bool:
        with self._lock:
            existed = self._current(key) is not None
            if existed:
                self._deleted.add(key)
                self._values.pop(key, None)
                self._monotonic.pop(key, None)
                self._unix_ms.pop(key, None)
            return existed

    def list_raw(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(
                key
                for key in set(self._base_values) | set(self._values)
                if key.startswith(prefix) and self._current(key) is not None
            )

    def changes(self) -> tuple[dict[str, tuple[str, int | None]], set[str]]:
        """Return the staged delta: ``{key: (payload, expiry_ms|None)}, deletes``."""
        with self._lock:
            sets: dict[str, tuple[str, int | None]] = {}
            deletes: set[str] = set()
            for key in self._values:
                current = self._current(key)
                if current is None:
                    if key in self._base_values:
                        deletes.add(key)
                    continue
                sets[key] = (current[0], self._unix_ms.get(key))
            for key in self._deleted:
                if key in self._base_values:
                    deletes.add(key)
            return sets, deletes

    def current_string_values(self) -> list[str]:
        """Every string leaf visible in the staged namespace (base + staged)."""
        with self._lock:
            leaves: list[str] = []
            for key in set(self._base_values) | set(self._values):
                current = self._current(key)
                if current is None:
                    continue
                leaves.extend(_string_leaves(_decode_or_raw(current[0])))
            return leaves

    def _current(self, key: str) -> tuple[str, float | None] | None:
        if key in self._deleted:
            return None
        if key in self._values:
            payload = self._values[key]
            deadline = self._monotonic.get(key)
        elif key in self._base_values:
            payload = self._base_values[key]
            deadline = self._base_monotonic.get(key)
        else:
            return None
        if deadline is not None and self._clock() >= deadline:
            return None
        return payload, deadline


# ---------------------------------------------------------------------------
# SDK Storage facades
# ---------------------------------------------------------------------------


class _StorageFacade:
    """Shared SDK ``Storage`` implementation over a backend."""

    def __init__(
        self,
        backend: _StorageBackend,
        *,
        slug: str,
        namespace: str,
        allow_reserved: bool,
        seal: Event | None = None,
    ) -> None:
        self._backend = backend
        self._slug = slug
        self._namespace = namespace
        self._allow_reserved = allow_reserved
        self._seal = seal

    def get(self, key: str, default: Any = None) -> Any:
        """Return the stored value, ``default`` when the key is absent."""
        raw = self._backend.get_raw(validate_key(key))
        if raw is None:
            return default
        return decode_value(raw)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store a strict-JSON value, optionally expiring after ``ttl`` seconds.

        Once the owning runtime is sealed (retired), writes are dropped
        silently so a late worker cannot recreate a wiped secret.
        """
        if self._sealed():
            return
        checked = validate_key(key)
        self._guard_reserved(checked)
        self._backend.set_raw(checked, encode_value(value), validate_ttl(ttl))

    def delete(self, key: str) -> bool:
        """Delete a value; return whether it existed."""
        if self._sealed():
            return False
        checked = validate_key(key)
        self._guard_reserved(checked)
        return self._backend.delete_raw(checked)

    def list_keys(self, prefix: str = "") -> list[str]:
        """Return sorted, deduplicated key names under ``prefix``."""
        return list(self._backend.list_raw(validate_prefix(prefix)))

    def _sealed(self) -> bool:
        """Whether this facade belongs to a retired (sealed) runtime.

        A deliberate lifecycle call (``shutdown``) running on its own worker
        thread carries a thread-scoped permit and may still persist *data*, so a
        plugin can checkpoint buffered state during normal teardown. The permit
        never unblocks the secret namespace: a retired generation must not
        recreate a wiped credential even from its own shutdown hook. A leaked
        worker on a different thread never has the permit in either namespace.
        """
        if self._seal is None or not self._seal.is_set():
            return False
        if self._namespace == SECRET_NAMESPACE:
            return True
        return not retired_writes_allowed()

    def _guard_reserved(self, key: str) -> None:
        if key.startswith(RESERVED_PREFIX) and not self._allow_reserved:
            raise PermissionError(f"storage key {key!r} is reserved for core writes")


class ScopedStorage(_StorageFacade):
    """The live SDK ``Storage`` implementation bound to one slug and namespace."""

    def __init__(
        self,
        redis: SyncRedis,
        slug: str,
        *,
        namespace: str = DATA_NAMESPACE,
        allow_reserved: bool = False,
        seal: Event | None = None,
    ) -> None:
        if namespace not in _NAMESPACES:
            raise StorageError(f"unknown storage namespace: {namespace!r}")
        checked = validate_slug(slug)
        super().__init__(
            _RedisBackend(redis, checked, namespace),
            slug=checked,
            namespace=namespace,
            allow_reserved=allow_reserved,
            seal=seal,
        )


class StorageProxy(_StorageFacade):
    """A live-or-staged facade whose backend is rebound by a transaction.

    Every operation holds the transaction's ``RLock`` for its whole duration so
    backend resolution and the operation itself cannot interleave with promotion:
    a call that starts while a commit is in progress blocks until the rebind has
    happened and then operates on live storage (never a stale staging backend).
    """

    def __init__(
        self,
        backend: _StorageBackend,
        *,
        slug: str,
        namespace: str,
        allow_reserved: bool,
        lock: RLock,
        seal: Event | None = None,
    ) -> None:
        super().__init__(
            backend,
            slug=slug,
            namespace=namespace,
            allow_reserved=allow_reserved,
            seal=seal,
        )
        self._lock = lock

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return super().get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        with self._lock:
            super().set(key, value, ttl)

    def delete(self, key: str) -> bool:
        with self._lock:
            return super().delete(key)

    def list_keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return super().list_keys(prefix)

    def current_string_values(self) -> list[str]:
        """Every string leaf visible on the current (staged or live) backend."""
        with self._lock:
            return self._backend.current_string_values()

    def rebind(self, backend: _StorageBackend) -> None:
        """Point this facade at a different backend under the transaction lock."""
        with self._lock:
            self._backend = backend


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """Non-expiring data/secrets for a plugin (future backup input)."""

    data: dict[str, Any]
    secrets: dict[str, Any]


# ---------------------------------------------------------------------------
# Candidate transaction
# ---------------------------------------------------------------------------


class StorageTransaction:
    """A detached candidate storage session for one plugin replacement.

    Constructed by :meth:`StorageManager.begin_transaction`. Its
    :attr:`context` is a frozen SDK :class:`PluginContext` whose ``storage`` and
    ``secrets`` are staging proxies and whose ``log`` redacts against the initial
    snapshot, the current staged secrets and the current live secrets. Nothing
    reaches live Redis until :meth:`commit`.
    """

    def __init__(
        self,
        manager: StorageManager,
        slug: str,
        *,
        callback_url: str,
        log: Callable[[str, str], None],
        data_snapshot: _NamespaceSnapshot,
        secret_snapshot: _NamespaceSnapshot,
        clock: Callable[[], float],
        wall_clock: Callable[[], float],
        initial_secret_strings: tuple[str, ...],
    ) -> None:
        self._manager = manager
        self._slug = slug
        self._commit_id = uuid.uuid4().hex
        self._initial_secret_strings = initial_secret_strings
        self._state = "open"
        self._lock: RLock = RLock()
        self._state_lock: Lock = Lock()
        # A candidate generation is one runtime generation: when it is retired its
        # seal is set so late workers cannot write or log, exactly like a
        # fresh-install runtime. Without this a reloaded/replaced generation
        # would keep writable facades after ``disconnect`` wiped its secrets.
        self.seal: Event = Event()
        self._data_backend = _StagingBackend(data_snapshot, self._lock, clock, wall_clock)
        self._secret_backend = _StagingBackend(
            secret_snapshot, self._lock, clock, wall_clock
        )
        self._data_proxy = StorageProxy(
            self._data_backend,
            slug=slug,
            namespace=DATA_NAMESPACE,
            allow_reserved=False,
            lock=self._lock,
            seal=self.seal,
        )
        self._secret_proxy = StorageProxy(
            self._secret_backend,
            slug=slug,
            namespace=SECRET_NAMESPACE,
            allow_reserved=False,
            lock=self._lock,
            seal=self.seal,
        )
        self._context = PluginContext(
            slug=slug,
            storage=self._data_proxy,
            secrets=self._secret_proxy,
            log=self._wrap_log(log),
            callback_url=callback_url,
        )

    @property
    def context(self) -> PluginContext:
        """The candidate :class:`PluginContext` bound to staging storage."""
        return self._context

    @property
    def state(self) -> str:
        """One of ``open``, ``committing``, ``committed``, ``discarded``, ``uncertain``."""
        return self._state

    @property
    def commit_id(self) -> str:
        """The idempotent server-side commit receipt id for this transaction."""
        return self._commit_id

    def redact(self, text: str) -> str:
        """Redact ``text`` against the initial, staged and current live secrets."""
        secrets = set(self._initial_secret_strings)
        secrets.update(self._secret_proxy.current_string_values())
        with contextlib.suppress(Exception):
            secrets.update(
                self._manager.live_backend(
                    self._slug, SECRET_NAMESPACE
                ).current_string_values()
            )
        return _redact_text(text, secrets)

    def _wrap_log(self, base_log: Callable[[str, str], None]) -> Callable[[str, str], None]:
        def wrapper(level: str, message: str) -> None:
            # A retired generation never emits logs (see ``_sealed_log``); the
            # redactor cannot cover a cached credential after a disconnect wipe.
            if self.seal.is_set():
                return
            base_log(level, self.redact(message))

        return wrapper

    async def commit(self) -> None:
        """Atomically apply the staged delta to live Redis and rebind to live.

        The entire promotion runs synchronously in one worker thread holding the
        transaction lock, so a concurrent :meth:`discard` cannot linearize a
        "discarded" decision ahead of a live write. If the caller is cancelled,
        the worker is settled to completion before ``CancelledError`` propagates;
        the transaction state then reflects the settled outcome. A lost reply is
        reconciled against the server-side commit receipt, and an unqueryable
        outcome raises :class:`CommitOutcomeUnknown` with state ``uncertain``.
        """
        if not self._state_lock.acquire(blocking=False):
            raise TransactionStateError("storage transaction commit is in progress")
        try:
            if self._state != "open":
                raise TransactionStateError("storage transaction is not open")
            self._state = "committing"
        finally:
            self._state_lock.release()

        task = asyncio.ensure_future(asyncio.to_thread(self._commit_worker))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Settle the worker before propagating so no live write can happen
            # after this returns and the state is coherent.
            await self._settle(task)
            raise

    def _commit_worker(self) -> None:
        """Promotion body; runs in a worker thread holding the transaction lock."""
        with self._lock:
            sets, deletes = self._collect_ops()
            try:
                outcome = self._manager.apply_delta(self._commit_id, sets, deletes)
            except BaseException as exc:
                self._reconcile_after_failure(exc)
                return
            self._apply_outcome(outcome)

    def _collect_ops(self) -> tuple[dict[str, tuple[str, int | None]], set[str]]:
        sets: dict[str, tuple[str, int | None]] = {}
        deletes: set[str] = set()
        data_sets, data_deletes = self._data_backend.changes()
        self._merge(data_sets, data_deletes, sets, deletes, DATA_NAMESPACE)
        secret_sets, secret_deletes = self._secret_backend.changes()
        self._merge(secret_sets, secret_deletes, sets, deletes, SECRET_NAMESPACE)
        return sets, deletes

    def _merge(
        self,
        ns_sets: dict[str, tuple[str, int | None]],
        ns_deletes: set[str],
        sets: dict[str, tuple[str, int | None]],
        deletes: set[str],
        namespace: str,
    ) -> None:
        prefix = f"plugin:{self._slug}:{namespace}:"
        for key, value in ns_sets.items():
            sets[prefix + key] = value
        for key in ns_deletes:
            deletes.add(prefix + key)

    def _apply_outcome(self, outcome: str) -> None:
        """Resolve a trusted script outcome received without a transport error."""
        if outcome == "committed":
            self._mark_committed()
            return
        if outcome == "rolled_back":
            self._mark_open()
            raise StorageError("storage commit was rolled back by the server")
        if outcome == "prewrite_failed":
            self._mark_open()
            raise StorageError("storage commit was rejected before any change")
        if outcome == "conflict":
            self._mark_open()
            raise StorageError("storage commit id conflicted with an existing receipt")
        # ``pending``/``unknown``: a change may exist, so the outcome is unknown.
        self._mark_uncertain()
        raise CommitOutcomeUnknown("storage commit outcome is unknown")

    def _reconcile_after_failure(self, exc: BaseException) -> None:
        """Resolve a transport/script failure against the durable receipt."""
        try:
            outcome = self._manager.read_commit_outcome(self._commit_id)
        except BaseException:
            self._mark_uncertain()
            raise CommitOutcomeUnknown(
                "storage commit outcome could not be reconciled"
            ) from exc
        if outcome == "committed":
            self._mark_committed()
            return
        if outcome == "rolled_back":
            self._mark_open()
            raise StorageError("storage commit was rolled back by the server") from exc
        # pending, absent, or unreadable: cannot prove a safe pre-write state.
        self._mark_uncertain()
        raise CommitOutcomeUnknown("storage commit outcome is unknown") from exc

    def _mark_open(self) -> None:
        with self._state_lock:
            if self._state == "committing":
                self._state = "open"

    def _mark_uncertain(self) -> None:
        with self._state_lock:
            self._state = "uncertain"

    def _mark_committed(self) -> None:
        with self._state_lock:
            self._state = "committed"
        self._data_proxy.rebind(self._manager.live_backend(self._slug, DATA_NAMESPACE))
        self._secret_proxy.rebind(
            self._manager.live_backend(self._slug, SECRET_NAMESPACE)
        )

    @staticmethod
    async def _settle(task: asyncio.Task[None]) -> None:
        """Wait for a commit worker to finish despite repeated cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        with contextlib.suppress(BaseException):
            task.exception()

    def discard(self) -> None:
        """Discard the candidate; staging stays detached so late writes are safe.

        Non-blocking: if a commit is in progress this raises
        :class:`TransactionStateError`, and an unreconciled commit outcome raises
        :class:`CommitOutcomeUnknown`, rather than blocking the event loop.
        """
        if not self._state_lock.acquire(blocking=False):
            raise TransactionStateError("storage transaction commit is in progress")
        try:
            if self._state == "open":
                self._state = "discarded"
            elif self._state == "committing":
                raise TransactionStateError("storage transaction commit is in progress")
            elif self._state == "uncertain":
                raise CommitOutcomeUnknown("storage commit outcome is unknown")
        finally:
            self._state_lock.release()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _encode_commit_args(
    sets: dict[str, tuple[str, int | None]], deletes: set[str]
) -> list[str]:
    """Flatten the delta into the flat ARGV layout the commit script expects."""
    args: list[str] = [str(len(sets))]
    for key, (payload, deadline) in sets.items():
        args.append(key)
        args.append(payload)
        args.append(str(deadline) if deadline is not None else "-1")
    ordered = sorted(deletes)
    args.append(str(len(ordered)))
    args.extend(ordered)
    return args


class StorageManager:
    """Owns scoped storage for every plugin plus core reserved-secret access."""

    def __init__(self, redis_sync: SyncRedis, redis_async: AsyncRedis) -> None:
        self._redis_sync = redis_sync
        self._redis_async = redis_async
        self._clock: Callable[[], float] = time.monotonic
        self._wall_clock: Callable[[], float] = time.time

    def scoped(
        self,
        slug: str,
        *,
        secret: bool = False,
        allow_reserved: bool = False,
        seal: Event | None = None,
    ) -> ScopedStorage:
        """Return a live scoped storage for ``slug`` (data or secret namespace).

        ``seal`` optionally binds the storage to a runtime retirement flag: once
        set, ``set``/``delete`` become silent no-ops while reads keep working.
        """
        return ScopedStorage(
            self._redis_sync,
            slug,
            namespace=SECRET_NAMESPACE if secret else DATA_NAMESPACE,
            allow_reserved=allow_reserved,
            seal=seal,
        )

    def context(
        self,
        slug: str,
        *,
        callback_url: str,
        log: Callable[[str, str], None],
        seal: Event | None = None,
    ) -> PluginContext:
        """Build an SDK :class:`PluginContext` for a trusted plugin instance.

        When ``seal`` is supplied, both scoped facades honour it so a retired
        runtime's late worker threads cannot write or delete anything.
        """
        return PluginContext(
            slug=validate_slug(slug),
            storage=self.scoped(slug, seal=seal),
            secrets=self.scoped(slug, secret=True, seal=seal),
            log=log,
            callback_url=callback_url,
        )

    async def begin_transaction(
        self,
        slug: str,
        *,
        callback_url: str,
        log: Callable[[str, str], None],
    ) -> StorageTransaction:
        """Snapshot ``slug`` and return a detached candidate transaction.

        The snapshot captures current data and secrets including their remaining
        TTLs (via ``PTTL``) as both monotonic and absolute Unix deadlines. Reads
        and writes on the returned context stay detached until
        :meth:`StorageTransaction.commit`.
        """
        checked = validate_slug(slug)
        clock = self._clock
        wall_clock = self._wall_clock
        data_snapshot, secret_snapshot = await asyncio.to_thread(
            self._snapshot_for_transaction, checked, clock, wall_clock
        )
        return StorageTransaction(
            self,
            checked,
            callback_url=callback_url,
            log=log,
            data_snapshot=data_snapshot,
            secret_snapshot=secret_snapshot,
            clock=clock,
            wall_clock=wall_clock,
            initial_secret_strings=_snapshot_string_leaves(secret_snapshot),
        )

    def live_backend(self, slug: str, namespace: str) -> _StorageBackend:
        """Return a live backend for one namespace (used to rebind a proxy)."""
        return _RedisBackend(self._redis_sync, validate_slug(slug), namespace)

    def apply_delta(
        self,
        commit_id: str,
        sets: dict[str, tuple[str, int | None]],
        deletes: set[str],
    ) -> str:
        """Apply a staged delta atomically through the server-side commit script.

        Returns the script's authoritative outcome (see ``_COMMIT_OUTCOMES``).
        Prevalidated inputs keep the script free of argument errors; expired
        deadlines are deleted rather than resurrected; a transient receipt makes
        retrying the same ``commit_id`` idempotent. Runs in a worker thread; the
        caller holds the transaction lock.
        """
        args = _encode_commit_args(sets, deletes)
        result = self._redis_sync.eval(
            _COMMIT_SCRIPT, 1, self.receipt_key(commit_id), commit_id, *args
        )
        outcome = result.decode("utf-8") if isinstance(result, bytes) else str(result)
        if outcome not in _COMMIT_OUTCOMES:
            raise StorageError("storage commit returned an unexpected outcome")
        return outcome

    def read_commit_receipt(self, commit_id: str) -> str | None:
        """Return the raw transient receipt value for ``commit_id``, if any."""
        return self._redis_sync.get(self.receipt_key(commit_id))

    def read_commit_outcome(self, commit_id: str) -> str | None:
        """Decode a receipt into ``committed``/``rolled_back``/``pending`` or ``None``."""
        raw = self.read_commit_receipt(commit_id)
        if raw is None:
            return None
        prefix = f"{commit_id}:"
        if raw.startswith(prefix):
            return raw[len(prefix) :]
        return None

    @staticmethod
    def receipt_key(commit_id: str) -> str:
        """Return the transient receipt key for ``commit_id`` (excluded from backups)."""
        return f"{RECEIPT_PREFIX}{commit_id}"

    def _snapshot_for_transaction(
        self,
        slug: str,
        clock: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> tuple[_NamespaceSnapshot, _NamespaceSnapshot]:
        now_monotonic = clock()
        now_wall_ms = int(wall_clock() * 1000)
        return (
            self._snapshot_namespace_sync(slug, DATA_NAMESPACE, now_monotonic, now_wall_ms),
            self._snapshot_namespace_sync(
                slug, SECRET_NAMESPACE, now_monotonic, now_wall_ms
            ),
        )

    def _snapshot_namespace_sync(
        self,
        slug: str,
        namespace: str,
        now_monotonic: float,
        now_wall_ms: int,
    ) -> _NamespaceSnapshot:
        prefix = f"plugin:{slug}:{namespace}:"
        values: dict[str, str] = {}
        monotonic: dict[str, float | None] = {}
        unix_ms: dict[str, int | None] = {}
        for full in self._redis_sync.scan_iter(match=prefix + "*"):
            if not full.startswith(prefix):
                continue
            raw = self._redis_sync.get(full)
            if raw is None:
                continue
            pttl = self._redis_sync.pttl(full)
            if pttl == -2:
                continue
            key = full[len(prefix) :]
            if pttl == -1:
                monotonic[key] = None
                unix_ms[key] = None
            else:
                if pttl <= 0:
                    continue
                monotonic[key] = now_monotonic + pttl / 1000.0
                unix_ms[key] = now_wall_ms + pttl
            values[key] = raw
        return _NamespaceSnapshot(values, monotonic, unix_ms)

    async def set_core_secret(
        self,
        slug: str,
        key: str,
        value: Any,
        *,
        ttl: int | None = None,
    ) -> None:
        """Write a reserved ``core.`` secret on behalf of the core."""
        checked = validate_key(key)
        if not checked.startswith(RESERVED_PREFIX):
            raise InvalidKeyError("core secrets must use the 'core.' prefix")
        checked_ttl = validate_ttl(ttl)
        if checked_ttl is not None:
            validate_representable_ttl(checked_ttl)
        payload = encode_value(value)
        await self._redis_async.set(
            self._secret_prefix(slug) + checked, payload, ex=checked_ttl
        )

    async def get_core_secret(self, slug: str, key: str, default: Any = None) -> Any:
        """Read a reserved ``core.`` secret."""
        raw = await self._redis_async.get(self._secret_prefix(slug) + validate_key(key))
        if raw is None:
            return default
        return decode_value(raw)

    async def delete_core_secret(self, slug: str, key: str) -> bool:
        """Delete a reserved ``core.`` secret; return whether it existed."""
        result = await self._redis_async.delete(
            self._secret_prefix(slug) + validate_key(key)
        )
        return bool(result)

    async def secret_names(self, slug: str) -> list[str]:
        """Return sorted secret key names (including reserved ``core.`` keys)."""
        return await self._names(slug, SECRET_NAMESPACE)

    async def data_names(self, slug: str) -> list[str]:
        """Return sorted data key names."""
        return await self._names(slug, DATA_NAMESPACE)

    async def snapshot(self, slug: str) -> StorageSnapshot:
        """Return all non-expiring data and secret values for ``slug``."""
        return StorageSnapshot(
            data=await self._snapshot_namespace(slug, DATA_NAMESPACE),
            secrets=await self._snapshot_namespace(slug, SECRET_NAMESPACE),
        )

    def _namespace_prefix(self, slug: str, namespace: str) -> str:
        return f"plugin:{validate_slug(slug)}:{namespace}:"

    def _secret_prefix(self, slug: str) -> str:
        return self._namespace_prefix(slug, SECRET_NAMESPACE)

    async def _names(self, slug: str, namespace: str) -> list[str]:
        prefix = self._namespace_prefix(slug, namespace)
        found: set[str] = set()
        async for full in self._redis_async.scan_iter(match=prefix + "*"):
            if full.startswith(prefix):
                found.add(full[len(prefix) :])
        return sorted(found)

    async def _snapshot_namespace(self, slug: str, namespace: str) -> dict[str, Any]:
        prefix = self._namespace_prefix(slug, namespace)
        values: dict[str, Any] = {}
        async for full in self._redis_async.scan_iter(match=prefix + "*"):
            if not full.startswith(prefix):
                continue
            raw = await self._redis_async.get(full)
            if raw is None:
                continue
            # Non-expiring keys only: -1 means no TTL, > -1 is transient.
            if await self._redis_async.ttl(full) != -1:
                continue
            values[full[len(prefix) :]] = decode_value(raw)
        return values


__all__ = [
    "DATA_NAMESPACE",
    "KEY_PATTERN",
    "MAX_REDIS_DEADLINE_MS",
    "MAX_VALUE_BYTES",
    "PREFIX_PATTERN",
    "RECEIPT_PREFIX",
    "RECEIPT_TTL_SECONDS",
    "RESERVED_PREFIX",
    "SECRET_NAMESPACE",
    "CommitOutcomeUnknown",
    "InvalidKeyError",
    "InvalidPrefixError",
    "InvalidSlugError",
    "InvalidTtlError",
    "ScopedStorage",
    "StorageDecodeError",
    "StorageError",
    "StorageManager",
    "StorageProxy",
    "StorageSnapshot",
    "StorageTransaction",
    "TransactionStateError",
    "decode_value",
    "encode_value",
    "validate_key",
    "validate_prefix",
    "validate_representable_ttl",
    "validate_slug",
    "validate_ttl",
]
