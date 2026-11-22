"""Plugin lifecycle registry: publish, load, connect, reload and remove.

The registry validates packages through :mod:`custom_mcp.plugins.packaging`,
stores the original ZIP bytes as the source of truth, keeps a freshly extracted
copy under ``DATA_DIR/plugins/{slug}`` and records metadata in Redis. When a
:class:`~custom_mcp.plugins.host.PluginHost` and
:class:`~custom_mcp.plugins.loader.PluginLoader` are injected, it also loads the
extracted code off the event loop, caches its validated tools and performs the
Phase 2 auto-connect policy. An optional ``on_runtime_change`` observer (set by
``main.py`` to mount/unmount the MCP router) is notified on load and drop; it is
``None`` for store-only managers, so lifecycle semantics are unchanged.

Without those collaborators the manager remains a store-only registry (used by
the Phase 1 unit tests); production always injects the full stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from jsonschema import Draft202012Validator

from custom_mcp.logging import get_logger
from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.loader import PluginLoader
from custom_mcp.plugins.logs import PluginLogs
from custom_mcp.plugins.manifest import SLUG_RE, Manifest, ManifestError
from custom_mcp.plugins.packaging import (
    InvalidPluginError,
    PackageTooLargeError,
    ValidatedPackage,
    extract_package,
    validate_package,
)
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from custom_mcp.storage.manager import StorageManager, StorageTransaction
from custom_mcp.storage.redis_client import AsyncRedis
from mcp_plugin_sdk import AuthMode, ConnectResult, PluginContext, Status, Tool

logger = get_logger("plugins.manager")

#: A slug must be used verbatim for every path and Redis key built from it.
SLUG_PATTERN = SLUG_RE
_CORE_PLUGINS_KEY = "core:plugins"
_OAUTH_STATE_PREFIX = "core:oauth_state:"
#: OAuth state lifetime (seconds). Kept a module constant, not a new setting.
_OAUTH_STATE_TTL_SECONDS = 600
_OAUTH_REDIRECT_INVALID = "The plugin returned an invalid OAuth sign-in URL."
_LOG_LIMIT = 50
_ERROR_MESSAGE_LIMIT = 300

# Only scratch directories created by this manager are ever cleaned. The names
# are random and live directly below DATA_DIR/tmp.
_OWNED_TMP_NAME = re.compile(
    r"(?:install|extract|package|old|restore)-[0-9a-f]{32}"
)
_INSTALL_MODES = frozenset({"reject", "replace"})

#: Load-state strings exposed by the read models.
LOADING = "loading"
LOADED = "loaded"
LOAD_FAILED = "load_failed"
UNLOADING = "unloading"

#: Internal on-disk journal dirs for an unresolved replacement commit. The
#: ``pending-`` name is deliberately outside the owned-scratch cleanup pattern.
_PENDING_PREFIX = "pending-"
_PENDING_JOURNAL = "journal.json"
_PENDING_OLD_ZIP = "old.zip"
_PENDING_OLD_DIR = "old_dir"

#: Optional observer invoked when a slug's live runtime is mounted or dropped.
#: ``None`` unloads/unmounts; a runtime mounts it. It is set after construction
#: (typically by ``main.py`` wiring the MCP router) and is ``None`` for
#: store-only managers, so existing lifecycle semantics are unchanged.
RuntimeChangeHook = Callable[[str, "PluginRuntime | None"], Awaitable[None]]

#: Optional observer that drains in-flight MCP calls for a slug without retiring
#: it. Called before a plugin instance is shut down during a replacement/reload so
#: an in-flight old call completes against a live instance and any writes it makes
#: are captured by the replacement snapshot. ``None`` for store-only managers.
RuntimeDrainHook = Callable[[str], Awaitable[None]]

_EMPTY_META: dict[str, Any] = {
    "version": None,
    "installed_at": None,
    "auth_mode": None,
    "status": "not_connected",
    "fail_count": 0,
    "transient_error_count": 0,
    "last_ping_at": None,
    "last_error": None,
}


class _ReentrantLock(asyncio.Lock):
    """A per-slug asyncio lock a single task may re-acquire.

    Composite lifecycle work (backup restore) holds the lock across several
    manager calls that each ``async with`` the same per-slug lock. Reentrancy is
    scoped to the owning task, so a different task still waits, preserving
    mutual exclusion with ordinary import/connect/remove/reload operations.
    """

    def __init__(self) -> None:
        super().__init__()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def acquire(self) -> Literal[True]:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return True
        await super().acquire()
        self._owner = task
        self._depth = 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._owner = None
            self._depth = 0
            super().release()


class PluginExistsError(Exception):
    """Raised when an install would replace an existing slug in reject mode."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"Plugin {slug!r} is already installed")


class PluginUpgradeUncertain(RuntimeError):
    """A replacement's storage commit outcome is unresolved.

    Raised when the storage commit could not be reconciled (``uncertain``). The
    manager preserves the old and new code and blocks further lifecycle changes
    for the slug; the message is intentionally generic and maps to the existing
    generic ``internal_error`` response.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(
            "Plugin upgrade outcome is unresolved and requires operator reconciliation."
        )


class PluginConnectError(Exception):
    """A plugin-domain connect/disconnect failure.

    ``code`` is one of ``not_found``, ``not_loaded`` or ``invalid_request`` and
    maps to an HTTP status through the API error layer. ``message`` is always a
    client-safe description that never contains plugin exception text or a
    credential value.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class PluginRecord:
    """In-memory state for one installed (or discovered) package.

    ``runtime`` holds the concrete immutable generation identity for the loaded
    (or failed) plugin code. A store-only manager leaves it ``None`` and keeps
    ``load_state`` at ``loading``.
    """

    slug: str
    manifest: Manifest | None
    warnings: tuple[str, ...] = ()
    load_state: str = LOADING
    load_error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    runtime: PluginRuntime | None = None


@dataclass(slots=True)
class _PublicationState:
    """Mutable facts about a partially published install.

    Worker threads update these flags *before* the corresponding async caller
    resumes, so a cancelled ``to_thread`` step can be settled and rolled back
    using accurate state.
    """

    package_published: bool = False
    dir_published: bool = False
    old_dir_moved: bool = False
    meta_written: bool = False
    membership_written: bool = False
    commit_settled: bool = False
    commit_uncertain: bool = False
    commit_id: str | None = None


def is_valid_slug(slug: object) -> bool:
    """Return ``True`` only for a complete, safe plugin slug."""
    return isinstance(slug, str) and SLUG_PATTERN.fullmatch(slug) is not None


def tool_to_dict(tool: Tool) -> dict[str, Any]:
    """Serialize an SDK :class:`Tool` using the SDK's own key names."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:_ERROR_MESSAGE_LIMIT]


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _absolute_http_url(value: object) -> str | None:
    """Return ``value`` when it is a clean absolute http(s) URL, else ``None``.

    Rejects whitespace/control characters and any non-http(s) scheme (including
    scheme-relative or relative references) so a hostile plugin value can never
    become a browser redirect target.
    """
    if not isinstance(value, str) or not value:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return value


def _with_state_param(url: str, state: str) -> str:
    """Insert/replace the ``state`` query parameter of an absolute URL.

    The existing query is preserved byte-for-byte (only a ``state`` pair is
    dropped) rather than parsed and re-encoded, so a value's original encoding
    is never rewritten into a form a later redactor would not recognise.
    """
    parts = urlsplit(url)
    kept: list[str] = []
    if parts.query:
        for pair in parts.query.split("&"):
            if not pair:
                continue
            key = pair.split("=", 1)[0]
            if unquote(key) == "state":
                continue
            kept.append(pair)
    kept.append(f"state={quote(state, safe='')}")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment)
    )


def _secret_encodings(secret: str) -> tuple[str, ...]:
    """Return the common textual encodings of ``secret`` used for redaction.

    Covers the raw value, percent-encoding, form encoding and the lowercase
    hex-escape variants, so a simple encoding is rewritten in place.
    """
    forms: set[str] = {secret, quote(secret, safe=""), quote_plus(secret)}
    forms.update(form.lower() for form in tuple(forms))
    return tuple(form for form in forms if form)


def _decoded_forms(text: str) -> tuple[str, ...]:
    """Return a decode ladder so no alternative encoding can hide a secret.

    Covers the raw text, every single percent/form decode and all four
    two-step orders, including the mixed ``unquote(unquote_plus(...))`` and
    ``unquote_plus(unquote(...))``. A secret containing both spaces and a
    literal ``+`` can otherwise survive double encoding: repeated percent
    decoding leaves the spaces as ``+`` while repeated form decoding changes
    the genuine ``+`` into a space, but the mixed order restores the secret.
    """
    first = unquote(text)
    plus = unquote_plus(text)
    return (
        text,
        first,
        plus,
        unquote(first),
        unquote_plus(plus),
        unquote(plus),
        unquote_plus(first),
    )


def _text_contains(text: str, secret: str) -> bool:
    """Whether ``secret`` appears in ``text`` under any decoded form."""
    return any(secret in form for form in _decoded_forms(text))


def _redact_schema_leaves(value: Any, redact: Callable[[str], str]) -> tuple[Any, bool]:
    """Return a deep copy of a JSON-schema tree with every string leaf redacted.

    Both object keys and string values are rewritten. A credential can be hidden
    in a property name, an ``enum`` member, a ``const``, a ``default``, a
    ``title``/``description`` or a nested ``$defs`` key, and ``tools/list`` serves
    all of them, so every string anywhere in the tree is treated as untrusted.

    The second element reports whether redaction *changed* anything, so the
    caller only pays for schema re-validation when it actually rewrote a value.
    """
    if isinstance(value, str):
        safe = redact(value)
        return safe, safe != value
    if isinstance(value, dict):
        safe_dict: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            safe_key = redact(key) if isinstance(key, str) else key
            safe_item, item_changed = _redact_schema_leaves(item, redact)
            changed = changed or item_changed or safe_key != key
            safe_dict[safe_key] = safe_item
        # A key rewrite that collides with another key would silently drop a
        # property; treat that as damage so the tool is dropped instead.
        if len(safe_dict) != len(value):
            changed = True
        return safe_dict, changed
    if isinstance(value, (list, tuple)):
        safe_list: list[Any] = []
        changed = False
        for item in value:
            safe_item, item_changed = _redact_schema_leaves(item, redact)
            safe_list.append(safe_item)
            changed = changed or item_changed
        return safe_list, changed
    return value, False


def _schema_reference_signature(schema: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the ``$ref`` values and definition names a schema depends on.

    Redaction can rewrite a ``$ref`` or a ``$defs`` key, which would leave the
    schema structurally valid but unresolvable at argument-validation time. The
    signature lets the caller drop such a tool instead of advertising a schema
    that would fail every call.
    """
    refs: list[str] = []
    defs: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "$ref" and isinstance(item, str):
                    refs.append(item)
                elif key in ("$defs", "definitions") and isinstance(item, dict):
                    defs.extend(str(name) for name in item)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return tuple(sorted(refs)), tuple(sorted(defs))


