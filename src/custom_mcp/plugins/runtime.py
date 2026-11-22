"""Per-plugin runtime state shared by the Phase 2 host and loader.

A runtime is deliberately a plain state object: the lifecycle coordinator owns
the stable slug lock and semaphore and passes them in. Retiring an MCP app or
swapping an instance must never replace these coordination primitives.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Any

from custom_mcp.plugins.manifest import Manifest
from mcp_plugin_sdk import AuthMode, MCPPlugin, PluginContext, Status, Tool


class LoadState(str, Enum):  # noqa: UP042 - spec mandates str, Enum
    """Lifecycle of a runtime's plugin code."""

    LOADING = "loading"
    LOADED = "loaded"
    LOAD_FAILED = "load_failed"
    UNLOADING = "unloading"


@dataclass(slots=True)
class PluginRuntime:
    """State for one immutable plugin generation.

    ``lock`` and ``sem`` are supplied by the lifecycle coordinator. They are
    shared across replacements and must not be recreated for a new runtime.
    ``_leaked_workers`` is host-private bookkeeping for timed-out or cancelled
    workers that have not actually finished yet.

    ``seal`` is a cross-thread visible retirement flag. When set, the runtime's
    scoped storage drops writes and its log callback drops messages, so a
    timed-out worker from a retired generation cannot recreate wiped secrets or
    leak a credential after ``disconnect``/unload. A runtime built from a
    candidate transaction context (replace/reload) seals through that
    transaction's own event, so promoted generations are contained exactly like a
    fresh-install generation. A deliberate ``shutdown`` hook runs under a
    thread-scoped permit and may still persist buffered state while sealed.
    """

    slug: str
    manifest: Manifest
    context: PluginContext
    lock: asyncio.Lock
    sem: asyncio.Semaphore
    generation: int = 0
    runtime_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    load_state: LoadState = LoadState.LOADING
    load_error: str | None = None
    instance: MCPPlugin | None = None
    tools: list[Tool] = field(default_factory=list)
    auth_methods: list[AuthMode] = field(default_factory=list)
    mounted: Any | None = None
    status: Status = Status.NOT_CONNECTED
    seal: Event | None = None
    #: Secret strings this generation has ever known (e.g. the key supplied to
    #: ``connect`` and any snapshot taken just before a ``disconnect`` wipe). Tool
    #: descriptions are redacted against the union of these and the *current*
    #: secrets, so a plugin that cached a credential in memory cannot re-expose it
    #: through a later ``list_tools`` once the stored secret is gone.
    retained_secrets: set[str] = field(default_factory=set)
    meta: dict[str, Any] = field(default_factory=dict)
    fail_count: int = 0
    transient_error_count: int = 0
    _leaked_workers: int = field(default=0, repr=False, init=False)

    @property
    def runtimeid(self) -> str:
        """Compatibility alias for the immutable runtime identifier."""
        return self.runtime_id

    @property
    def leaked_workers(self) -> int:
        """Number of timed-out/cancelled workers still running for this runtime."""
        return self._leaked_workers


__all__ = ["LoadState", "PluginRuntime"]
