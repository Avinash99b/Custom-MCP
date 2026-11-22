"""Scoped storage contract and the frozen plugin context."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Storage(Protocol):
    """Scoped key/value storage handed to a plugin.

    Keys are plugin-local; the host scopes them to the owning slug. Values are
    JSON-serializable and bounded by the host. A ``ttl`` marks a value as
    transient (excluded from backups).
    """

    def get(self, key: str, default: Any = None) -> Any:
        """Return the stored value, or ``default`` when absent."""
        ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store a JSON-serializable value, optionally with a TTL in seconds."""
        ...

    def delete(self, key: str) -> bool:
        """Delete a stored value; return whether it existed."""
        ...

    def list_keys(self, prefix: str = "") -> list[str]:
        """List stored keys, optionally filtered by ``prefix``."""
        ...


@dataclass(frozen=True)
class PluginContext:
    """Immutable per-plugin context passed to the plugin by the host."""

    slug: str
    storage: Storage
    secrets: Storage
    log: Callable[[str, str], None]
    callback_url: str


__all__ = ["PluginContext", "Storage"]
