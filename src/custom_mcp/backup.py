"""Validated backup export and restore for installed plugins.

A backup is a deflated ZIP built into a :class:`tempfile.SpooledTemporaryFile`
and streamed to the client. It contains ``backup.json`` plus, for every plugin,
``plugins/{slug}/plugin.zip`` (byte-identical to the stored package),
``plugins/{slug}/data.json`` and ``plugins/{slug}/secrets.json`` (flat
``{key: value}`` objects of decoded JSON values) and ``plugins/{slug}/meta.json``
(``{"auth_mode": <str|null>, "version": <str>}``).

**The archive is plaintext.** ``backup.json`` records ``"encrypted": false`` and
every credential is written as readable JSON, so the dashboard warns the owner
to treat a backup exactly like a password. Encrypted archives are never produced
and an archive declaring ``encrypted: true`` is rejected on restore.

Restore is two phase. Phase A (:func:`validate_archive`) validates the complete
outer archive -- ``backup.json``, every listed package (through
:func:`~custom_mcp.plugins.packaging.validate_package`) and every JSON key/value
schema -- without touching live disk or Redis. Phase B
(:meth:`RestoreService.restore`) then applies each plugin independently using the
manager's install/remove/connect operations (each serialized on the plugin's
lifecycle lock): ``skip`` an existing plugin or wipe and reinstall it
(``replace``), restore data and secrets with **no** TTL, call the optional
``on_restore`` hook (non-fatal on failure), then reconnect with the stored auth
mode and persist the resulting status.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from custom_mcp.logging import get_logger
from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.manager import (
    LOAD_FAILED,
    PluginConnectError,
    PluginManager,
    is_valid_slug,
)
from custom_mcp.plugins.manifest import ManifestError
from custom_mcp.plugins.packaging import (
    InvalidPluginError,
    PackageTooLargeError,
    validate_package,
)
from custom_mcp.storage.manager import (
    DATA_NAMESPACE,
    KEY_PATTERN,
    SECRET_NAMESPACE,
    InvalidValueError,
    StorageDecodeError,
    StorageManager,
    decode_value,
    encode_value,
)
from custom_mcp.storage.redis_client import SyncRedis

logger = get_logger("backup")

#: The only backup format version this core understands.
FORMAT_VERSION = 1
#: Name of the archive's top-level descriptor.
BACKUP_FILENAME = "backup.json"
#: Streaming chunk size for the response body.
STREAM_CHUNK_BYTES = 64 * 1024
#: Spool threshold before the archive spills to a temporary file on disk.
_SPOOL_MAX_BYTES = 8 * 1024 * 1024
_RESTORE_MODES = frozenset({"skip", "replace"})
_RECONNECT_FAILED = "The stored connection could not be re-established."
_LOAD_FAILED_DETAIL = "The plugin failed to load."


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


class BackupError(Exception):
    """Base class for backup/restore domain failures."""


class InvalidBackupError(BackupError):
    """A backup archive failed validation; ``message`` is client-safe."""


class UnknownPluginError(BackupError):
    """A requested slug is not present where it was looked up."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"unknown plugin slug: {slug!r}")


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One plugin captured for export."""

    slug: str
    version: str
    auth_mode: str | None
    package_bytes: bytes
    data: dict[str, Any]
    secrets: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArchivePlugin:
    """One fully validated plugin parsed from a backup archive."""

    slug: str
    version: str
    auth_mode: str | None
    package_bytes: bytes
    data: dict[str, Any]
    secrets: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """The outcome of applying one plugin during restore."""

    slug: str
    outcome: str
    status: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "outcome": self.outcome,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """The per-plugin outcome of a restore."""

    format_version: int
    results: tuple[RestoreResult, ...]

    @property
    def restored(self) -> list[str]:
        return [r.slug for r in self.results if r.outcome == "restored"]

    @property
    def skipped(self) -> list[str]:
        return [r.slug for r in self.results if r.outcome == "skipped"]

    @property
    def failed(self) -> list[str]:
        return [r.slug for r in self.results if r.outcome == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "results": [r.to_dict() for r in self.results],
            "restored": self.restored,
            "skipped": self.skipped,
            "failed": self.failed,
        }


# ---------------------------------------------------------------------------
# Parsing helpers (Phase A)
# ---------------------------------------------------------------------------


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def _load_json_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidBackupError(f"{name} is not valid UTF-8.") from exc
    try:
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise InvalidBackupError(f"{name} is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise InvalidBackupError(f"{name} must be a JSON object.")
    return parsed


def _load_flat_object(raw: bytes, name: str) -> dict[str, Any]:
    """Decode a flat storage object, validating every key and value."""
    obj = _load_json_object(raw, name)
    result: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise InvalidBackupError(f"{name} contains an invalid storage key.")
        try:
            encode_value(value)
        except InvalidValueError as exc:
            raise InvalidBackupError(f"{name} contains an invalid storage value.") from exc
        result[key] = value
    return result


def _parse_backup_descriptor(raw: bytes) -> list[dict[str, Any]]:
    descriptor = _load_json_object(raw, BACKUP_FILENAME)
    if descriptor.get("format_version") != FORMAT_VERSION:
        raise InvalidBackupError("The backup format version is not supported.")
    encrypted = descriptor.get("encrypted")
    if encrypted is True:
        raise InvalidBackupError("Encrypted backups are not supported.")
    if encrypted is not False:
        raise InvalidBackupError(f"{BACKUP_FILENAME} is missing the encryption marker.")
    plugins = descriptor.get("plugins")
    if not isinstance(plugins, list):
        raise InvalidBackupError(f"{BACKUP_FILENAME} has an invalid plugin list.")
    seen: set[str] = set()
    for entry in plugins:
        if not isinstance(entry, dict):
            raise InvalidBackupError(f"{BACKUP_FILENAME} has an invalid plugin entry.")
        slug = entry.get("slug")
        version = entry.get("version")
        has_secrets = entry.get("has_secrets")
        if not is_valid_slug(slug):
            raise InvalidBackupError(f"{BACKUP_FILENAME} has an invalid plugin slug.")
        assert isinstance(slug, str)
        if not isinstance(version, str) or not version:
            raise InvalidBackupError(f"{BACKUP_FILENAME} has an invalid plugin version.")
        if not isinstance(has_secrets, bool):
            raise InvalidBackupError(f"{BACKUP_FILENAME} has an invalid plugin entry.")
        if slug in seen:
            raise InvalidBackupError(f"{BACKUP_FILENAME} contains a duplicate plugin.")
        seen.add(slug)
    return plugins


def _read_entry(archive: zipfile.ZipFile, name: str, slug: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as exc:
        raise InvalidBackupError(
            f"The backup is missing a required file for {slug!r}."
        ) from exc
    except _CORRUPT_ENTRY_ERRORS as exc:
        raise InvalidBackupError("The backup contains a corrupt entry.") from exc


#: Errors a corrupt or unreadable ZIP member can raise while being decompressed.
_CORRUPT_ENTRY_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    NotImplementedError,
    zlib.error,
    EOFError,
    OSError,
    ValueError,
)


def _validate_members(
    archive: zipfile.ZipFile, descriptor: list[dict[str, Any]]
) -> None:
    """Verify every outer member before any parsing: no duplicates, no unexpected
    entries, and a clean CRC/decompress for each (so a corrupt trailing entry can
    never be silently ignored)."""
    names = [info.filename for info in archive.infolist()]
    if len(names) != len(set(names)):
        raise InvalidBackupError("The backup contains duplicate entries.")
    allowed = {BACKUP_FILENAME}
    for entry in descriptor:
        slug = entry["slug"]
        for leaf in ("plugin.zip", "data.json", "secrets.json", "meta.json"):
            allowed.add(f"plugins/{slug}/{leaf}")
    for name in names:
        if name not in allowed:
            raise InvalidBackupError("The backup contains an unexpected entry.")
        try:
            archive.read(name)
        except _CORRUPT_ENTRY_ERRORS as exc:
            raise InvalidBackupError("The backup contains a corrupt entry.") from exc


def _read_plugin(
    archive: zipfile.ZipFile,
    entry: dict[str, Any],
    *,
    max_plugin_zip_bytes: int,
    core_version: str,
) -> ArchivePlugin:
    slug = entry["slug"]
    assert isinstance(slug, str)
    base = f"plugins/{slug}"
    package_bytes = _read_entry(archive, f"{base}/plugin.zip", slug)
    data = _load_flat_object(
        _read_entry(archive, f"{base}/data.json", slug), f"{base}/data.json"
    )
    secrets = _load_flat_object(
        _read_entry(archive, f"{base}/secrets.json", slug), f"{base}/secrets.json"
    )
    try:
        validated = validate_package(
            package_bytes,
            max_size_bytes=max_plugin_zip_bytes,
            core_version=core_version,
        )
    except (InvalidPluginError, PackageTooLargeError, ManifestError) as exc:
        raise InvalidBackupError(
            f"The backup package for {slug!r} is invalid."
        ) from exc
    if validated.manifest.name != slug:
        raise InvalidBackupError(
            f"The backup package for {slug!r} does not match its slug."
        )

    meta = _load_json_object(
        _read_entry(archive, f"{base}/meta.json", slug), f"{base}/meta.json"
    )
    auth_mode = meta.get("auth_mode")
    if auth_mode is not None and not isinstance(auth_mode, str):
        raise InvalidBackupError(f"The backup metadata for {slug!r} is invalid.")
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        raise InvalidBackupError(f"The backup metadata for {slug!r} is invalid.")
    if version != entry.get("version") or version != validated.manifest.version:
        raise InvalidBackupError(f"The backup version for {slug!r} is inconsistent.")

    return ArchivePlugin(
        slug=slug,
        version=version,
        auth_mode=auth_mode,
        package_bytes=package_bytes,
        data=data,
        secrets=secrets,
    )


def validate_archive(
    data: bytes,
    *,
    max_plugin_zip_bytes: int,
    core_version: str,
) -> tuple[ArchivePlugin, ...]:
    """Validate a complete backup archive without modifying any live state.

    Raises :class:`InvalidBackupError` for any defect (not a ZIP, bad
    ``backup.json``, ``encrypted: true``, a missing/invalid package, JSON or
    storage-schema violation). The returned plugins preserve archive order.
    """
    if not data:
        raise InvalidBackupError("The backup file is empty.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        ValueError,
        EOFError,
        OSError,
    ) as exc:
        raise InvalidBackupError("The backup is not a valid ZIP archive.") from exc
    with archive:
        descriptor = _parse_backup_descriptor(
            _read_entry(archive, BACKUP_FILENAME, BACKUP_FILENAME)
        )
        # Validate the *complete* archive before parsing anything: every member
        # must be expected, unique and integrity-clean.
        _validate_members(archive, descriptor)
        plugins = tuple(
            _read_plugin(
                archive,
                entry,
                max_plugin_zip_bytes=max_plugin_zip_bytes,
                core_version=core_version,
            )
            for entry in descriptor
        )
    return plugins


# ---------------------------------------------------------------------------
# Export (Phase A snapshot + streaming build)
# ---------------------------------------------------------------------------


def _dump_object(values: dict[str, Any]) -> str:
    return json.dumps(values, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


class BackupService:
    """Build a streamed backup archive from the live plugin registry."""

    def __init__(self, manager: PluginManager, redis_sync: SyncRedis) -> None:
        self._manager = manager
        self._redis = redis_sync

    async def build(
        self, slugs: Iterable[str] | None = None
    ) -> tempfile.SpooledTemporaryFile[bytes]:
        """Snapshot the selected plugins and return a seeked archive stream.

        ``None`` selects every installed plugin. An unknown or invalid requested
        slug raises :class:`UnknownPluginError` without producing an archive.
        """
        selected = self._select(slugs)
        snapshots = [await self._snapshot(slug) for slug in selected]

        # The returned spool is owned (and closed) by the streaming caller.
        spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=_SPOOL_MAX_BYTES, mode="w+b"
        )
        try:
            with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    BACKUP_FILENAME,
                    json.dumps(
                        {
                            "format_version": FORMAT_VERSION,
                            "created_at": _utc_now(),
                            "core_version": self._manager.core_version,
                            "encrypted": False,
                            "plugins": [
                                {
                                    "slug": snapshot.slug,
                                    "version": snapshot.version,
                                    "has_secrets": bool(snapshot.secrets),
                                }
                                for snapshot in snapshots
                            ],
                        },
                        separators=(",", ":"),
                    ),
                )
                for snapshot in snapshots:
                    base = f"plugins/{snapshot.slug}"
                    archive.writestr(f"{base}/plugin.zip", snapshot.package_bytes)
                    archive.writestr(f"{base}/data.json", _dump_object(snapshot.data))
                    archive.writestr(
                        f"{base}/secrets.json", _dump_object(snapshot.secrets)
                    )
                    archive.writestr(
                        f"{base}/meta.json",
                        json.dumps(
                            {
                                "auth_mode": snapshot.auth_mode,
                                "version": snapshot.version,
                            },
                            separators=(",", ":"),
                        ),
                    )
        except BaseException:
            spool.close()
            raise
        spool.seek(0)
        return spool

    def _select(self, slugs: Iterable[str] | None) -> list[str]:
        installed = set(self._manager.installed_slugs())
        if slugs is None:
            return sorted(installed)
        chosen: list[str] = []
        for raw in slugs:
            if not is_valid_slug(raw) or raw not in installed:
                raise UnknownPluginError(str(raw))
            if raw not in chosen:
                chosen.append(raw)
        return chosen

    async def _snapshot(self, slug: str) -> _Snapshot:
        lock = await self._manager.lifecycle_lock(slug)
        async with lock:
            package_bytes = await asyncio.to_thread(
                self._manager.package_path(slug).read_bytes
            )
            detail = await self._manager.detail(slug)
            version: Any = None
            auth_mode: Any = None
            if detail is not None:
                manifest = detail.get("manifest")
                if isinstance(manifest, dict):
                    version = manifest.get("version")
                auth_mode = detail.get("auth_mode")
            data, secrets = await asyncio.to_thread(self._snapshot_state, slug)
        if not isinstance(version, str) or not version:
            raise BackupError(f"plugin {slug!r} has no installed version to back up")
        return _Snapshot(
            slug=slug,
            version=version,
            auth_mode=auth_mode if isinstance(auth_mode, str) else None,
            package_bytes=package_bytes,
            data=data,
            secrets=secrets,
        )

    def _snapshot_state(self, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            self._snapshot_namespace(slug, DATA_NAMESPACE),
            self._snapshot_namespace(slug, SECRET_NAMESPACE),
        )

    def _snapshot_namespace(self, slug: str, namespace: str) -> dict[str, Any]:
        """Return non-expiring decoded values for one slug namespace."""
        prefix = f"plugin:{slug}:{namespace}:"
        values: dict[str, Any] = {}
        for full in self._redis.scan_iter(match=prefix + "*"):
            if not full.startswith(prefix):
                continue
            raw = self._redis.get(full)
            if raw is None:
                continue
            pttl = self._redis.pttl(full)
            if pttl == -2:
                # The key expired between the read and the TTL probe.
                continue
            if pttl > -1:
                # A live TTL makes the key transient and therefore excluded.
                continue
            try:
                values[full[len(prefix) :]] = decode_value(raw)
            except StorageDecodeError as exc:
                raise BackupError(
                    "plugin data could not be serialized for backup"
                ) from exc
        return values


# ---------------------------------------------------------------------------
# Restore (Phase B apply)
# ---------------------------------------------------------------------------


def _credentials_for(mode: str, secrets: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the connect credentials the stored auth mode needs."""
    if mode == "api_key":
        api_key = secrets.get("core.api_key")
        if isinstance(api_key, str) and api_key:
            return {"api_key": api_key}
        return {}
    if mode == "oauth":
        credentials: dict[str, Any] = {}
        client_id = secrets.get("core.oauth_client_id")
        if isinstance(client_id, str) and client_id:
            credentials["client_id"] = client_id
        client_secret = secrets.get("core.oauth_client_secret")
        if isinstance(client_secret, str) and client_secret:
            credentials["client_secret"] = client_secret
        return credentials
    return {}


