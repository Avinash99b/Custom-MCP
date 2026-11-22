"""Concurrent periodic health pings and the connection state machine.

The scheduler only observes plugins that are loaded and currently
``connected`` or ``auth_expired``. Pings run concurrently through the
:class:`~custom_mcp.plugins.host.PluginHost` (which enforces the per-plugin
semaphore and timeout), so a hung plugin cannot block another. Cycles never
overlap: a tick is skipped while a cycle is still running.

Every observation is encoded as one of three outcomes:

* ``connected`` -- reset both failure counters and clear ``last_error``;
* transient (``error`` status, timeout, exception, overload) -- keep the
  current status and increment only ``transient_error_count``;
* hard (``not_connected`` or an unrefreshable ``auth_expired``) -- increment
  ``fail_count`` and, at the configured threshold, change the status.

A result is applied only while the runtime that produced it is still the
manager's current generation and the plugin is still loaded with a pingable
status, so a ping that raced a disconnect/replacement is discarded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from custom_mcp.logging import get_logger
from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.manager import PluginManager
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import Status

logger = get_logger("health")

#: Persisted statuses that are eligible for an active health probe.
_PINGABLE_STATUSES = frozenset({Status.CONNECTED.value, Status.AUTH_EXPIRED.value})

_PING_TIMEOUT_MESSAGE = "Plugin health check failed."


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class HealthScheduler:
    """Periodically ping eligible plugins and drive connection status."""

    def __init__(
        self,
        manager: PluginManager,
        host: PluginHost,
        *,
        interval: float,
        timeout: float,
        threshold: int,
    ) -> None:
        if interval <= 0 or timeout <= 0:
            raise ValueError("health interval and timeout must be positive")
        if threshold < 1:
            raise ValueError("health failure threshold must be positive")
        self._manager = manager
        self._host = host
        self._interval = interval
        self._timeout = timeout
        self._threshold = threshold
        self._task: asyncio.Task[None] | None = None
        self._cycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        """Whether the background cycle loop is currently active."""
        return self._task is not None and not self._task.done()

    @property
    def interval(self) -> float:
        """Seconds between scheduled cycles."""
        return self._interval

    @property
    def timeout(self) -> float:
        """Per-ping host timeout in seconds."""
        return self._timeout

    @property
    def threshold(self) -> int:
        """Hard-failure count that changes the status."""
        return self._threshold

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Start the background cycle loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="custom-mcp-health")

    async def stop(self) -> None:
        """Cancel and await the background loop; no ping runs afterwards."""
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("health_scheduler_failed", exc_info=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("health_cycle_failed", exc_info=True)

    # --- cycle ----------------------------------------------------------

    async def run_cycle(self) -> None:
        """Ping every eligible plugin once, concurrently; skip if one is running."""
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            runtimes = self._manager.loaded_runtimes()
            if not runtimes:
                return
            await asyncio.gather(
                *(self._guarded_ping(runtime) for runtime in runtimes)
            )

    async def _guarded_ping(self, runtime: PluginRuntime) -> None:
        try:
            await self._ping_one(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "health_ping_failed",
                extra={"extra_fields": {"slug": runtime.slug}},
                exc_info=True,
            )

    async def _ping_one(self, runtime: PluginRuntime) -> None:
        if not await self._eligible(runtime):
            return

        try:
            result = await self._host.call(runtime, "ping", timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except PluginCallError as exc:
            await self._record_transient(runtime, exc.safe_message)
            return
        except Exception:
            await self._record_transient(runtime, _PING_TIMEOUT_MESSAGE)
            return

        if result is Status.CONNECTED:
            await self._record_connected(runtime)
        elif result is Status.AUTH_EXPIRED:
            await self._refresh_expired(runtime)
        elif result is Status.NOT_CONNECTED:
            await self._record_hard(
                runtime,
                "Plugin health check reported not connected.",
                cause=None,
            )
        elif result is Status.ERROR:
            await self._record_transient(
                runtime, "Plugin health check reported an error."
            )
        else:
            await self._record_transient(
                runtime, "Plugin health check returned an unexpected status."
            )

    async def _eligible(self, runtime: PluginRuntime) -> bool:
        if runtime.load_state != LoadState.LOADED or runtime.instance is None:
            return False
        if self._manager.runtime_for(runtime.slug) is not runtime:
            return False
        health = await self._manager.read_health(runtime.slug)
        return health.get("status") in _PINGABLE_STATUSES

    # --- state machine application -------------------------------------

    async def _current_health(self, runtime: PluginRuntime) -> dict[str, Any] | None:
        """Return health when ``runtime`` is still current and pingable.

        Callers must hold ``runtime.lock`` so no lifecycle operation can change
        the runtime or connected state between this check and the write.
        """
        if runtime.load_state != LoadState.LOADED or runtime.instance is None:
            return None
        if self._manager.runtime_for(runtime.slug) is not runtime:
            return None
        health = await self._manager.read_health(runtime.slug)
        if health.get("status") not in _PINGABLE_STATUSES:
            return None
        return health

    async def _record_connected(self, runtime: PluginRuntime) -> None:
        async with runtime.lock:
            health = await self._current_health(runtime)
            if health is None:
                return
            await self._apply_connected(runtime, health, _utc_now())

    async def _record_transient(self, runtime: PluginRuntime, message: str) -> None:
        async with runtime.lock:
            health = await self._current_health(runtime)
            if health is None:
                return
            await self._apply_transient(runtime, health, message, _utc_now())

    async def _record_hard(
        self,
        runtime: PluginRuntime,
        message: str,
        *,
        cause: Status | None,
    ) -> None:
        async with runtime.lock:
            health = await self._current_health(runtime)
            if health is None:
                return
            await self._apply_hard(runtime, health, message, cause, _utc_now())

    async def _refresh_expired(self, runtime: PluginRuntime) -> None:
        async with runtime.lock:
            health = await self._current_health(runtime)
            if health is None:
                return
            # A refresh rotates the stored tokens. Retain the pre-rotation values
            # first so a plugin that cached them in memory cannot re-expose an
            # old (still usable) token through a later log or description.
            await self._manager.retain_current_secrets(runtime.slug)
            try:
                result = await self._host.call(
                    runtime, "refresh", timeout=self._timeout
                )
            except asyncio.CancelledError:
                raise
            except PluginCallError as exc:
                # Retain whatever the rotation produced before scoring the result.
                await self._manager.retain_current_secrets(runtime.slug)
                await self._apply_hard(
                    runtime, health, exc.safe_message, Status.AUTH_EXPIRED, _utc_now()
                )
                return
            except Exception:
                await self._manager.retain_current_secrets(runtime.slug)
                await self._apply_hard(
                    runtime,
                    health,
                    "Plugin authentication could not be refreshed.",
                    Status.AUTH_EXPIRED,
                    _utc_now(),
                )
                return

            # Retain the freshly issued tokens too: a later failed wipe snapshot
            # must not lose the redaction history for them either.
            await self._manager.retain_current_secrets(runtime.slug)
            if result is Status.CONNECTED:
                await self._apply_connected(runtime, health, _utc_now())
                return
            await self._apply_hard(
                runtime,
                health,
                "Plugin authentication could not be refreshed.",
                Status.AUTH_EXPIRED,
                _utc_now(),
            )

    # The ``_apply_*`` helpers assume the caller holds ``runtime.lock`` and has
    # already re-validated the runtime generation and pingable status.
    async def _apply_connected(
        self, runtime: PluginRuntime, health: dict[str, Any], now: str
    ) -> None:
        previous = str(health.get("status") or Status.NOT_CONNECTED.value)
        await self._manager.apply_health(
            runtime.slug,
            status=Status.CONNECTED,
            fail_count=0,
            transient_error_count=0,
            last_ping_at=now,
            last_error=None,
        )
        await self._log_transition(runtime.slug, previous, Status.CONNECTED.value)

    async def _apply_transient(
        self,
        runtime: PluginRuntime,
        health: dict[str, Any],
        message: str,
        now: str,
    ) -> None:
        status = str(health.get("status") or Status.CONNECTED.value)
        await self._manager.apply_health(
            runtime.slug,
            status=status,
            fail_count=_as_int(health.get("fail_count")),
            transient_error_count=_as_int(health.get("transient_error_count")) + 1,
            last_ping_at=now,
            last_error=message,
        )

    async def _apply_hard(
        self,
        runtime: PluginRuntime,
        health: dict[str, Any],
        message: str,
        cause: Status | None,
        now: str,
    ) -> None:
        previous = str(health.get("status") or Status.NOT_CONNECTED.value)
        fail_count = _as_int(health.get("fail_count")) + 1
        status = previous
        if fail_count >= self._threshold:
            status = (
                Status.AUTH_EXPIRED.value
                if cause is Status.AUTH_EXPIRED
                else Status.NOT_CONNECTED.value
            )
        await self._manager.apply_health(
            runtime.slug,
            status=status,
            fail_count=fail_count,
            transient_error_count=_as_int(health.get("transient_error_count")),
            last_ping_at=now,
            last_error=message,
        )
        logger.warning(
            "plugin_health_hard_failure",
            extra={
                "extra_fields": {
                    "slug": runtime.slug,
                    "fail_count": fail_count,
                    "threshold": self._threshold,
                    "status": status,
                    "message": message,
                }
            },
        )
        await self._manager.log_health(runtime.slug, message, level="error")
        await self._log_transition(runtime.slug, previous, status)

    async def _log_transition(self, slug: str, previous: str, current: str) -> None:
        if previous == current:
            return
        logger.info(
            "plugin_health_status_changed",
            extra={
                "extra_fields": {
                    "slug": slug,
                    "from": previous,
                    "to": current,
                }
            },
        )
        # Persist the transition in the plugin's own bounded log too. The
        # helper is best-effort and never masks a health result.
        await self._manager.log_health(
            slug,
            f"Health status changed from {previous} to {current}.",
            level="info",
        )


__all__ = ["HealthScheduler"]
