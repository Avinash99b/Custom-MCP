"""Per-plugin log capture with current-secret redaction.

Plugins log from worker threads (synchronous callback), the core logs
asynchronously. Every message is redacted against the plugin's current string
secrets (including reserved ``core.`` secrets) *before* it is written to Redis or
stdout, then pushed with ``LPUSH`` and capped with ``LTRIM``. This module is
owned by the Phase 2 storage/logs unit.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from custom_mcp.logging import get_logger
from custom_mcp.storage.manager import validate_slug
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis

LOG_CAP = 200
DEFAULT_RECENT_LIMIT = 50
REDACT_MIN_LENGTH = 6
REDACTION = "***"
#: Upper bound on the extra per-slug retained redaction secrets so a long-lived
#: process cannot grow without limit across many token rotations.
RETAINED_CAP = 64
LEVELS = frozenset({"debug", "info", "warning", "error"})
_PY_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
_SECRET_NAMESPACE = "secret"
#: The contract stores each OAuth PKCE verifier in plugin *data* at
#: ``pkce:{state}``, so those values must be redacted like a secret even though
#: they are not in the secret namespace.
_PKCE_DATA_PREFIX = "pkce:"


class LogError(ValueError):
    """Raised for an invalid slug, level, or message."""


def _string_leaves(value: Any) -> list[str]:
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


def _redact_text(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTION)
    return text


def _json_key_text(key: Any) -> str:
    """Return the exact JSON object-key rendering for a permitted scalar key."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    if isinstance(key, (int, float)):
        try:
            return json.dumps(key, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise LogError("metadata contains an unsupported key value") from exc
    raise LogError("metadata object keys must be JSON scalar values")


def _render_scalar(value: Any, secrets: Sequence[str]) -> Any:
    """Keep a numeric/bool/null scalar, or redact it when it spells a secret."""
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LogError("metadata contains a non-finite value") from exc
    for secret in secrets:
        if secret in text:
            return REDACTION
    return value


def _normalize_metadata(value: Any, secrets: Sequence[str]) -> Any:
    """Rebuild a JSON metadata tree with keys normalized and secrets redacted."""
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return _render_scalar(value, secrets)
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            redacted_key = _redact_text(_json_key_text(key), secrets)
            normalized[redacted_key] = _normalize_metadata(item, secrets)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_metadata(item, secrets) for item in value]
    raise LogError("metadata contains an unsupported value type")


