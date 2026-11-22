"""In-memory, per-IP login rate limiting.

The limiter is process-local and holds no Redis state. The caller-supplied IP
must come from the transport peer (``request.client``), never from spoofable
forwarding headers. Time comes from a monotonic clock so the window cannot be
moved by wall-clock changes, and the number of tracked IPs is bounded.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_MAX_ENTRIES = 10_000


@dataclass(slots=True)
class _Window:
    """A fixed window of failed attempts for a single IP."""

    start: float
    count: int = 0


class LoginRateLimiter:
    """Count failed logins per IP within a fixed, monotonic window."""

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._windows: dict[str, _Window] = {}
        self._lock = asyncio.Lock()

    @property
    def tracked_count(self) -> int:
        """Number of IP entries currently retained (for tests and diagnostics)."""
        return len(self._windows)

    def _prune(self, now: float) -> None:
        expired = [
            ip
            for ip, window in self._windows.items()
            if now - window.start >= self._window_seconds
        ]
        for ip in expired:
            self._windows.pop(ip, None)

    def _enforce_capacity(self, now: float) -> None:
        if len(self._windows) < self._max_entries:
            return
        self._prune(now)
        if len(self._windows) >= self._max_entries:
            overflow = len(self._windows) - self._max_entries + 1
            oldest = sorted(self._windows.items(), key=lambda item: item[1].start)[:overflow]
            for ip, _ in oldest:
                self._windows.pop(ip, None)

    async def retry_after(self, ip: str) -> int | None:
        """Return whole seconds to wait when blocked, else ``None``."""
        now = self._clock()
        async with self._lock:
            window = self._windows.get(ip)
            if window is None:
                return None
            elapsed = now - window.start
            if elapsed >= self._window_seconds:
                self._windows.pop(ip, None)
                return None
            if window.count < self._max_failures:
                return None
            return max(1, math.ceil(self._window_seconds - elapsed))

    async def record_failure(self, ip: str) -> None:
        """Record a failed login attempt for ``ip``."""
        now = self._clock()
        async with self._lock:
            window = self._windows.get(ip)
            if window is None or now - window.start >= self._window_seconds:
                self._enforce_capacity(now)
                self._windows[ip] = _Window(start=now, count=1)
                return
            window.count += 1

    async def reset(self, ip: str) -> None:
        """Clear the failure counter after a successful login."""
        async with self._lock:
            self._windows.pop(ip, None)


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_WINDOW_SECONDS",
    "LoginRateLimiter",
]