class PluginManager:
    """Publish, load, connect and remove plugin packages."""

    def __init__(
        self,
        redis: AsyncRedis,
        *,
        data_dir: str | Path,
        max_plugin_zip_bytes: int,
        base_url: str,
        core_version: str = "0.1.0",
        host: PluginHost | None = None,
        loader: PluginLoader | None = None,
        storage: StorageManager | None = None,
        logs: PluginLogs | None = None,
        per_plugin_concurrency: int = 4,
    ) -> None:
        if per_plugin_concurrency < 1:
            raise ValueError("per_plugin_concurrency must be positive")
        if (host is not None or loader is not None) and (
            host is None or loader is None or storage is None
        ):
            raise ValueError(
                "host, loader and storage must be supplied together to enable loading"
            )
        self._redis = redis
        self._data_dir = Path(data_dir)
        self._packages_dir = self._data_dir / "packages"
        self._plugins_dir = self._data_dir / "plugins"
        self._tmp_dir = self._data_dir / "tmp"
        self._max_zip_bytes = max_plugin_zip_bytes
        self._base_url = base_url.rstrip("/")
        self._core_version = core_version
        self._host = host
        self._loader = loader
        self._storage = storage
        self._logs = logs
        self._per_plugin_concurrency = per_plugin_concurrency
        # One shared budget for the whole exit teardown, so process exit time
        # does not scale with the number of installed plugins. Three times the
        # per-plugin ``shutdown`` hook bound lets a few slow hooks finish while
        # still bounding the total; ``main`` sizes the container grace period
        # against the same figure.
        self._teardown_timeout = (
            3.0 * host.shutdown_timeout if host is not None else 15.0
        )

        self._records: dict[str, PluginRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Optional runtime-change observer (set by main.py to keep the MCP router
        # in sync). Kept default-None and non-breaking for store-only managers.
        self.on_runtime_change: RuntimeChangeHook | None = None
        # Optional pre-unload drain observer (set by main.py). It lets an
        # in-flight MCP call finish before its plugin instance is shut down.
        self.on_runtime_drain: RuntimeDrainHook | None = None
        # Stable per-slug semaphores, recreated never: they must survive a
        # runtime replacement so late workers keep counting against the slug.
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._generations: dict[str, int] = {}
        # Slugs with an unresolved replacement commit; blocked from lifecycle
        # changes until an operator reconciles the receipt.
        self._pending: dict[str, str] = {}
        # Serializes creation of stable per-slug locks and unknown-slug
        # install/remove decisions without holding a lock during slow I/O.
        self._registry_guard = asyncio.Lock()

    @property
    def loading_enabled(self) -> bool:
        """Whether the manager loads and connects plugin code."""
        return self._host is not None and self._loader is not None

    @property
    def max_plugin_zip_bytes(self) -> int:
        """The configured compressed package limit in bytes."""
        return self._max_zip_bytes

    @property
    def core_version(self) -> str:
        """The core version every package is validated against."""
        return self._core_version

    async def _lock_for(self, slug: str) -> asyncio.Lock:
        """Return the stable lifecycle lock for a slug, creating it if needed.

        The lock is reentrant for a single task so a composite operation (backup
        restore) can hold it across several manager calls that each acquire it.
        """
        async with self._registry_guard:
            lock = self._locks.get(slug)
            if lock is None:
                lock = _ReentrantLock()
                self._locks[slug] = lock
            return lock

    async def _sem_for(self, slug: str) -> asyncio.Semaphore:
        """Return the stable per-slug concurrency semaphore."""
        async with self._registry_guard:
            sem = self._sems.get(slug)
            if sem is None:
                sem = asyncio.Semaphore(self._per_plugin_concurrency)
                self._sems[slug] = sem
            return sem

    # --- cancellation-safe worker helpers ------------------------------

    @staticmethod
    async def _settle_worker(task: asyncio.Task[Any]) -> None:
        """Wait for a worker thread to finish, ignoring further cancellation."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done():
            with contextlib.suppress(BaseException):
                task.exception()

    async def _await_worker(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run a filesystem worker and settle it before propagating cancellation.

        ``asyncio.to_thread`` cannot stop a running thread. Waiting for the
        worker makes its mutation visible before the caller starts rollback.
        """
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._settle_worker(task)
            raise

    async def _run_worker_uncancellable(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run rollback/cleanup work to completion even while being cancelled."""
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                continue

    async def _run_coro_uncancellable(
        self, coro: Coroutine[Any, Any, Any]
    ) -> Any:
        """Run Redis rollback work to completion even while being cancelled."""
        task = asyncio.ensure_future(coro)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                continue

    # --- lifecycle ------------------------------------------------------

    async def startup(self) -> None:
        """Prepare directories, clean owned scratch, re-extract and load."""
        await asyncio.to_thread(self._ensure_directories)
        # An unresolved upgrade leaves a pending journal; halt before scratch
        # cleanup or loading so old+new code is preserved and never auto-loaded
        # in a possibly mixed state.
        pending = await asyncio.to_thread(self._scan_pending)
        if pending:
            slugs = ", ".join(sorted(str(item.get("slug", "?")) for item in pending))
            raise RuntimeError(
                f"unresolved plugin upgrade for {slugs}; "
                "reconcile the storage commit receipt before restart"
            )
        await asyncio.to_thread(self._cleanup_tmp)
        package_files = await asyncio.to_thread(self._list_package_files)

        # Publish records incrementally so a cancellation mid-startup leaves
        # already-loaded runtimes discoverable for ``shutdown_all``.
        async with self._registry_guard:
            self._records = {}
        for path in package_files:
            slug = path.stem
            if not is_valid_slug(slug):
                logger.warning(
                    "skipping plugin package with invalid filename slug",
                    extra={"extra_fields": {"filename": path.name}},
                )
                continue
            lock = await self._lock_for(slug)
            sem = await self._sem_for(slug)
            async with lock:
                record = await self._load_stored_package(slug, path)
                # Register the record *before* loading so a cancellation during
                # the post-load connect still leaves the runtime discoverable
                # for ``shutdown_all``.
                async with self._registry_guard:
                    self._records[slug] = record
                if (
                    self.loading_enabled
                    and record.load_state != LOAD_FAILED
                    and record.manifest is not None
                ):
                    await self._load_runtime(record, lock=lock, sem=sem)

        await self._sync_membership()

    async def _load_stored_package(self, slug: str, path: Path) -> PluginRecord:
        try:
            zip_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError:
            logger.warning(
                "plugin package could not be read",
                extra={"extra_fields": {"slug": slug}},
                exc_info=True,
            )
            return PluginRecord(
                slug=slug,
                manifest=None,
                load_state=LOAD_FAILED,
                load_error="The stored plugin package could not be read.",
            )

        try:
            validated = await asyncio.to_thread(
                validate_package,
                zip_bytes,
                max_size_bytes=self._max_zip_bytes,
                core_version=self._core_version,
            )
        except (ManifestError, InvalidPluginError, PackageTooLargeError) as exc:
            safe = _safe_error(exc)
            reason = await self._redact_or(slug, safe, "<redacted>")
            logger.warning(
                "stored plugin package is invalid",
                extra={"extra_fields": {"slug": slug, "reason": reason}},
            )
            return PluginRecord(
                slug=slug,
                manifest=None,
                load_state=LOAD_FAILED,
                load_error=await self._redact_or(
                    slug, safe, "The stored plugin package is invalid."
                ),
            )
        except Exception:
            logger.warning(
                "unexpected failure validating stored plugin package",
                extra={"extra_fields": {"slug": slug}},
            )
            return PluginRecord(
                slug=slug,
                manifest=None,
                load_state=LOAD_FAILED,
                load_error="The stored plugin package could not be validated.",
            )

        if validated.manifest.name != slug:
            logger.warning(
                "plugin package filename does not match manifest slug",
                extra={"extra_fields": {"slug": slug}},
            )
            return PluginRecord(
                slug=slug,
                manifest=None,
                load_state=LOAD_FAILED,
                load_error="The package filename does not match the manifest slug.",
            )

        try:
            await self._replace_extracted(slug, validated)
        except Exception:
            logger.exception(
                "stored plugin package could not be extracted",
                extra={"extra_fields": {"slug": slug}},
            )
            return PluginRecord(
                slug=slug,
                manifest=validated.manifest,
                warnings=self._warnings(validated),
                load_state=LOAD_FAILED,
                load_error="The stored plugin package could not be extracted.",
            )

        try:
            await self._ensure_meta(validated.manifest)
        except Exception:
            logger.exception(
                "plugin metadata could not be initialized",
                extra={"extra_fields": {"slug": slug}},
            )
            return PluginRecord(
                slug=slug,
                manifest=validated.manifest,
                warnings=self._warnings(validated),
                load_state=LOAD_FAILED,
                load_error="The plugin metadata could not be initialized.",
            )

        return PluginRecord(
            slug=slug,
            manifest=validated.manifest,
            warnings=self._warnings(validated),
            load_state=LOADING,
            load_error=None,
        )

    # --- runtime loading and connect ------------------------------------

    def _log_callback(self, slug: str) -> Callable[[str, str], None]:
        if self._logs is not None:
            return self._logs.callback(slug)
        return lambda level, message: None

    def _build_context(
        self,
        slug: str,
        *,
        seal: Event | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> PluginContext:
        assert self._storage is not None  # guarded by loading_enabled
        return self._storage.context(
            slug,
            callback_url=self._callback_url(slug),
            log=self._log_callback(slug) if log is None else log,
            seal=seal,
        )

    @staticmethod
    def _retire_seal(runtime: PluginRuntime) -> None:
        """Seal a retired runtime so late worker threads cannot write or log."""
        seal = runtime.seal
        if seal is not None:
            seal.set()

    def _new_runtime(
        self,
        slug: str,
        manifest: Manifest,
        *,
        lock: asyncio.Lock,
        sem: asyncio.Semaphore,
        context_override: PluginContext | None = None,
        seal_override: Event | None = None,
    ) -> PluginRuntime:
        generation = self._generations.get(slug, 0) + 1
        self._generations[slug] = generation
        if context_override is not None:
            # A candidate generation seals through the transaction's own Event, so
            # its late workers stop writing/logging once it is retired (unload,
            # disconnect, remove) exactly like a fresh-install generation.
            context = context_override
            seal: Event | None = seal_override
        else:
            seal = Event()
            base_log = self._log_callback(slug)

            def _sealed_log(level: str, message: str) -> None:
                # A retired generation never emits logs, even during its shutdown
                # hook: the redactor only knows *current* secrets, so a cached
                # credential logged at teardown would otherwise leak.
                if seal.is_set():
                    return
                base_log(level, message)

            context = self._build_context(slug, seal=seal, log=_sealed_log)
        return PluginRuntime(
            slug=slug,
            manifest=manifest,
            context=context,
            lock=lock,
            sem=sem,
            generation=generation,
            seal=seal,
        )

    async def _begin_candidate_txn(self, slug: str) -> StorageTransaction:
        """Open a detached candidate storage transaction for a replacement."""
        assert self._storage is not None
        return await self._storage.begin_transaction(
            slug, callback_url=self._callback_url(slug), log=self._log_callback(slug)
        )

    async def _commit_candidate(
        self, txn: StorageTransaction, state: _PublicationState
    ) -> None:
        """Commit ``txn`` to live storage, surviving caller cancellation.

        The commit task is shielded so an external cancellation cannot interrupt
        the atomic MULTI/EXEC. Afterwards the *authoritative* signal is
        ``txn.state``: if it is ``committed`` the commit point passed even when
        the surrounding await raised ``CancelledError``, so the caller keeps the
        new runtime/files/meta instead of rolling back over new data.
        """
        task = asyncio.ensure_future(txn.commit())
        cancelled = False
        failure: BaseException | None = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                # Record before the done-check so a cancel coincident with the
                # commit finishing still propagates.
                cancelled = True
                if task.done():
                    break
                continue
            except BaseException as exc:
                # A normal commit failure (e.g. CommitOutcomeUnknown): the
                # authoritative decision is ``txn.state`` below, inspected only
                # after the worker has settled.
                failure = exc
                break
        if failure is None and task.done():
            if task.cancelled():
                failure = asyncio.CancelledError()
            else:
                try:
                    failure = task.exception()
                except asyncio.CancelledError as cancel_exc:
                    failure = cancel_exc
        if txn.state == "uncertain":
            # Unreconciled outcome: the candidate writes may or may not be live.
            # Record it and raise a generic error; the caller preserves both the
            # old and new code rather than rolling back over possibly-live data.
            state.commit_uncertain = True
            state.commit_id = txn.commit_id
            if cancelled or isinstance(failure, asyncio.CancelledError):
                # Preserve the uncertain flags for the caller, but let the
                # caller's cancellation win over the generic upgrade error.
                raise asyncio.CancelledError() from None
            raise PluginUpgradeUncertain(txn.context.slug) from None
        if txn.state == "committed":
            state.commit_settled = True
            if cancelled:
                raise asyncio.CancelledError()
            return
        if failure is not None:
            if cancelled or isinstance(failure, asyncio.CancelledError):
                # The rollback/prewrite decision is already reflected in
                # ``txn.state``; the caller still performs its restore, then the
                # caller's cancellation is surfaced instead of the storage error.
                raise asyncio.CancelledError() from None
            raise failure
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("storage transaction did not commit")

    def _discard_txn(self, txn: StorageTransaction) -> None:
        """Discard an open transaction, never masking the original error."""
        if txn.state != "open":
            return
        try:
            txn.discard()
        except Exception:
            logger.warning(
                "storage_transaction_discard_failed",
                extra={"extra_fields": {"slug": txn.context.slug}},
            )

    async def _unload_runtime_safe(self, runtime: PluginRuntime) -> None:
        """Best-effort bounded unload that completes even under cancellation.

        Only for **rollback** cleanup (candidate unload / old restore), where the
        process must finish regardless of caller cancellation.
        """
        if self._loader is None:
            return
        # Unmount first (drain in-flight MCP calls) so an in-flight call always
        # completes against the still-live instance; both steps settle even under
        # cancellation, matching this rollback helper's contract. Only unmount a
        # runtime this router actually published, so a never-mounted candidate
        # cannot evict the previous generation's live lookup.
        if runtime.mounted is not None:
            await self._notify_runtime_change(runtime.slug, None)
        try:
            await self._run_coro_uncancellable(self._loader.unload(runtime))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "plugin_unload_failed", extra={"extra_fields": {"slug": runtime.slug}}
            )

    async def _forward_unload(self, runtime: PluginRuntime) -> bool:
        """Unload an old runtime before a forward candidate, reporting cancel.

        Unlike rollback cleanup, a forward unload must not swallow a caller
        cancellation. It settles the shutdown (so the runtime reference state is
        accurate) and returns whether the caller was cancelled, so the caller
        restores the old generation and propagates instead of committing v2.
        """
        if self._loader is None:
            return False
        # First drain in-flight MCP calls for this slug so an in-flight old call
        # finishes against the still-live instance and any writes it makes are
        # captured by the replacement snapshot. The old *MCP app* stays mounted;
        # the replacement's own ``mount`` swaps the lookup after this returns.
        cancelled = await self._notify_drain(runtime.slug)
        # Seal before the instance is shut down so any late worker thread from
        # this generation cannot write to storage or log after unload.
        self._retire_seal(runtime)
        task = asyncio.ensure_future(self._loader.unload(runtime))
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                # Record before the done-check: a cancel delivered coincident
                # with the unload finishing must still propagate, never be
                # swallowed as a clean forward unload.
                cancelled = True
                if task.done():
                    break
                continue
            except Exception:
                logger.warning(
                    "plugin_unload_failed",
                    extra={"extra_fields": {"slug": runtime.slug}},
                )
                break
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                logger.warning(
                    "plugin_unload_failed",
                    extra={"extra_fields": {"slug": runtime.slug}},
                )
        return cancelled

    async def _load_runtime(
        self,
        record: PluginRecord,
        *,
        lock: asyncio.Lock,
        sem: asyncio.Semaphore,
        context_override: PluginContext | None = None,
        seal_override: Event | None = None,
        auto_connect: bool = True,
    ) -> bool:
        """Load ``record``'s manifest into a fresh runtime and cache its tools.

        ``auto_connect=False`` skips the auto-connect policy: the plugin is left
        ``not_connected`` with its (redacted) tools cached, so a caller can restore
        state and run a hook before deciding how/whether to connect.
        """
        assert self._host is not None and self._loader is not None
        assert record.manifest is not None
        slug = record.slug
        runtime = self._new_runtime(
            slug,
            record.manifest,
            lock=lock,
            sem=sem,
            context_override=context_override,
            seal_override=seal_override,
        )
        state = await self._loader.load(runtime, self._plugin_dir(slug))
        if state == LoadState.LOADED:
            record.runtime = runtime
            record.load_state = LOADED
            record.load_error = None
            # Redact the loader's listing before it can be read: ``detail()``
            # serves ``record.tools`` verbatim, and ``_after_load_connect`` can
            # block on a slow plugin ``connect`` before it re-lists. Publishing
            # the raw list first would expose a plugin-supplied credential.
            await self._redact_listed_tools(runtime, record, runtime.tools)
            if auto_connect:
                await self._after_load_connect(runtime, record)
            else:
                runtime.status = Status.NOT_CONNECTED
                await self._persist_status(slug, Status.NOT_CONNECTED)
                await self._refresh_tools(runtime, record)
            # Publish the freshly loaded generation to any runtime-change
            # observer (the MCP router mounts it here, atomically replacing and
            # retiring the previous generation). Surface a cancellation observed
            # during the mount so the caller rolls back as usual.
            if await self._notify_runtime_change(slug, runtime):
                raise asyncio.CancelledError()
            return True
        record.runtime = runtime
        record.tools = []
        record.load_state = LOAD_FAILED
        record.load_error = runtime.load_error or "Plugin failed to load."
        await self._persist_status(slug, Status.NOT_CONNECTED)
        return False

    async def _after_load_connect(
        self, runtime: PluginRuntime, record: PluginRecord
    ) -> None:
        """Apply the Phase 2 auto-connect policy without touching load state.

        Connect is attempted when the plugin is ``none``-auth or a mode has been
        selected in stored metadata. A connect failure never marks the plugin
        ``load_failed``: its tools stay cached and listed, and the connection
        status records the error.
        """
        assert self._host is not None
        slug = runtime.slug
        manifest = runtime.manifest
        mode = await self._meta_auth_mode(slug)
        should_connect = AuthMode.NONE in manifest.auth_modes or mode is not None
        if not should_connect:
            runtime.status = Status.NOT_CONNECTED
            await self._persist_status(slug, Status.NOT_CONNECTED)
            return

        try:
            result = await self._host.call(runtime, "connect")
            status = result.status if isinstance(result, ConnectResult) else Status.ERROR
        except asyncio.CancelledError:
            raise
        except PluginCallError as exc:
            status = Status.ERROR
            await self._log_safe(slug, exc.safe_message)
        except Exception:
            status = Status.ERROR
            await self._log_safe(slug, "Plugin connect failed.")

        runtime.status = status
        await self._persist_status(slug, status)
        await self._refresh_tools(runtime, record)

    async def _refresh_tools(
        self,
        runtime: PluginRuntime,
        record: PluginRecord,
        *,
        extra_secrets: tuple[str, ...] = (),
        secrets_known: bool = True,
    ) -> None:
        """Re-list tools after connect; keep the previous cache on failure.

        ``host.call`` validates the tool list inside its worker thread, so the
        manager never re-validates (or touches plugin objects) on the loop.

        ``extra_secrets`` is a pre-wipe snapshot used by ``disconnect`` so a
        plugin that cached a credential in memory still has its description
        redacted even though the stored secret was just deleted. When
        ``secrets_known`` is false the snapshot could not be read and every
        description fails closed instead.
        """
        assert self._host is not None
        try:
            listed = await self._host.call(runtime, "list_tools")
        except asyncio.CancelledError:
            raise
        except PluginCallError as exc:
            await self._log_safe(runtime.slug, exc.safe_message)
            return
        except Exception:
            return
        await self._redact_listed_tools(
            runtime,
            record,
            listed,
            extra_secrets=extra_secrets,
            secrets_known=secrets_known,
        )

    async def _redact_listed_tools(
        self,
        runtime: PluginRuntime,
        record: PluginRecord,
        listed: list[Tool],
        *,
        extra_secrets: tuple[str, ...] = (),
        secrets_known: bool = True,
    ) -> None:
        """Redact a freshly listed tool set and publish it to the record.

        A tool listing is plugin-supplied and every part of it reaches a reader:
        ``detail()`` serves ``record.tools`` verbatim and ``build_plugin_app``
        passes ``input_schema`` straight to FastMCP, so a stored credential can
        hide in a description, a tool *name*, or any string leaf of the JSON
        schema (``default``, ``enum``, ``const``, ``title``, a property name, a
        ``$defs`` key). All of them are redacted here, before the list is
        published, so both the detail API and MCP ``tools/list`` are covered.

        Failing closed is the rule, and it has two documented outcomes:

        * a tool whose **name** would have to change is dropped -- its identity
          embeds a credential, so there is no safe name to advertise;
        * a tool whose redacted **schema** would no longer be usable for argument
          validation (``Draft202012Validator.check_schema`` rejects it, or a
          ``$ref``/``$defs`` name was rewritten) is dropped.

        Redacting a ``default``/``enum``/``const``/``title``/``description``
        keeps the schema valid, so the usual case keeps the tool with the
        credential replaced by ``***``. ``extra_secrets`` is a pre-wipe snapshot
        used by ``disconnect`` so a plugin that cached a credential in memory
        still has its listing redacted even though the stored secret was just
        deleted; when ``secrets_known`` is false that snapshot could not be read
        and every tool fails closed.
        """
        combined = tuple(
            dict.fromkeys((*runtime.retained_secrets, *extra_secrets))
        )
        secrets = await self._redaction_secrets(
            runtime.slug, extra_secrets=combined, secrets_known=secrets_known
        )
        redacted: list[Tool] = []
        for index, tool in enumerate(listed):
            safe = self._redact_tool(runtime.slug, index, tool, secrets)
            if safe is not None:
                redacted.append(safe)
        runtime.tools = redacted
        record.tools = [tool_to_dict(tool) for tool in runtime.tools]

    def _redact_tool(
        self,
        slug: str,
        index: int,
        tool: Tool,
        secrets: tuple[str, ...] | None,
    ) -> Tool | None:
        """Return a fully redacted copy of ``tool``, or ``None`` to drop it.

        Never logs or returns the plugin-supplied name or schema: either may
        embed the credential this method exists to remove.
        """
        if secrets is None:
            # No redactor (or an unreadable pre-wipe snapshot): nothing about
            # this tool can be proven safe, so it must not be advertised.
            self._warn_tool_dropped(slug, index, "unredactable")
            return None
        try:
            name = self._redact_text(tool.name, secrets)
            if name != tool.name:
                # A credential is part of the tool's identity; renaming it would
                # advertise a name the client never asked for.
                self._warn_tool_dropped(slug, index, "name")
                return None
            description = tool.description
            if isinstance(description, str) and description:
                description = self._redact_text(description, secrets)
            schema, changed = _redact_schema_leaves(
                tool.input_schema, lambda text: self._redact_text(text, secrets)
            )
        except Exception:
            self._warn_tool_dropped(slug, index, "redaction_failed")
            return None
        # Only a rewritten schema needs re-validation; an untouched one was
        # already validated by the host when the listing was produced.
        if changed and not self._schema_still_usable(tool.input_schema, schema):
            self._warn_tool_dropped(slug, index, "schema")
            return None
        return Tool(name=name, description=description, input_schema=schema)

    def _redact_text(self, text: str, secrets: tuple[str, ...]) -> str:
        """Redact one string against an already-captured secret set."""
        assert self._logs is not None  # guarded by ``_redaction_secrets``
        return self._logs.redact_secrets(text, secrets)

    @staticmethod
    def _schema_still_usable(original: Any, redacted: Any) -> bool:
        """Whether a redacted schema can still validate tool arguments.

        ``check_schema`` catches a rewritten ``pattern``/``type``/``required``;
        the reference signature catches a rewritten ``$ref`` or ``$defs`` key,
        which ``check_schema`` accepts but which resolves to nothing.
        """
        if not isinstance(redacted, dict):
            return False
        if _schema_reference_signature(original) != _schema_reference_signature(redacted):
            return False
        try:
            Draft202012Validator.check_schema(redacted)
        except Exception:
            return False
        return True

    @staticmethod
    def _warn_tool_dropped(slug: str, index: int, reason: str) -> None:
        """Log a dropped tool without echoing its (possibly secret-bearing) name."""
        logger.warning(
            "plugin_tool_dropped",
            extra={
                "extra_fields": {"slug": slug, "index": index, "reason": reason}
            },
        )

    async def _rebuild_sealed_runtime(
        self,
        record: PluginRecord,
        *,
        lock: asyncio.Lock,
        sem: asyncio.Semaphore,
    ) -> bool:
        """Replace a sealed (retired) generation with a brand-new one.

        A seal is a one-way retirement flag: a worker leaked from the retired
        generation must never become writable again, so a reconnect never clears
        it. Instead the retired generation is unloaded and a fresh runtime
        (fresh :class:`~mcp_plugin_sdk.PluginContext` and fresh seal) is loaded
        from the same extracted code. Every old worker stays bound to the
        permanently sealed facades and its late writes/logs remain dropped.

        Returns whether the fresh generation loaded. A load failure leaves the
        record ``load_failed`` (data and secrets are untouched) so the plugin can
        be reloaded from the stored package.
        """
        assert self._loader is not None  # guarded by loading_enabled
        assert record.manifest is not None
        slug = record.slug
        old_runtime = record.runtime
        if old_runtime is not None:
            # Unmount (drain in-flight calls) *before* the instance is torn down
            # so a concurrent MCP call never observes a half-unloaded runtime.
            await self._notify_runtime_change(slug, None)
            self._retire_seal(old_runtime)
            record.runtime = None
            record.tools = []
            record.load_state = UNLOADING
            try:
                await self._loader.unload(old_runtime)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "plugin_reconnect_unload_failed",
                    extra={"extra_fields": {"slug": slug}},
                )
        runtime = self._new_runtime(slug, record.manifest, lock=lock, sem=sem)
        state = await self._loader.load(runtime, self._plugin_dir(slug))
        record.runtime = runtime
        if state != LoadState.LOADED:
            record.tools = []
            record.load_state = LOAD_FAILED
            record.load_error = runtime.load_error or "Plugin failed to load."
            await self._persist_status(slug, Status.NOT_CONNECTED)
            return False
        record.load_state = LOADED
        record.load_error = None
        # Redact before publishing, exactly as in ``_load_runtime``: a reader
        # must never see the raw loader listing for a rebuilt generation.
        await self._redact_listed_tools(runtime, record, runtime.tools)
        return True

    async def connect(
        self,
        slug: str,
        *,
        mode: str,
        credentials: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate the selected mode, apply credentials and run ``connect``.

        Raises :class:`PluginConnectError` for a plugin-domain failure
        (``not_found``, ``not_loaded`` or ``invalid_request``). For ``api_key``
        the credential is stored first, so the ``on_api_key_set`` hook and the
        plugin instance can read it from ``core.api_key``. For ``oauth`` the
        resolved ``client_id``/``client_secret`` are stored before
        ``on_oauth_credentials_set``; an omitted ``client_id`` reuses the stored
        ``core.oauth_client_id``. A hook or ``connect`` failure is recorded as a
        safe ``last_error``/``error`` status and still returns the refreshed
        detail rather than raising.

        When ``connect`` reports ``oauth_required`` a single-use, slug-bound
        state is created and the returned detail carries a validated absolute
        http(s) ``redirect_url`` (the plugin ``redirect_url`` with ``state``
        inserted/replaced, or the plugin sign-in URL). A hostile/invalid URL is
        treated as a connect failure instead of being surfaced to the browser.

        A generation sealed by ``disconnect`` is never unsealed: it is unloaded
        and a fresh generation replaces it (see :meth:`_rebuild_sealed_runtime`),
        so a leaked worker from the retired generation can never write again.
        """
        if not is_valid_slug(slug):
            raise PluginConnectError("not_found", "No such plugin.")
        self._require_not_pending(slug)
        lock = await self._lock_for(slug)
        async with lock:
            self._require_not_pending(slug)
            record = self._records.get(slug)
            runtime = record.runtime if record is not None else None
            if (
                not self.loading_enabled
                or record is None
                or runtime is None
                or runtime.instance is None
                or record.load_state != LOADED
            ):
                raise PluginConnectError("not_loaded", "The plugin is not loaded.")

            supported = {item.value for item in runtime.manifest.auth_modes}
            if mode not in supported:
                raise PluginConnectError(
                    "invalid_request",
                    "The plugin does not support this authentication mode.",
                )

            # Validate every credential *before* any state change. A rejected
            # request must not unmount or rebuild a loaded, disconnected plugin.
            api_key: str | None = None
            client_id: str | None = None
            client_secret: str | None = None
            if mode == AuthMode.API_KEY.value:
                candidate = (credentials or {}).get("api_key")
                if not isinstance(candidate, str) or not candidate.strip():
                    raise PluginConnectError("invalid_request", "An API key is required.")
                api_key = candidate
            elif mode == AuthMode.OAUTH.value:
                client_id, client_secret = await self._resolve_oauth_credentials(
                    slug, credentials or {}
                )

            if runtime.seal is not None and runtime.seal.is_set():
                # The generation was retired by ``disconnect``. Rebuild it rather
                # than clearing the seal, so leaked workers stay sealed forever.
                sem = await self._sem_for(slug)
                if not await self._rebuild_sealed_runtime(record, lock=lock, sem=sem):
                    # Never leave a stale mounted app for unloadable code.
                    await self._notify_runtime_change(slug, None)
                    raise PluginConnectError(
                        "not_loaded",
                        "The plugin could not be reloaded for this connection.",
                    )
                rebuilt = record.runtime
                assert rebuilt is not None
                runtime = rebuilt

            if api_key is not None:
                assert self._storage is not None  # guarded by loading_enabled
                # Store the exact value (never trimmed) before the hook so the
                # plugin can read ``core.api_key``.
                await self._storage.set_core_secret(slug, "core.api_key", api_key)
                # Remember it so any later description refresh can still redact a
                # credential the plugin cached in memory.
                self.retain_secrets(slug, (api_key,))
            elif mode == AuthMode.OAUTH.value:
                assert self._storage is not None  # guarded by loading_enabled
                assert client_id is not None
                # Store the exact, untrimmed values before the credential hook.
                await self._storage.set_core_secret(
                    slug, "core.oauth_client_id", client_id
                )
                self.retain_secrets(slug, (client_id,))
                if client_secret is not None:
                    await self._storage.set_core_secret(
                        slug, "core.oauth_client_secret", client_secret
                    )
                    self.retain_secrets(slug, (client_secret,))

            assert self._host is not None  # guarded by loading_enabled
            redirect_url: str | None = None
            try:
                if api_key is not None:
                    await self._host.call(runtime, "on_api_key_set", api_key)
                elif mode == AuthMode.OAUTH.value:
                    assert client_id is not None
                    await self._host.call(
                        runtime, "on_oauth_credentials_set", client_id, client_secret
                    )
                result = await self._host.call(runtime, "connect")
            except asyncio.CancelledError:
                raise
            except PluginCallError as exc:
                # Never surface plugin text: ``safe_message`` is host-produced.
                runtime.status = Status.ERROR
                await self._log_safe(slug, exc.safe_message)
                await self._set_auth_mode(slug, mode)
                await self._persist_status(slug, Status.ERROR)
                await self._set_last_error(slug, exc.safe_message)
            except Exception:
                # Defensive: the host normalizes plugin failures, so this only
                # guards a host-side bug without leaking plugin text.
                runtime.status = Status.ERROR
                await self._log_safe(slug, "Plugin connect failed.")
                await self._set_auth_mode(slug, mode)
                await self._persist_status(slug, Status.ERROR)
                await self._set_last_error(slug, "Plugin connect failed.")
            else:
                if mode == AuthMode.OAUTH.value and result.status is Status.OAUTH_REQUIRED:
                    redirect_url = await self._begin_oauth(runtime, slug, result)
                    if redirect_url is not None:
                        # Never surface a stored credential inside the URL the
                        # browser will follow, even when the plugin embedded one;
                        # an encoding that cannot be rewritten is refused.
                        redirect_url = await self._redact_url(slug, redirect_url)
                    if redirect_url is None:
                        # Hostile/invalid/secret-bearing URL: fail safely.
                        runtime.status = Status.ERROR
                        await self._log_safe(slug, _OAUTH_REDIRECT_INVALID)
                        await self._set_auth_mode(slug, mode)
                        await self._persist_status(slug, Status.ERROR)
                        await self._set_last_error(slug, _OAUTH_REDIRECT_INVALID)
                    else:
                        await self._set_auth_mode(slug, mode)
                        runtime.status = Status.OAUTH_REQUIRED
                        await self._persist_status(slug, Status.OAUTH_REQUIRED)
                else:
                    await self._set_auth_mode(slug, mode)
                    runtime.status = result.status
                    await self._persist_status(slug, result.status)

            # Retain whatever credentials the plugin stored during the hook so a
            # later snapshot failure cannot lose the redaction history for them.
            await self.retain_current_secrets(slug)
            await self._refresh_tools(runtime, record)
            await self._notify_runtime_change(slug, runtime)
            detail = await self.detail(slug)
            if detail is None:
                raise PluginConnectError("not_found", "No such plugin.")
            if redirect_url is not None:
                detail["redirect_url"] = redirect_url
            return detail

    async def _resolve_oauth_credentials(
        self, slug: str, credentials: Mapping[str, Any]
    ) -> tuple[str, str | None]:
        """Resolve OAuth client credentials, reusing the stored client id.

        ``client_id`` is optional: when omitted/empty the stored
        ``core.oauth_client_id`` is reused, otherwise the request is rejected
        with ``invalid_request``. ``client_secret`` is optional. Raises
        :class:`PluginConnectError` before any state change on a bad request.
        """
        raw_id = credentials.get("client_id")
        if raw_id is None or (isinstance(raw_id, str) and not raw_id):
            assert self._storage is not None  # guarded by loading_enabled
            stored = await self._storage.get_core_secret(slug, "core.oauth_client_id")
            if not isinstance(stored, str) or not stored:
                raise PluginConnectError("invalid_request", "A client id is required.")
            client_id = stored
        elif isinstance(raw_id, str):
            client_id = raw_id
        else:
            raise PluginConnectError("invalid_request", "A client id is required.")

        raw_secret = credentials.get("client_secret")
        if raw_secret is None:
            client_secret: str | None = None
        elif isinstance(raw_secret, str):
            client_secret = raw_secret
        else:
            raise PluginConnectError(
                "invalid_request", "The client secret must be a string."
            )
        return client_id, client_secret

    async def _begin_oauth(
        self, runtime: PluginRuntime, slug: str, result: ConnectResult
    ) -> str | None:
        """Create a single-use state and build a validated sign-in URL.

        Returns the absolute http(s) URL to surface, or ``None`` when the plugin
        supplied neither a usable ``redirect_url`` nor a usable sign-in URL. The
        state is created first (the plugin sign-in hook needs it); an orphaned
        state simply expires via its EX TTL. The PKCE verifier the plugin stores
        in data ``pkce:{state}`` is retained for redaction, since it lives in the
        data namespace rather than the secret namespace.
        """
        state = await self.create_oauth_state(slug)
        raw = result.redirect_url
        if isinstance(raw, str) and raw:
            validated = _absolute_http_url(raw)
            if validated is None:
                return None
            url = _with_state_param(validated, state)
        else:
            # No plugin redirect URL: fall back to the explicit sign-in hook.
            assert self._host is not None  # guarded by loading_enabled
            try:
                candidate = await self._host.call(
                    runtime, "get_oauth_sign_in_url", state
                )
            except asyncio.CancelledError:
                raise
            except PluginCallError as exc:
                await self._log_safe(slug, exc.safe_message)
                return None
            except Exception:
                await self._log_safe(slug, "Plugin OAuth sign-in URL failed.")
                return None
            if _absolute_http_url(candidate) is None:
                return None
            url = str(candidate)
        await self._retain_pkce_verifier(slug, state)
        return url

    async def _retain_pkce_verifier(self, slug: str, state: str) -> None:
        """Retain a plugin's PKCE verifier, which lives in data ``pkce:{state}``.

        The contract mandates that location, so it is not covered by the secret
        namespace; without this a plugin that echoed the verifier into a log or
        description could leak it. Best effort and secret-free on failure.
        """
        if self._logs is None or not state:
            return
        key = f"plugin:{self._require_slug(slug)}:data:pkce:{state}"
        try:
            raw = await self._redis.get(key)
        except Exception:
            return
        if raw is None:
            return
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = raw
        if isinstance(value, str) and value:
            self.retain_secrets(slug, (value,))

    async def _redact_url(self, slug: str, url: str) -> str | None:
        """Return a URL free of stored credentials, or ``None`` if one remains.

        The OAuth client id is a public protocol identifier that legitimately
        appears in an authorize URL, so it is exempted; every other secret is
        first replaced in its raw, percent-encoded, form-encoded and lowercase
        forms. The result is then re-checked against a decode ladder so an
        encoding this method cannot rewrite is refused rather than leaked.
        """
        if self._logs is None:
            return url
        permitted = await self._stored_client_id(slug)
        secrets = [
            secret
            for secret in dict.fromkeys(await self._logs.redaction_strings(slug))
            if not (permitted is not None and secret == permitted)
        ]
        candidates: list[str] = []
        for secret in secrets:
            candidates.extend(_secret_encodings(secret))
        redacted = self._logs.redact_secrets(url, tuple(candidates))
        if any(_text_contains(redacted, secret) for secret in secrets):
            return None
        return redacted

    async def _stored_client_id(self, slug: str) -> str | None:
        """Return the stored OAuth client id (a public protocol identifier)."""
        if self._storage is None:
            return None
        try:
            value = await self._storage.get_core_secret(slug, "core.oauth_client_id")
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    async def create_oauth_state(self, slug: str) -> str:
        """Create and store a single-use, slug-bound OAuth state token.

        Stored as ``core:oauth_state:{state}`` = ``{slug, created_at}`` JSON with
        an ``EX`` TTL of :data:`_OAUTH_STATE_TTL_SECONDS`. The state is consumed
        exactly once via :meth:`consume_oauth_state` (GETDEL).
        """
        checked = self._require_slug(slug)
        state = secrets.token_urlsafe(32)
        payload = json.dumps(
            {"slug": checked, "created_at": _utc_now()},
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._redis.set(
            f"{_OAUTH_STATE_PREFIX}{state}", payload, ex=_OAUTH_STATE_TTL_SECONDS
        )
        return state

    async def consume_oauth_state(self, state: str, slug: str) -> bool:
        """Atomically consume a state token; ``True`` only when it matched.

        Uses a single Redis ``GETDEL`` so the token is single-use even under
        concurrent callbacks. Returns ``True`` only when the key existed *and*
        its stored ``slug`` equals ``slug``; in either failure case the key (if
        present) is still consumed.
        """
        if not isinstance(state, str) or not state:
            return False
        raw = await self._redis.getdel(f"{_OAUTH_STATE_PREFIX}{state}")
        parsed = _parse_json_object(raw)
        if parsed is None:
            return False
        return parsed.get("slug") == slug

    async def complete_oauth(
        self, slug: str, *, code: str, state: str
    ) -> dict[str, Any]:
        """Finish an OAuth callback under the lifecycle lock.

        Consumes and validates ``state`` (single-use), runs ``on_oauth_result``
        through the host, persists ``auth_mode='oauth'`` and the returned status,
        re-lists tools and re-mounts. A forged/reused/expired/mismatched state
        raises :class:`PluginConnectError` *before* any status change. A plugin
        failure maps to a safe ``error`` status; details never carry plugin text.
        """
        if not is_valid_slug(slug):
            raise PluginConnectError("not_found", "No such plugin.")
        self._require_not_pending(slug)
        lock = await self._lock_for(slug)
        async with lock:
            self._require_not_pending(slug)
            record = self._records.get(slug)
            runtime = record.runtime if record is not None else None
            if (
                not self.loading_enabled
                or record is None
                or runtime is None
                or runtime.instance is None
                or record.load_state != LOADED
            ):
                raise PluginConnectError("not_loaded", "The plugin is not loaded.")
            # Consume before any mutation: a forged/reused/expired/mismatched
            # state must never change status.
            if not await self.consume_oauth_state(state, slug):
                raise PluginConnectError(
                    "invalid_request", "The OAuth state is invalid or expired."
                )
            if runtime.seal is not None and runtime.seal.is_set():
                # A callback must not resurrect a generation retired by disconnect.
                raise PluginConnectError(
                    "invalid_request", "The OAuth state is no longer valid."
                )

            # Retain the authorization code and the data-stored PKCE verifier
            # before the hook runs: a plugin may log either, and neither lives in
            # the secret namespace, so only retention makes them redactable.
            await self._retain_pkce_verifier(slug, state)
            if code:
                self.retain_secrets(slug, (code,))

            assert self._host is not None  # guarded by loading_enabled
            try:
                result = await self._host.call(runtime, "on_oauth_result", code, state)
            except asyncio.CancelledError:
                raise
            except PluginCallError as exc:
                runtime.status = Status.ERROR
                await self._log_safe(slug, exc.safe_message)
                await self._set_auth_mode(slug, AuthMode.OAUTH.value)
                await self._persist_status(slug, Status.ERROR)
                await self._set_last_error(slug, exc.safe_message)
            except Exception:
                runtime.status = Status.ERROR
                await self._log_safe(slug, "Plugin OAuth callback failed.")
                await self._set_auth_mode(slug, AuthMode.OAUTH.value)
                await self._persist_status(slug, Status.ERROR)
                await self._set_last_error(slug, "Plugin OAuth callback failed.")
            else:
                await self._set_auth_mode(slug, AuthMode.OAUTH.value)
                runtime.status = result.status
                await self._persist_status(slug, result.status)

            # Retain the derived credentials immediately so a later snapshot
            # failure cannot lose their redaction history.
            await self.retain_current_secrets(slug)
            await self._refresh_tools(runtime, record)
            await self._notify_runtime_change(slug, runtime)
            detail = await self.detail(slug)
            if detail is None:
                raise PluginConnectError("not_found", "No such plugin.")
            return detail

    async def disconnect(self, slug: str) -> dict[str, Any]:
        """Run ``on_disconnect``, wipe every secret and reset mode/status.

        Uses the same guards as :meth:`connect`. A failing ``on_disconnect`` is
        logged with its safe message and never propagated (and never leaks plugin
        text); all scoped secrets, including the reserved ``core.`` keys, are
        deleted before the status is set to ``not_connected``.
        """
        if not is_valid_slug(slug):
            raise PluginConnectError("not_found", "No such plugin.")
        self._require_not_pending(slug)
        lock = await self._lock_for(slug)
        async with lock:
            self._require_not_pending(slug)
            record = self._records.get(slug)
            runtime = record.runtime if record is not None else None
            if (
                not self.loading_enabled
                or record is None
                or runtime is None
                or runtime.instance is None
                or record.load_state != LOADED
            ):
                raise PluginConnectError("not_loaded", "The plugin is not loaded.")

            assert self._host is not None  # guarded by loading_enabled
            try:
                await self._host.call(runtime, "on_disconnect")
            except asyncio.CancelledError:
                raise
            except PluginCallError as exc:
                await self._log_safe(slug, exc.safe_message)
            except Exception:
                # Defensive: the host normalizes plugin failures, so this only
                # guards a host-side bug without ever propagating plugin text.
                await self._log_safe(slug, "Plugin disconnect failed.")

            # Seal the retired generation now that ``on_disconnect`` has run, so
            # a timed-out worker from the connected generation cannot recreate a
            # secret or log the credential after the wipe below.
            self._retire_seal(runtime)

            # Capture the current secrets *before* the wipe: a plugin may have
            # cached the credential in memory and echo it in a tool description
            # returned by the post-wipe ``list_tools``, which current-secret
            # redaction alone could no longer cover.
            snapshot = await self._secret_snapshot(slug)
            if snapshot:
                self.retain_secrets(slug, snapshot)

            await self._delete_scanned(f"plugin:{slug}:secret:*")
            runtime.status = Status.NOT_CONNECTED
            await self._set_auth_mode(slug, None)
            await self._persist_status(slug, Status.NOT_CONNECTED)
            await self._refresh_tools(
                runtime,
                record,
                extra_secrets=snapshot or (),
                secrets_known=snapshot is not None,
            )
            await self._notify_runtime_change(slug, runtime)
            detail = await self.detail(slug)
            if detail is None:
                raise PluginConnectError("not_found", "No such plugin.")
            return detail

    async def _meta_auth_mode(self, slug: str) -> str | None:
        mode = (await self._read_meta(slug)).get("auth_mode")
        return mode if isinstance(mode, str) and mode else None

    async def _persist_status(self, slug: str, status: Status) -> None:
        meta = await self._read_meta(slug)
        meta["status"] = status.value if isinstance(status, Status) else "not_connected"
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )

    async def _set_auth_mode(self, slug: str, mode: str | None) -> None:
        """Merge the selected auth mode into the persisted metadata."""
        meta = await self._read_meta(slug)
        meta["auth_mode"] = mode
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )

    async def set_auth_mode(self, slug: str, mode: str | None) -> None:
        """Set the persisted selected auth mode (used by backup restore)."""
        await self._set_auth_mode(slug, mode)

    async def current_secret_strings(self, slug: str) -> tuple[str, ...]:
        """Return the slug's current secret strings (best effort; empty on error)."""
        if self._logs is None:
            return ()
        try:
            return await self._logs.secret_strings(slug)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ()

    def retain_secrets(self, slug: str, secrets: Iterable[str]) -> None:
        """Add secret strings to a generation's redaction history.

        Both the in-memory generation (used for tool-description redaction) and
        the process-wide log redactor are updated, so a value the core learns at
        runtime that is not (or is no longer) in the secret namespace still gets
        redacted from later logs, descriptions and diagnostics.
        """
        values = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
        if not values:
            return
        runtime = self.runtime_for(slug)
        if runtime is not None:
            runtime.retained_secrets.update(values)
        if self._logs is not None:
            self._logs.retain(slug, values)

    async def retain_current_secrets(self, slug: str) -> None:
        """Retain a generation's current secret strings for description redaction.

        Used before a restore wipes the live secrets: a replacement constructor
        may have cached a soon-to-be-deleted credential in memory, and a later
        ``list_tools`` description must still redact it.
        """
        self.retain_secrets(slug, await self.current_secret_strings(slug))

    async def _set_last_error(self, slug: str, message: str | None) -> None:
        """Merge a safe ``last_error`` into the persisted metadata."""
        meta = await self._read_meta(slug)
        meta["last_error"] = message
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )

    async def _log_safe(self, slug: str, message: str) -> None:
        if self._logs is None:
            return
        try:
            await self._logs.write(slug, "error", message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No ``exc_info``: the traceback could carry plugin-supplied text.
            logger.warning(
                "plugin_log_write_failed", extra={"extra_fields": {"slug": slug}}
            )

    async def _notify_runtime_change(
        self, slug: str, runtime: PluginRuntime | None
    ) -> bool:
        """Run the optional runtime-change observer to completion.

        Returns whether the caller was cancelled while waiting. The hook is
        settled (never interrupted mid-flight) so a mount/unmount decision is
        always applied; a caller that must surface cancellation re-raises using
        the returned flag. Called while the stable per-slug lock is held, right
        after a successful load (mount) or when a runtime is being dropped
        (``None``). A failing hook is logged and ignored so plugin lifecycle
        semantics are unaffected, and the observer is ``None`` for store-only
        managers.
        """
        hook = self.on_runtime_change
        if hook is None:
            return False
        return await self._settle_hook(hook(slug, runtime), slug=slug)

    async def _notify_drain(self, slug: str) -> bool:
        """Drain in-flight MCP calls for ``slug`` without retiring it.

        Called before a plugin instance is shut down during a replacement or
        reload, so an in-flight old call completes against a live instance and
        its writes are visible to the replacement snapshot. Returns whether the
        caller was cancelled while waiting (the drain is still settled).
        """
        hook = self.on_runtime_drain
        if hook is None:
            return False
        return await self._settle_hook(
            hook(slug), slug=slug, event="plugin_runtime_drain_failed"
        )

    async def _settle_hook(
        self,
        coro: Awaitable[Any],
        *,
        slug: str,
        event: str = "plugin_runtime_change_failed",
    ) -> bool:
        """Run an observer coroutine to completion, returning the cancel flag."""
        task = asyncio.ensure_future(coro)
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    break
                continue
            except Exception:
                logger.warning(event, extra={"extra_fields": {"slug": slug}})
                break
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                logger.warning(event, extra={"extra_fields": {"slug": slug}})
        return cancelled

    # --- unresolved commit preservation ---------------------------------

    def _owned_pending_path(self, path: Path) -> None:
        if path.parent.resolve() != self._tmp_dir.resolve():
            raise RuntimeError("pending journal path must live directly under tmp")

    def _prepare_pending_dir(
        self,
        pending_path: Path,
        old_zip_bytes: bytes | None,
        backup_dir: Path | None,
        journal: str,
    ) -> None:
        self._owned_pending_path(pending_path)
        pending_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if old_zip_bytes is not None:
            (pending_path / _PENDING_OLD_ZIP).write_bytes(old_zip_bytes)
        if backup_dir is not None and backup_dir.exists():
            self._assert_owned_path(backup_dir)
            dest = pending_path / _PENDING_OLD_DIR
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(backup_dir), str(dest))
        (pending_path / _PENDING_JOURNAL).write_text(journal, encoding="utf-8")

    def _scan_pending(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if not self._tmp_dir.is_dir() or self._tmp_dir.is_symlink():
            return found
        for entry in os.scandir(self._tmp_dir):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not entry.name.startswith(_PENDING_PREFIX):
                continue
            try:
                data = json.loads(
                    (Path(entry.path) / _PENDING_JOURNAL).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                data = {}
            found.append(data if isinstance(data, dict) else {})
        return found

    async def _persist_status_best_effort(self, slug: str, status: Status) -> None:
        try:
            await self._persist_status(slug, status)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Redis may be unavailable (the reason the commit was uncertain).
            logger.warning(
                "plugin_status_persist_failed", extra={"extra_fields": {"slug": slug}}
            )

    async def _enter_pending(
        self,
        slug: str,
        commit_id: str,
        record: PluginRecord,
        *,
        old_version: str | None,
        old_zip_bytes: bytes | None,
        backup_dir: Path | None,
    ) -> None:
        """Preserve old+new code and mark ``slug`` blocked pending reconciliation.

        No secrets are written; only the slug, commit id, old version and the old
        ZIP bytes / extracted dir are retained under a ``pending-`` journal dir.
        """
        pending_path = self._tmp_dir / f"{_PENDING_PREFIX}{uuid.uuid4().hex}"
        journal = json.dumps(
            {"slug": slug, "commit_id": commit_id, "old_version": old_version},
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._run_worker_uncancellable(
            self._prepare_pending_dir, pending_path, old_zip_bytes, backup_dir, journal
        )
        self._pending[slug] = commit_id
        # Keep the loaded candidate visible; connection status is ERROR.
        record.load_state = LOADED
        if record.runtime is not None:
            record.runtime.status = Status.ERROR
        await self._persist_status_best_effort(slug, Status.ERROR)

    def _require_not_pending(self, slug: str) -> None:
        if slug in self._pending:
            raise RuntimeError(
                "Plugin upgrade is pending reconciliation; lifecycle operations are blocked."
            )

    # --- install --------------------------------------------------------

    async def install(
        self, zip_bytes: bytes, *, mode: str = "reject", auto_connect: bool = True
    ) -> PluginRecord:
        """Validate, publish and (when wired) load a package.

        ``reject`` refuses an existing slug. ``replace`` publishes the new
        package transactionally, loads it, and on load failure restores the
        previous package, extraction, metadata and runtime. Data and secrets are
        preserved on success. ``auto_connect=False`` loads without applying the
        auto-connect policy (used by restore, which connects explicitly later).
        """
        if mode not in _INSTALL_MODES:
            raise ValueError(f"unsupported install mode: {mode!r}")

        validated = await asyncio.to_thread(
            validate_package,
            zip_bytes,
            max_size_bytes=self._max_zip_bytes,
            core_version=self._core_version,
        )
        slug = validated.manifest.name
        if not is_valid_slug(slug):
            raise InvalidPluginError("manifest slug is not a valid plugin slug")

        lock = await self._lock_for(slug)
        sem = await self._sem_for(slug)
        async with lock:
            self._require_not_pending(slug)
            package_exists = await asyncio.to_thread(os.path.lexists, self._package_path(slug))
            exists = package_exists or slug in self._records
            if exists and mode == "reject":
                raise PluginExistsError(slug)
            return await self._publish(
                slug, validated, zip_bytes, lock=lock, sem=sem, auto_connect=auto_connect
            )

    async def _publish(
        self,
        slug: str,
        validated: ValidatedPackage,
        zip_bytes: bytes,
        *,
        lock: asyncio.Lock,
        sem: asyncio.Semaphore,
        auto_connect: bool = True,
    ) -> PluginRecord:
        package_path = self._package_path(slug)
        live_dir = self._plugin_dir(slug)

        old_package_bytes: bytes | None = None
        if await asyncio.to_thread(os.path.lexists, package_path):
            old_package_bytes = await asyncio.to_thread(package_path.read_bytes)
        # Snapshot even a stale metadata/membership entry so a cancelled new
        # install does not destroy pre-existing owner state.
        old_meta_raw = await self._redis.get(self._meta_key(slug))
        old_member = slug in await self._redis.smembers(_CORE_PLUGINS_KEY)
        old_record = self._records.get(slug)
        old_runtime = old_record.runtime if old_record is not None else None
        replacing = old_record is not None or old_package_bytes is not None

        stage_package = self._scratch_path("package")
        scratch_dir = self._scratch_path("extract")
        backup_dir = self._scratch_path("old")
        state = _PublicationState()
        committed = False
        preserve = False
        replaced_record = False
        old_unloaded = False
        txn: StorageTransaction | None = None
        record = PluginRecord(
            slug=slug,
            manifest=validated.manifest,
            warnings=self._warnings(validated),
            load_state=LOADING,
            load_error=None,
        )

        try:
            await self._await_worker(
                self._write_staged_package, stage_package, zip_bytes
            )
            await self._await_worker(scratch_dir.mkdir, mode=0o700)
            await self._await_worker(extract_package, validated, scratch_dir)

            # The previous generation shuts down before any new files are
            # published, so its shutdown hook still sees the old extracted code.
            # A caller cancellation observed here settles the shutdown and then
            # aborts into the rollback path (restoring the old runtime) rather
            # than committing the candidate.
            if self.loading_enabled and old_runtime is not None:
                old_unloaded = True
                if old_record is not None:
                    old_record.runtime = None
                    old_record.load_state = UNLOADING
                if await self._forward_unload(old_runtime):
                    raise asyncio.CancelledError()

            # Snapshot the post-shutdown live storage for a detached candidate;
            # candidate constructor/connect writes never touch live Redis.
            if self.loading_enabled and replacing:
                txn = await self._begin_candidate_txn(slug)

            await self._await_worker(
                self._publish_package_state, state, stage_package, package_path
            )
            await self._await_worker(
                self._publish_directory_state,
                state,
                live_dir,
                scratch_dir,
                backup_dir,
            )

            # Mark before awaiting: a cancelled Redis command may already have
            # executed, so rollback must restore the prior value unconditionally.
            state.meta_written = True
            await self._register_metadata(slug, validated.manifest, old_meta_raw)
            state.membership_written = True
            await self._redis.sadd(_CORE_PLUGINS_KEY, slug)

            self._records[slug] = record
            replaced_record = True

            if self.loading_enabled:
                candidate_context = txn.context if txn is not None else None
                loaded = await self._load_runtime(
                    record,
                    lock=lock,
                    sem=sem,
                    context_override=candidate_context,
                    seal_override=txn.seal if txn is not None else None,
                    auto_connect=auto_connect,
                )
                if loaded:
                    if txn is not None:
                        await self._commit_candidate(txn, state)
                elif replacing:
                    # A failed replacement must not publish unloadable code.
                    raise InvalidPluginError(
                        "Plugin upgrade failed; the previous version was restored."
                    )
            committed = True
            return record
        except BaseException as exc:
            if state.commit_settled:
                # The commit point already passed: keep the new runtime and
                # files consistent and propagate (e.g. a late cancellation).
                committed = True
                raise
            if state.commit_uncertain:
                # Never roll back over possibly-live data: preserve old bytes/dir
                # and the loaded candidate, block the slug, fail generically.
                # A caller cancellation is still preserved: the journal/flags are
                # written first, then CancelledError propagates.
                preserve = True
                await self._enter_pending(
                    slug,
                    state.commit_id or "",
                    record,
                    old_version=(
                        old_record.manifest.version
                        if old_record is not None and old_record.manifest is not None
                        else None
                    ),
                    old_zip_bytes=old_package_bytes,
                    backup_dir=backup_dir,
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise PluginUpgradeUncertain(slug) from None
            if record.runtime is not None:
                await self._unload_runtime_safe(record.runtime)
                record.runtime = None
            if txn is not None:
                self._discard_txn(txn)
            await self._rollback_publication(
                slug=slug,
                package_path=package_path,
                live_dir=live_dir,
                backup_dir=backup_dir,
                old_package_bytes=old_package_bytes,
                old_meta_raw=old_meta_raw,
                old_member=old_member,
                state=state,
            )
            if replaced_record:
                if old_record is not None:
                    self._records[slug] = old_record
                else:
                    self._records.pop(slug, None)
            if old_unloaded and old_record is not None:
                await self._run_coro_uncancellable(
                    self._restore_old_runtime(old_record, lock=lock, sem=sem)
                )
            raise
        finally:
            if preserve:
                pass
            elif committed:
                if state.old_dir_moved:
                    await self._await_worker(self._remove_tree, backup_dir)
                await self._await_worker(
                    self._remove_scratch, stage_package, scratch_dir
                )
            else:
                await self._run_worker_uncancellable(
                    self._remove_scratch, stage_package, scratch_dir
                )

    async def _restore_old_runtime(
        self,
        old_record: PluginRecord,
        *,
        lock: asyncio.Lock,
        sem: asyncio.Semaphore,
    ) -> None:
        """Reload the previous package after a rolled-back replacement."""
        old_record.runtime = None
        old_record.tools = []
        old_record.load_state = LOADING
        old_record.load_error = None
        if not self.loading_enabled or old_record.manifest is None:
            old_record.load_state = LOADED
            return
        try:
            await self._load_runtime(old_record, lock=lock, sem=sem)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "failed to restore previous plugin runtime",
                extra={"extra_fields": {"slug": old_record.slug}},
            )
            old_record.load_state = LOAD_FAILED
            old_record.load_error = "The previous plugin version could not be restored."

    @staticmethod
    def _publish_package_state(
        state: _PublicationState, stage_package: Path, package_path: Path
    ) -> None:
        os.replace(stage_package, package_path)
        state.package_published = True

    @staticmethod
    def _publish_directory_state(
        state: _PublicationState,
        live_dir: Path,
        scratch_dir: Path,
        backup_dir: Path,
    ) -> None:
        if os.path.lexists(live_dir):
            os.replace(live_dir, backup_dir)
            state.old_dir_moved = True
        try:
            os.replace(scratch_dir, live_dir)
            state.dir_published = True
        except BaseException:
            if state.old_dir_moved:
                os.replace(backup_dir, live_dir)
                state.old_dir_moved = False
            raise

    async def _rollback_publication(
        self,
        *,
        slug: str,
        package_path: Path,
        live_dir: Path,
        backup_dir: Path,
        old_package_bytes: bytes | None,
        old_meta_raw: str | None,
        old_member: bool,
        state: _PublicationState,
    ) -> None:
        """Restore Redis and filesystem state after a failed/cancelled publish."""
        try:
            await self._run_coro_uncancellable(
                self._restore_redis_registration(
                    slug,
                    old_meta_raw=old_meta_raw,
                    old_member=old_member,
                    state=state,
                )
            )
        except Exception:
            logger.exception(
                "failed to roll back plugin Redis registration",
                extra={"extra_fields": {"slug": slug}},
            )

        try:
            await self._run_worker_uncancellable(
                self._rollback_filesystem,
                state,
                package_path,
                live_dir,
                backup_dir,
                old_package_bytes,
            )
        except Exception:
            logger.exception(
                "failed to roll back plugin filesystem",
                extra={"extra_fields": {"slug": slug}},
            )

    async def _restore_redis_registration(
        self,
        slug: str,
        *,
        old_meta_raw: str | None,
        old_member: bool,
        state: _PublicationState,
    ) -> None:
        if state.meta_written:
            if old_meta_raw is None:
                await self._redis.delete(self._meta_key(slug))
            else:
                await self._redis.set(self._meta_key(slug), old_meta_raw)
        if state.meta_written or state.membership_written:
            if old_member:
                await self._redis.sadd(_CORE_PLUGINS_KEY, slug)
            else:
                await self._redis.srem(_CORE_PLUGINS_KEY, slug)

    async def _restore_meta(self, slug: str, old_meta_raw: str | None) -> None:
        """Restore the metadata snapshot taken before a reload."""
        if old_meta_raw is None:
            await self._redis.delete(self._meta_key(slug))
        else:
            await self._redis.set(self._meta_key(slug), old_meta_raw)

    async def _restore_meta_best_effort(
        self, slug: str, old_meta_raw: str | None
    ) -> None:
        """Restore reload metadata without masking the original failure."""
        try:
            await self._run_coro_uncancellable(
                self._restore_meta(slug, old_meta_raw)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "failed to restore plugin metadata after reload rollback",
                extra={"extra_fields": {"slug": slug}},
                exc_info=True,
            )

    def _rollback_filesystem(
        self,
        state: _PublicationState,
        package_path: Path,
        live_dir: Path,
        backup_dir: Path,
        old_package_bytes: bytes | None,
    ) -> None:
        """Restore the filesystem half of a publish in one worker invocation."""
        if state.dir_published:
            self._remove_tree(live_dir)
            state.dir_published = False
        if state.old_dir_moved:
            os.replace(backup_dir, live_dir)
            state.old_dir_moved = False
        if state.package_published:
            if old_package_bytes is None:
                self._unlink_if_exists(package_path)
            else:
                self._write_atomic_bytes(package_path, old_package_bytes)
            state.package_published = False

    # --- reload ---------------------------------------------------------

    async def reload(self, slug: str) -> bool:
        """Re-extract the stored package, unload the old code and load fresh.

        The previous generation is unloaded *before* re-extraction so its
        shutdown hook still sees the old extracted files, and so a failed reload
        never leaves orphan modules or a half-swapped runtime.
        """
        if not is_valid_slug(slug):
            return False
        self._require_not_pending(slug)

        lock = await self._lock_for(slug)
        sem = await self._sem_for(slug)
        async with lock:
            # Re-assert containment inside the lock: the pre-check may have run
            # while a replacement was still in flight, and that replacement can
            # leave the slug pending before this queued reload acquires the lock.
            self._require_not_pending(slug)
            package_path = self._package_path(slug)
            package_exists = await asyncio.to_thread(os.path.lexists, package_path)
            if not package_exists:
                return False

            record = self._records.get(slug)
            if (
                record is not None
                and record.runtime is not None
                and self._loader is not None
            ):
                old_runtime = record.runtime
                record.runtime = None
                record.load_state = UNLOADING
                if await self._forward_unload(old_runtime):
                    # Caller cancelled during old shutdown: no files changed yet,
                    # so restore the old generation and propagate.
                    await self._run_coro_uncancellable(
                        self._restore_old_runtime(record, lock=lock, sem=sem)
                    )
                    raise asyncio.CancelledError()

            zip_bytes = await asyncio.to_thread(package_path.read_bytes)
            invalid: str | None = None
            validated: ValidatedPackage | None = None
            try:
                validated = await asyncio.to_thread(
                    validate_package,
                    zip_bytes,
                    max_size_bytes=self._max_zip_bytes,
                    core_version=self._core_version,
                )
            except (ManifestError, InvalidPluginError, PackageTooLargeError) as exc:
                invalid = _safe_error(exc)
            if validated is not None and validated.manifest.name != slug:
                invalid = "The package filename does not match the manifest slug."
                validated = None
            if invalid is not None:
                if record is None:
                    record = PluginRecord(slug=slug, manifest=None)
                    self._records[slug] = record
                record.load_state = LOAD_FAILED
                record.load_error = await self._redact_or(
                    slug, invalid, "The stored plugin package is invalid."
                )
                record.tools = []
                record.runtime = None
                await self._persist_status(slug, Status.NOT_CONNECTED)
                # The stored package is now invalid; drop the still-mounted old
                # generation so the endpoint does not keep serving stale tools.
                await self._notify_runtime_change(slug, None)
                return True

            assert validated is not None
            live_dir = self._plugin_dir(slug)
            old_record = record
            old_version = (
                old_record.manifest.version
                if old_record is not None and old_record.manifest is not None
                else None
            )
            old_meta_raw = await self._redis.get(self._meta_key(slug))
            # Keep the pre-reload extraction until the commit decision is known:
            # on ``uncertain`` it moves into the pending journal as ``old_dir``.
            backup_dir = await self._replace_extracted(
                slug, validated, keep_backup=True
            )
            try:
                await self._ensure_meta(validated.manifest)
            except BaseException:
                # Never leave the failed candidate's extraction live with the
                # old record. Put the retained old extraction back (or remove
                # the new one when there was no prior extraction), then restore
                # the metadata snapshot and the old runtime where possible.
                await self._run_worker_uncancellable(
                    self._restore_extraction, live_dir, backup_dir
                )
                await self._restore_meta_best_effort(slug, old_meta_raw)
                if self.loading_enabled and old_record is not None:
                    await self._run_coro_uncancellable(
                        self._restore_old_runtime(old_record, lock=lock, sem=sem)
                    )
                raise

            new_record = PluginRecord(
                slug=slug,
                manifest=validated.manifest,
                warnings=self._warnings(validated),
                load_state=LOADING,
            )
            self._records[slug] = new_record
            if self.loading_enabled:
                txn = await self._begin_candidate_txn(slug)
                state = _PublicationState()
                try:
                    loaded = await self._load_runtime(
                        new_record,
                        lock=lock,
                        sem=sem,
                        context_override=txn.context,
                        seal_override=txn.seal,
                    )
                    if loaded:
                        await self._commit_candidate(txn, state)
                    else:
                        # A failed reload leaves the plugin ``load_failed``; drop
                        # the still-mounted old generation so the endpoint does not
                        # keep serving tools the manager no longer tracks.
                        self._discard_txn(txn)
                        await self._notify_runtime_change(slug, None)
                except BaseException as exc:
                    if state.commit_settled:
                        if backup_dir is not None:
                            await self._await_worker(self._remove_tree, backup_dir)
                        raise
                    if state.commit_uncertain:
                        await self._enter_pending(
                            slug,
                            state.commit_id or "",
                            new_record,
                            old_version=old_version,
                            old_zip_bytes=None,
                            backup_dir=backup_dir,
                        )
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise PluginUpgradeUncertain(slug) from None
                    # Confirmed rollback / pre-write failure / conflict: restore
                    # the pre-reload extraction FIRST, so the old generation is
                    # loaded from the old code and never runs the new candidate
                    # against live storage. Mirror ``_publish``'s ordering.
                    if new_record.runtime is not None:
                        await self._unload_runtime_safe(new_record.runtime)
                        new_record.runtime = None
                    self._discard_txn(txn)
                    restored_old_extraction = bool(
                        await self._run_worker_uncancellable(
                            self._restore_extraction, live_dir, backup_dir
                        )
                    )
                    await self._restore_meta_best_effort(slug, old_meta_raw)
                    if old_record is not None:
                        self._records[slug] = old_record
                        await self._run_coro_uncancellable(
                            self._restore_old_runtime(old_record, lock=lock, sem=sem)
                        )
                    else:
                        new_record.load_state = LOAD_FAILED
                        new_record.load_error = (
                            "The plugin runtime could not be loaded after reload."
                        )
                    if backup_dir is not None and not restored_old_extraction:
                        # The backup was never consumed (it was already gone);
                        # never delete one that was just moved back into place.
                        await self._run_worker_uncancellable(
                            self._remove_tree, backup_dir
                        )
                    raise
            if backup_dir is not None:
                await self._await_worker(self._remove_tree, backup_dir)
            return True

    # --- removal --------------------------------------------------------

    async def remove(self, slug: str) -> bool:
        """Shut down (never disconnect) and remove the selected slug only."""
        if not is_valid_slug(slug):
            return False
        self._require_not_pending(slug)

        lock = await self._lock_for(slug)
        async with lock:
            # Re-assert containment inside the lock: a queued remove must not
            # delete the preserved data/code of a slug that became pending while
            # this call was waiting behind an in-flight replacement.
            self._require_not_pending(slug)
            package_path = self._package_path(slug)
            live_dir = self._plugin_dir(slug)
            package_exists = await asyncio.to_thread(os.path.lexists, package_path)
            if not package_exists and slug not in self._records:
                return False

            record = self._records.get(slug)
            # Always drop the mounted generation for this slug, even when the
            # record carries no live runtime (e.g. a ``load_failed`` record whose
            # old app is still mounted), so MCP stops serving it.
            cancelled = await self._notify_runtime_change(slug, None)
            if record is not None and record.runtime is not None and self._loader is not None:
                # Shut the plugin down (``shutdown`` is called; ``on_disconnect``
                # is not) before the code/state are deleted. A recorded
                # cancellation is surfaced after the teardown completes.
                self._retire_seal(record.runtime)
                await self._loader.unload(record.runtime)
                record.runtime = None
                record.load_state = UNLOADING

            # ``_await_worker`` settles the thread before propagating
            # cancellation, so the stable slug lock is never released while a
            # delete worker can still mutate the package/directory.
            await self._await_worker(self._remove_tree, live_dir)
            await self._await_worker(self._unlink_if_exists, package_path)
            await self._delete_redis_state(slug)
            self._records.pop(slug, None)
            if cancelled:
                raise asyncio.CancelledError()
            return True

    async def shutdown_all(self) -> None:
        """Shut every loaded plugin down under its lifecycle lock.

        The teardown is bounded by one *shared* budget (see
        :attr:`_teardown_timeout`), not one per plugin, so process exit time does
        not grow with the number of installed plugins. A slug that cannot be
        unloaded in time is left for process exit -- the host executor closing
        abandons its worker threads -- and the remaining slugs are skipped rather
        than each paying a fresh timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._teardown_timeout
        for slug in sorted(self._records):
            lock = await self._lock_for(slug)
            async with lock:
                record = self._records.get(slug)
                if (
                    record is None
                    or record.runtime is None
                    or self._loader is None
                ):
                    continue
                try:
                    await self._notify_runtime_change(slug, None)
                    self._retire_seal(record.runtime)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.warning(
                            "plugin_shutdown_budget_exhausted",
                            extra={"extra_fields": {"slug": slug}},
                        )
                        break
                    await asyncio.wait_for(
                        self._loader.unload(record.runtime), remaining
                    )
                except TimeoutError:
                    logger.warning(
                        "plugin_shutdown_budget_exhausted",
                        extra={"extra_fields": {"slug": slug}},
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "plugin_shutdown_failed",
                        extra={"extra_fields": {"slug": slug}},
                    )
                record.runtime = None
                record.load_state = UNLOADING

    async def _delete_redis_state(self, slug: str) -> None:
        await self._redis.srem(_CORE_PLUGINS_KEY, slug)
        await self._redis.delete(
            self._meta_key(slug),
            f"core:logs:{slug}",
        )
        # The trailing delimiter makes this exact to the slug: the pattern for
        # ``foo`` cannot match ``plugin:foobar:...``.
        await self._delete_scanned(f"plugin:{slug}:*")
        await self._delete_oauth_states(slug)

    async def _delete_scanned(self, pattern: str) -> None:
        keys = [key async for key in self._redis.scan_iter(match=pattern)]
        for start in range(0, len(keys), 100):
            batch = keys[start : start + 100]
            if batch:
                await self._redis.delete(*batch)

    async def _delete_oauth_states(self, slug: str) -> None:
        keys = [
            key async for key in self._redis.scan_iter(match=f"{_OAUTH_STATE_PREFIX}*")
        ]
        for key in keys:
            raw = await self._redis.get(key)
            state = _parse_json_object(raw)
            if state is not None and state.get("slug") == slug:
                await self._redis.delete(key)

    # --- accessors ------------------------------------------------------

    def runtime_for(self, slug: str) -> PluginRuntime | None:
        """Return the current runtime for ``slug`` (or ``None``)."""
        record = self._records.get(slug)
        return record.runtime if record is not None else None

    def generation(self, slug: str) -> int | None:
        """Return the current immutable generation id for ``slug``."""
        runtime = self.runtime_for(slug)
        return runtime.generation if runtime is not None else None

    def installed_slugs(self) -> list[str]:
        """Return the sorted slugs currently known to the registry.

        A load-failed package still has a record (it is installed and must
        remain visible/removable), so it is included.
        """
        return sorted(self._records)

    def loaded_runtimes(self) -> list[PluginRuntime]:
        """Return the current loaded runtimes that have a live instance.

        Sorted by slug for deterministic scheduling. The caller still has to
        check the persisted connection status before pinging.
        """
        runtimes: list[PluginRuntime] = []
        for slug in sorted(self._records):
            record = self._records[slug]
            runtime = record.runtime
            if (
                record.load_state == LOADED
                and runtime is not None
                and runtime.instance is not None
            ):
                runtimes.append(runtime)
        return runtimes

    async def read_health(self, slug: str) -> dict[str, Any]:
        """Return the persisted health metadata for ``slug``.

        Missing or malformed metadata is normalized to the empty-meta shape
        (``not_connected``, zero counts, no ping/error).
        """
        return await self._read_meta(slug)

    async def apply_health(
        self,
        slug: str,
        *,
        status: Status | str,
        fail_count: int,
        transient_error_count: int,
        last_ping_at: str | None,
        last_error: str | None,
    ) -> None:
        """Merge one health observation into persisted metadata and the runtime.

        The persisted key keeps the existing ``core:plugin:{slug}:meta`` shape,
        preserving unrelated fields (version, installed_at, auth_mode). The
        in-memory runtime, when it is still the current generation, mirrors the
        same status/counts so later manager operations observe consistent state.
        A slug that is no longer registered is ignored rather than recreated.
        """
        if not is_valid_slug(slug) or slug not in self._records:
            return
        status_value = status.value if isinstance(status, Status) else str(status)
        fail_value = max(0, fail_count)
        transient_value = max(0, transient_error_count)
        meta = await self._read_meta(slug)
        meta["status"] = status_value
        meta["fail_count"] = fail_value
        meta["transient_error_count"] = transient_value
        meta["last_ping_at"] = last_ping_at
        meta["last_error"] = last_error
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )
        record = self._records.get(slug)
        runtime = record.runtime if record is not None else None
        if runtime is not None:
            with contextlib.suppress(ValueError):
                runtime.status = Status(status_value)
            runtime.fail_count = fail_value
            runtime.transient_error_count = transient_value

    async def log_health(
        self, slug: str, message: str, *, level: str = "info"
    ) -> None:
        """Append a health transition/failure line to the plugin's own log."""
        if self._logs is None:
            return
        try:
            await self._logs.write(slug, level, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "plugin_health_log_write_failed",
                extra={"extra_fields": {"slug": slug}},
            )

    def package_path(self, slug: str) -> Path:
        """Return the on-disk package path for ``slug``.

        Raises :class:`ValueError` for an invalid slug; the returned path is not
        guaranteed to exist.
        """
        return self._package_path(slug)

    def plugin_dir(self, slug: str) -> Path:
        """Return the extracted plugin directory for ``slug``.

        Raises :class:`ValueError` for an invalid slug; the returned path is not
        guaranteed to exist.
        """
        return self._plugin_dir(slug)

    async def lifecycle_lock(self, slug: str) -> asyncio.Lock:
        """Return the stable per-slug lifecycle lock, creating it if needed.

        Callers that only read plugin files/state may hold this lock so a
        snapshot cannot interleave with a lifecycle operation. It must not be
        passed to a method that also acquires it (``install``/``remove``/
        ``connect``/``reload``/``disconnect``); those lock internally.
        """
        return await self._lock_for(slug)

    # --- read models ----------------------------------------------------

    async def summaries(self) -> list[dict[str, Any]]:
        """Return dashboard list rows, sorted by slug."""
        return [await self._summary(record) for record in self._sorted_records()]

    async def detail(self, slug: str) -> dict[str, Any] | None:
        """Return dashboard detail for a known slug, or ``None`` when unknown."""
        if not is_valid_slug(slug):
            return None
        record = self._records.get(slug)
        if record is None:
            return None

        meta = await self._read_meta(slug)
        manifest = record.manifest.to_dict() if record.manifest is not None else None
        return {
            "slug": slug,
            "manifest": manifest,
            "load_state": record.load_state,
            "load_error": await self._redact(slug, record.load_error),
            "status": meta.get("status") or "not_connected",
            "auth_methods": self._auth_modes(record),
            "auth_mode": meta.get("auth_mode"),
            "tools": [dict(tool) for tool in record.tools],
            "secret_names": await self._secret_names(slug),
            "callback_url": self._callback_url(slug),
            "endpoint": self._endpoint_url(slug),
            "endpoint_url": self._endpoint_url(slug),
            "mcp_endpoint_url": self._endpoint_url(slug),
            "warnings": list(record.warnings),
            "logs": await self.logs(slug, _LOG_LIMIT) or [],
            "installed_at": meta.get("installed_at"),
            "last_ping_at": meta.get("last_ping_at"),
            "last_error": await self._redact(slug, meta.get("last_error")),
        }

    async def _summary(self, record: PluginRecord) -> dict[str, Any]:
        meta = await self._read_meta(record.slug)
        manifest = record.manifest
        display_name = manifest.display_name if manifest is not None else record.slug
        return {
            "slug": record.slug,
            "name": display_name,
            "display_name": display_name,
            "version": manifest.version if manifest is not None else None,
            "description": manifest.description if manifest is not None else "",
            "auth_methods": self._auth_modes(record),
            "auth_modes": self._auth_modes(record),
            "auth_mode": meta.get("auth_mode"),
            "status": meta.get("status") or "not_connected",
            "load_state": record.load_state,
            "load_error": await self._redact(record.slug, record.load_error),
            "tool_names": [
                str(tool.get("name")) for tool in record.tools if tool.get("name")
            ],
            "warnings": list(record.warnings),
            "last_ping_at": meta.get("last_ping_at"),
            "last_error": await self._redact(record.slug, meta.get("last_error")),
        }

    async def logs(self, slug: str, limit: int = _LOG_LIMIT) -> list[dict[str, Any]] | None:
        """Return up to ``limit`` newest log entries for a known slug."""
        if not is_valid_slug(slug) or slug not in self._records:
            return None
        if self._logs is not None:
            return await self._logs.recent(slug, limit)
        return await self._recent_logs(slug, limit)

    def _sorted_records(self) -> list[PluginRecord]:
        return [self._records[slug] for slug in sorted(self._records)]

    @staticmethod
    def _auth_modes(record: PluginRecord) -> list[str]:
        if record.manifest is None:
            return []
        return [mode.value for mode in record.manifest.auth_modes]

    @staticmethod
    def _warnings(validated: ValidatedPackage) -> tuple[str, ...]:
        return tuple(str(warning) for warning in validated.warnings)

    async def _read_meta(self, slug: str) -> dict[str, Any]:
        raw = await self._redis.get(self._meta_key(slug))
        parsed = _parse_json_object(raw)
        if parsed is None:
            return dict(_EMPTY_META)
        merged = dict(_EMPTY_META)
        merged.update(parsed)
        return merged

    async def _ensure_meta(self, manifest: Manifest) -> None:
        slug = manifest.name
        raw = await self._redis.get(self._meta_key(slug))
        existing = _parse_json_object(raw)
        meta = self._build_meta(manifest, existing)
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )

    async def _register_metadata(
        self,
        slug: str,
        manifest: Manifest,
        old_meta_raw: str | None,
    ) -> None:
        existing = _parse_json_object(old_meta_raw)
        meta = self._build_meta(manifest, existing)
        await self._redis.set(
            self._meta_key(slug),
            json.dumps(meta, separators=(",", ":"), sort_keys=True),
        )

    @staticmethod
    def _build_meta(
        manifest: Manifest, existing: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        source: Mapping[str, Any] = existing or {}
        installed_at = source.get("installed_at")
        auth_mode = source.get("auth_mode")
        status = source.get("status")
        fail_count = source.get("fail_count")
        transient_count = source.get("transient_error_count")
        last_ping_at = source.get("last_ping_at")
        last_error = source.get("last_error")
        return {
            "version": manifest.version,
            "installed_at": (
                installed_at
                if isinstance(installed_at, str) and installed_at
                else _utc_now()
            ),
            "auth_mode": auth_mode if isinstance(auth_mode, str) else None,
            "status": status if isinstance(status, str) and status else "not_connected",
            "fail_count": (
                fail_count
                if isinstance(fail_count, int) and not isinstance(fail_count, bool)
                else 0
            ),
            "transient_error_count": (
                transient_count
                if isinstance(transient_count, int)
                and not isinstance(transient_count, bool)
                else 0
            ),
            "last_ping_at": last_ping_at if isinstance(last_ping_at, str) else None,
            "last_error": last_error if isinstance(last_error, str) else None,
        }

    async def _secret_names(self, slug: str) -> list[str]:
        if self._storage is not None:
            try:
                return await self._storage.secret_names(slug)
            except Exception:
                logger.warning(
                    "plugin secret names could not be listed",
                    extra={"extra_fields": {"slug": slug}},
                )
        prefix = f"plugin:{slug}:secret:"
        names: list[str] = []
        async for key in self._redis.scan_iter(match=f"{prefix}*"):
            if key.startswith(prefix):
                names.append(key[len(prefix) :])
        return sorted(names)

    async def _recent_logs(self, slug: str, limit: int = _LOG_LIMIT) -> list[dict[str, Any]]:
        raw_entries = await self._redis.lrange(
            f"core:logs:{slug}", 0, max(0, limit) - 1
        )
        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = {"message": str(raw)}
            entries.append(parsed if isinstance(parsed, dict) else {"message": str(raw)})
        return entries

    async def _redact(self, slug: str, message: Any) -> str | None:
        """Return a redacted message, or ``None`` when redaction is unavailable.

        Fails closed: if the redactor raises, the caller receives ``None``
        rather than the original (possibly secret-bearing) text.
        """
        if not isinstance(message, str) or not message:
            return None
        if self._logs is None:
            return message
        try:
            return await self._logs.redact(slug, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _redact_or(self, slug: str, message: str, generic: str) -> str:
        """Return a redacted message, falling back to a safe generic string."""
        redacted = await self._redact(slug, message)
        return redacted if redacted is not None else generic

    async def _redaction_secrets(
        self,
        slug: str,
        *,
        extra_secrets: tuple[str, ...] = (),
        secrets_known: bool = True,
    ) -> tuple[str, ...] | None:
        """Return the secret set to redact a listing against, or ``None``.

        ``None`` means redaction is unavailable -- no redactor is configured or
        the pre-wipe snapshot could not be read (``secrets_known=False``) -- so
        the caller must fail closed. Covers the slug's current secrets plus the
        retained ones and any supplied pre-wipe snapshot, so a credential a
        plugin cached in memory before the stored secret was deleted is still
        redacted.
        """
        if self._logs is None or not secrets_known:
            return None
        try:
            current = await self._logs.redaction_strings(slug)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return tuple(dict.fromkeys((*extra_secrets, *current)))

    async def _secret_snapshot(self, slug: str) -> tuple[str, ...] | None:
        """Return the slug's current string secrets, or ``None`` when unreadable."""
        if self._logs is None:
            return None
        try:
            return await self._logs.secret_strings(slug)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _sync_membership(self) -> None:
        # A load-failed package is still installed (it has a package file and
        # must remain visible/removable), so every discovered record is a
        # member. Clearing the set when no packages remain keeps Redis from
        # retaining stale slugs after external package removal.
        installed = set(self._records)
        current = await self._redis.smembers(_CORE_PLUGINS_KEY)
        for stale in sorted(current - installed):
            await self._redis.srem(_CORE_PLUGINS_KEY, stale)
        for slug in sorted(installed):
            await self._redis.sadd(_CORE_PLUGINS_KEY, slug)

    # --- paths and filesystem helpers ----------------------------------

    def _require_slug(self, slug: str) -> str:
        if not is_valid_slug(slug):
            raise ValueError("invalid plugin slug")
        return slug

    def _package_path(self, slug: str) -> Path:
        return self._packages_dir / f"{self._require_slug(slug)}.zip"

    def _plugin_dir(self, slug: str) -> Path:
        return self._plugins_dir / self._require_slug(slug)

    def _meta_key(self, slug: str) -> str:
        return f"core:plugin:{self._require_slug(slug)}:meta"

    def _callback_url(self, slug: str) -> str:
        return f"{self._base_url}/mcp/{self._require_slug(slug)}/oauth/callback"

    def _endpoint_url(self, slug: str) -> str:
        return f"{self._base_url}/mcp/{self._require_slug(slug)}/"

    def _scratch_path(self, kind: str) -> Path:
        return self._tmp_dir / f"{kind}-{uuid.uuid4().hex}"

    def _ensure_directories(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError("plugin data directory could not be created") from exc

        owned = (
            ("packages", self._packages_dir),
            ("plugins", self._plugins_dir),
            ("tmp", self._tmp_dir),
        )
        for name, path in owned:
            if path.is_symlink():
                raise RuntimeError(
                    f"plugin {name} directory must not be a symlink"
                )
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"plugin {name} directory could not be created"
                ) from exc
            if not path.is_dir():
                raise RuntimeError(f"plugin {name} path is not a directory")

        try:
            data_real = self._data_dir.resolve()
            resolved = {name: path.resolve() for name, path in owned}
        except OSError as exc:
            raise RuntimeError("plugin data directories could not be resolved") from exc
        for name, real in resolved.items():
            if real.parent != data_real or real.name != name:
                raise RuntimeError(
                    f"plugin {name} directory escapes the owned data directory"
                )

    def _cleanup_tmp(self) -> None:
        """Remove only scratch entries owned by this manager."""
        if not self._tmp_dir.is_dir() or self._tmp_dir.is_symlink():
            raise RuntimeError("plugin scratch directory is missing or unsafe")
        try:
            resolved_tmp = self._tmp_dir.resolve()
            resolved_data = self._data_dir.resolve()
        except OSError as exc:
            raise RuntimeError("plugin scratch directory could not be resolved") from exc
        if resolved_tmp.parent != resolved_data or resolved_tmp.name != "tmp":
            raise RuntimeError("plugin scratch directory is outside the owned data directory")

        for entry in os.scandir(self._tmp_dir):
            if _OWNED_TMP_NAME.fullmatch(entry.name) is None:
                continue
            path = Path(entry.path)
            self._assert_owned_path(path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(path)
                elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    os.unlink(path)
            except OSError:
                logger.warning(
                    "could not remove plugin scratch entry",
                    extra={"extra_fields": {"entry": entry.name}},
                    exc_info=True,
                )

    def _list_package_files(self) -> list[Path]:
        try:
            entries = list(os.scandir(self._packages_dir))
        except OSError:
            return []
        paths: list[Path] = []
        for entry in entries:
            if not entry.name.endswith(".zip"):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            paths.append(Path(entry.path))
        return sorted(paths)

    async def _replace_extracted(
        self,
        slug: str,
        validated: ValidatedPackage,
        *,
        keep_backup: bool = False,
    ) -> Path | None:
        live_dir = self._plugin_dir(slug)
        scratch_dir = self._scratch_path("extract")
        backup_dir = self._scratch_path("old")
        await self._await_worker(scratch_dir.mkdir, mode=0o700)
        try:
            await self._await_worker(extract_package, validated, scratch_dir)
            moved_old = await self._await_worker(
                self._publish_directory, live_dir, scratch_dir, backup_dir
            )
        except BaseException:
            # The worker has settled before cancellation propagates; clean up
            # scratch without allowing another worker to outlive the slug lock.
            await self._run_worker_uncancellable(self._remove_tree, scratch_dir)
            raise
        if keep_backup and moved_old:
            # The caller owns the old extraction until its commit decision.
            return backup_dir
        await self._await_worker(self._remove_tree, backup_dir)
        return None

    def _restore_extraction(self, live_dir: Path, backup_dir: Path | None) -> bool:
        """Undo a reload's extraction swap without touching a consumed backup.

        Removes the failed candidate's extraction from ``live_dir`` and, when a
        retained old extraction exists, moves it back. Returns whether the old
        extraction was restored so callers never delete one already moved.
        """
        self._assert_owned_path(live_dir)
        self._remove_tree(live_dir)
        if backup_dir is None or not backup_dir.exists():
            return False
        self._assert_owned_path(backup_dir)
        os.replace(backup_dir, live_dir)
        return True

    @staticmethod
    def _publish_directory(live_dir: Path, scratch_dir: Path, backup_dir: Path) -> bool:
        """Move the fresh extraction into place, restoring the old one on failure."""
        moved_old = False
        if os.path.lexists(live_dir):
            os.replace(live_dir, backup_dir)
            moved_old = True
        try:
            os.replace(scratch_dir, live_dir)
        except Exception:
            if moved_old:
                os.replace(backup_dir, live_dir)
            raise
        return moved_old

    def _assert_owned_path(self, path: Path) -> None:
        """Refuse to mutate anything outside the three owned data children."""
        parent = path.parent
        allowed = (self._packages_dir, self._plugins_dir, self._tmp_dir)
        if parent.is_symlink() or parent.resolve() not in {
            item.resolve() for item in allowed
        }:
            raise RuntimeError("refusing to modify a path outside owned plugin directories")

    def _write_staged_package(self, path: Path, data: bytes) -> None:
        self._assert_owned_path(path)
        path.write_bytes(data)

    def _write_atomic_bytes(self, destination: Path, data: bytes) -> None:
        self._assert_owned_path(destination)
        temp = self._scratch_path("package")
        try:
            self._assert_owned_path(temp)
            temp.write_bytes(data)
            os.replace(temp, destination)
        finally:
            self._unlink_if_exists(temp)

    def _unlink_if_exists(self, path: Path) -> None:
        self._assert_owned_path(path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return

    def _remove_tree(self, path: Path) -> None:
        """Remove one owned package/directory without following symlinks."""
        self._assert_owned_path(path)
        if path.is_symlink():
            os.unlink(path)
            return
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            os.unlink(path)

    def _remove_scratch(self, *paths: Path) -> None:
        for path in paths:
            try:
                self._assert_owned_path(path)
                if path.is_symlink():
                    os.unlink(path)
                elif path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    os.unlink(path)
            except OSError:
                logger.warning(
                    "could not remove plugin scratch path",
                    extra={"extra_fields": {"path": str(path)}},
                    exc_info=True,
                )


__all__ = [
    "LOADED",
    "LOADING",
    "LOAD_FAILED",
    "SLUG_PATTERN",
    "UNLOADING",
    "PluginConnectError",
    "PluginExistsError",
    "PluginManager",
    "PluginRecord",
    "RuntimeChangeHook",
    "RuntimeDrainHook",
    "is_valid_slug",
    "tool_to_dict",
]