def _ordered_secrets(found: set[str]) -> list[str]:
    return sorted(
        (secret for secret in found if len(secret) >= REDACT_MIN_LENGTH),
        key=len,
        reverse=True,
    )


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class PluginLogs:
    """Capture, redact and bound per-plugin logs."""

    def __init__(self, redis_sync: SyncRedis, redis_async: AsyncRedis) -> None:
        self._redis_sync = redis_sync
        self._redis_async = redis_async
        # Extra per-slug strings that must stay redacted even when they are no
        # longer (or never were) in the secret namespace: an OAuth authorization
        # code, a PKCE verifier kept in plugin *data*, and tokens replaced by a
        # refresh rotation. Bounded and shared by the sync (worker-thread) and
        # async (event-loop) redaction paths.
        self._retained: dict[str, dict[str, None]] = {}
        self._retained_lock = threading.Lock()

    def retain(self, slug: str, secrets: Iterable[str]) -> None:
        """Remember ``secrets`` so this slug keeps redacting them.

        The values are redacted from every future log message, tool description
        and diagnostic, regardless of generation or of whether the value is still
        present in the secret namespace. Each slug's set is capped at
        :data:`RETAINED_CAP` (oldest first out) so it cannot grow without limit.
        """
        try:
            valid = validate_slug(slug)
        except Exception:
            return
        additions = [secret for secret in secrets if isinstance(secret, str) and secret]
        if not additions:
            return
        with self._retained_lock:
            bucket = self._retained.setdefault(valid, {})
            for secret in additions:
                if secret in bucket:
                    del bucket[secret]
                bucket[secret] = None
            while len(bucket) > RETAINED_CAP:
                bucket.pop(next(iter(bucket)))

    def _retained_strings(self, slug: str) -> list[str]:
        with self._retained_lock:
            return list(self._retained.get(slug, ()))

    def callback(self, slug: str) -> Callable[[str, str], None]:
        """Return a synchronous plugin-thread log callback for ``slug``."""
        validate_slug(slug)
        return lambda level, message: self.write_sync(slug, level, message)

    def write_sync(
        self, slug: str, level: str, message: str, *, metadata: Any = None
    ) -> None:
        """Redact and store one log entry, then echo it to stdout."""
        entry, redacted = self._render(slug, level, message, metadata, self._redaction_sync(slug))
        self._append_sync(slug, entry)
        get_logger("plugin").log(_PY_LEVELS[level], redacted)

    async def write(
        self, slug: str, level: str, message: str, *, metadata: Any = None
    ) -> None:
        """Async core-event equivalent of :meth:`write_sync`."""
        entry, redacted = self._render(
            slug, level, message, metadata, await self._redaction_async(slug)
        )
        await self._append_async(slug, entry)
        get_logger("plugin").log(_PY_LEVELS[level], redacted)

    def redact_sync(self, slug: str, message: str) -> str:
        """Return ``message`` with the plugin's current and retained secrets removed."""
        return _redact_text(message, self._redaction_sync(slug))

    async def redact(self, slug: str, message: str) -> str:
        """Async equivalent of :meth:`redact_sync`."""
        return _redact_text(message, await self._redaction_async(slug))

    def redact_secrets(self, text: str, secrets: Sequence[str]) -> str:
        """Redact ``text`` against an already-captured secret set (pure).

        Used by in-flight call paths that must redact against the secrets
        captured when the call started, not whatever is currently stored (a
        concurrent disconnect may already have wiped them).
        """
        return _redact_text(text, secrets)

    async def secret_strings(self, slug: str) -> tuple[str, ...]:
        """Return the ordered current string secrets for ``slug``.

        The result is a snapshot the caller can redact against later even after
        the underlying secrets have been wiped.
        """
        return tuple(await self._secrets_async(slug))

    async def redaction_strings(self, slug: str) -> tuple[str, ...]:
        """Return current *and* retained strings a caller must redact against.

        Unlike :meth:`secret_strings` this also includes the retained set (an
        OAuth authorization code, a PKCE verifier kept in plugin data, tokens
        rotated away by a refresh), so an in-flight consumer's captured set stays
        authoritative for a credential a plugin cached in memory.
        """
        return tuple(await self._redaction_async(slug))

    async def recent(self, slug: str, limit: int = DEFAULT_RECENT_LIMIT) -> list[dict[str, Any]]:
        """Return up to ``limit`` newest log entries for ``slug`` (capped at 200)."""
        key = self._key(slug)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise LogError("limit must be an integer")
        if limit <= 0:
            return []
        entries = await self._redis_async.lrange(key, 0, min(limit, LOG_CAP) - 1)
        parsed: list[dict[str, Any]] = []
        for raw in entries:
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                parsed.append(item)
        return parsed

    def _render(
        self,
        slug: str,
        level: str,
        message: str,
        metadata: Any,
        secrets: Sequence[str],
    ) -> tuple[str, str]:
        self._validate(slug, level, message)
        redacted = _redact_text(message, secrets)
        entry: dict[str, Any] = {
            "timestamp": _utcnow(),
            "level": level,
            "message": redacted,
        }
        if metadata is not None:
            try:
                entry["metadata"] = _normalize_metadata(metadata, secrets)
            except RecursionError as exc:
                raise LogError("metadata is too deeply nested") from exc
        try:
            rendered = json.dumps(entry, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise LogError("metadata could not be serialized") from exc
        return rendered, redacted

    @staticmethod
    def _validate(slug: str, level: str, message: str) -> None:
        validate_slug(slug)
        if level not in LEVELS:
            raise LogError(f"unknown log level: {level!r}")
        if not isinstance(message, str):
            raise LogError("log message must be a string")

    def _secrets_sync(self, slug: str) -> list[str]:
        found: set[str] = set()
        for prefix in self._secret_prefixes(slug):
            for key in self._redis_sync.scan_iter(match=prefix + "*"):
                if not key.startswith(prefix):
                    continue
                raw = self._redis_sync.get(key)
                if raw is not None:
                    found.update(self._leaves(raw))
        return _ordered_secrets(found)

    async def _secrets_async(self, slug: str) -> list[str]:
        found: set[str] = set()
        for prefix in self._secret_prefixes(slug):
            async for key in self._redis_async.scan_iter(match=prefix + "*"):
                if not key.startswith(prefix):
                    continue
                raw = await self._redis_async.get(key)
                if raw is not None:
                    found.update(self._leaves(raw))
        return _ordered_secrets(found)

    def _redaction_sync(self, slug: str) -> list[str]:
        return _ordered_secrets(
            set(self._secrets_sync(slug)) | set(self._retained_strings(slug))
        )

    async def _redaction_async(self, slug: str) -> list[str]:
        current = await self._secrets_async(slug)
        return _ordered_secrets(set(current) | set(self._retained_strings(slug)))

    @staticmethod
    def _leaves(raw: str) -> list[str]:
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        return _string_leaves(value)

    def _append_sync(self, slug: str, entry: str) -> None:
        key = self._key(slug)
        self._redis_sync.lpush(key, entry)
        self._redis_sync.ltrim(key, 0, LOG_CAP - 1)

    async def _append_async(self, slug: str, entry: str) -> None:
        key = self._key(slug)
        await self._redis_async.lpush(key, entry)
        await self._redis_async.ltrim(key, 0, LOG_CAP - 1)

    @staticmethod
    def _key(slug: str) -> str:
        return f"core:logs:{validate_slug(slug)}"

    @staticmethod
    def _secret_prefixes(slug: str) -> tuple[str, ...]:
        base = f"plugin:{validate_slug(slug)}:"
        return (f"{base}{_SECRET_NAMESPACE}:", f"{base}data:{_PKCE_DATA_PREFIX}")


__all__ = [
    "DEFAULT_RECENT_LIMIT",
    "LEVELS",
    "LOG_CAP",
    "REDACTION",
    "RETAINED_CAP",
    "LogError",
    "PluginLogs",
]