class RestoreService:
    """Validate a backup then apply each plugin independently."""

    def __init__(
        self,
        manager: PluginManager,
        storage: StorageManager,
        host: PluginHost,
    ) -> None:
        self._manager = manager
        self._storage = storage
        self._host = host

    async def restore(
        self,
        data: bytes,
        *,
        mode: str = "skip",
        slugs: Iterable[str] | None = None,
    ) -> RestoreReport:
        """Validate ``data`` and apply the selected plugins.

        Raises :class:`InvalidBackupError` (Phase A) before any mutation, or
        :class:`UnknownPluginError` when a requested filter slug is not in the
        archive. Per-plugin apply failures are reported, never raised.
        """
        if mode not in _RESTORE_MODES:
            raise ValueError(f"unsupported restore mode: {mode!r}")
        plugins = validate_archive(
            data,
            max_plugin_zip_bytes=self._manager.max_plugin_zip_bytes,
            core_version=self._manager.core_version,
        )
        selected = self._select(plugins, slugs)
        results: list[RestoreResult] = []
        for plugin in selected:
            # Hold the plugin's lifecycle lock (reentrant for this task) across the
            # whole apply, so an ordinary import/connect/remove/reload of the same
            # slug cannot interleave with the restore sequence.
            lock = await self._manager.lifecycle_lock(plugin.slug)
            async with lock:
                results.append(await self._apply(plugin, mode))
        return RestoreReport(format_version=FORMAT_VERSION, results=tuple(results))

    def _select(
        self, plugins: Sequence[ArchivePlugin], slugs: Iterable[str] | None
    ) -> list[ArchivePlugin]:
        if slugs is None:
            return list(plugins)
        by_slug = {plugin.slug: plugin for plugin in plugins}
        chosen: list[ArchivePlugin] = []
        seen: set[str] = set()
        for raw in slugs:
            if not is_valid_slug(raw) or raw not in by_slug:
                raise UnknownPluginError(str(raw))
            if raw not in seen:
                seen.add(raw)
                chosen.append(by_slug[raw])
        return chosen

    async def _apply(self, plugin: ArchivePlugin, mode: str) -> RestoreResult:
        slug = plugin.slug
        exists = await self._exists(slug)
        if mode == "skip" and exists:
            return RestoreResult(slug, "skipped", await self._status(slug))
        # Capture the secrets the replacement constructor will read *before* it can
        # rotate them, so a constructor-cached original stays redactable.
        pre_secrets = await self._manager.current_secret_strings(slug)
        try:
            # Install without auto-connect so state and ``on_restore`` run before
            # any connect. ``replace`` is transactional: a new package that fails
            # to load restores the previous code instead of destroying a working
            # plugin (and a failed restore therefore never loses the original).
            record = await self._manager.install(
                plugin.package_bytes,
                mode="replace" if exists else "reject",
                auto_connect=False,
            )
        except asyncio.CancelledError:
            # The install may have committed and then been cancelled during
            # cleanup, leaving a live replacement runtime. Attach the captured
            # originals before propagating so its cached secrets stay redactable.
            self._manager.retain_secrets(slug, pre_secrets)
            raise
        except (InvalidPluginError, PackageTooLargeError, ManifestError) as exc:
            return RestoreResult(slug, "failed", await self._status(slug), _safe_message(exc))
        except Exception:
            logger.warning(
                "restore_install_failed", extra={"extra_fields": {"slug": slug}}
            )
            return RestoreResult(
                slug, "failed", await self._status(slug), "The plugin could not be restored."
            )

        if record.load_state == LOAD_FAILED:
            return RestoreResult(slug, "failed", await self._status(slug), _LOAD_FAILED_DETAIL)

        # Retain the pre-constructor secrets (the replacement may have rotated
        # them), then the current ones, so every later description refresh redacts
        # both. Then restore the archived auth mode exactly.
        self._manager.retain_secrets(slug, pre_secrets)
        await self._manager.retain_current_secrets(slug)
        await self._manager.set_auth_mode(slug, plugin.auth_mode)

        worker = asyncio.ensure_future(
            asyncio.to_thread(self._write_state, slug, plugin.data, plugin.secrets)
        )
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Settle the started worker *while still holding the lifecycle lock*
            # so it cannot write live state after the lock is released, then
            # propagate the cancellation.
            await _settle_worker(worker)
            raise
        except Exception:
            logger.warning(
                "restore_state_failed", extra={"extra_fields": {"slug": slug}}
            )
            return RestoreResult(
                slug, "failed", await self._status(slug), "The plugin state could not be restored."
            )

        await self._call_on_restore(slug)
        status, detail = await self._reconnect(slug, plugin)
        return RestoreResult(slug, "restored", status, detail)

    async def _exists(self, slug: str) -> bool:
        if slug in self._manager.installed_slugs():
            return True
        return await asyncio.to_thread(
            os.path.lexists, self._manager.package_path(slug)
        )

    async def _status(self, slug: str) -> str | None:
        detail = await self._manager.detail(slug)
        if detail is None:
            return None
        status = detail.get("status")
        return status if isinstance(status, str) else None

    def _write_state(
        self, slug: str, data: dict[str, Any], secrets: dict[str, Any]
    ) -> None:
        """Restore an exact data/secret snapshot with no TTL.

        Each namespace is written from the backup and then any remaining key is
        deleted, so a pre-existing key that is absent from the backup does not
        survive (including reserved ``core.*`` secret keys). Restored values carry
        no TTL because ``set`` is called without one.
        """
        for namespace, values in ((DATA_NAMESPACE, data), (SECRET_NAMESPACE, secrets)):
            storage = self._storage.scoped(
                slug,
                secret=(namespace == SECRET_NAMESPACE),
                allow_reserved=True,
            )
            existing = set(storage.list_keys())
            for key, value in values.items():
                storage.set(key, value)
            for key in existing - set(values):
                storage.delete(key)

    async def _call_on_restore(self, slug: str) -> None:
        """Run the optional ``on_restore`` hook; a failure is logged, not raised."""
        runtime = self._manager.runtime_for(slug)
        if runtime is None or runtime.instance is None:
            return
        try:
            await self._host.call(runtime, "on_restore")
        except asyncio.CancelledError:
            raise
        except PluginCallError as exc:
            logger.warning(
                "plugin_on_restore_failed",
                extra={"extra_fields": {"slug": slug, "message": exc.safe_message}},
            )
        except Exception:
            logger.warning(
                "plugin_on_restore_failed", extra={"extra_fields": {"slug": slug}}
            )

    async def _reconnect(self, slug: str, plugin: ArchivePlugin) -> tuple[str | None, str | None]:
        """Reconnect with the stored auth mode and return ``(status, detail)``."""
        detail = await self._manager.detail(slug)
        methods = detail.get("auth_methods") if isinstance(detail, dict) else None
        supported = methods if isinstance(methods, list) else []
        mode = plugin.auth_mode
        credentials: dict[str, Any]
        if mode is None:
            if "none" not in supported:
                # No stored mode and no ``none`` auth: leave it disconnected.
                return await self._status(slug), None
            mode = "none"
            credentials = {}
        else:
            credentials = _credentials_for(mode, plugin.secrets)
        try:
            result = await self._manager.connect(slug, mode=mode, credentials=credentials)
        except PluginConnectError as exc:
            logger.warning(
                "restore_reconnect_failed",
                extra={"extra_fields": {"slug": slug, "message": exc.message}},
            )
            return await self._status(slug), _RECONNECT_FAILED
        except Exception:
            logger.warning(
                "restore_reconnect_failed", extra={"extra_fields": {"slug": slug}}
            )
            return await self._status(slug), _RECONNECT_FAILED
        status = result.get("status")
        return (status if isinstance(status, str) else None), None


def _safe_message(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:300] if text else "The plugin could not be restored."


async def _settle_worker(worker: asyncio.Task[Any]) -> None:
    """Wait for a started worker to finish, ignoring further cancellation."""
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    with contextlib.suppress(BaseException):
        worker.exception()


__all__ = [
    "BACKUP_FILENAME",
    "FORMAT_VERSION",
    "STREAM_CHUNK_BYTES",
    "ArchivePlugin",
    "BackupError",
    "BackupService",
    "InvalidBackupError",
    "RestoreReport",
    "RestoreResult",
    "RestoreService",
    "UnknownPluginError",
    "validate_archive",
]
