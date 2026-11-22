"""Thread-scoped, revocable permit for deliberate lifecycle writes.

A retired runtime is sealed so a worker leaked from an earlier timed-out call
cannot recreate a wiped secret or log a credential after ``disconnect`` /
unload. That seal must not also suppress the *deliberate* ``shutdown`` hook, which
may legitimately checkpoint buffered **data** with ``ctx.storage.set``. The
permit never unblocks plugin logs: a retired generation must not emit a
credential it cached before the secrets were wiped.

The permit is thread-scoped on purpose: only the worker thread running the
approved lifecycle call inherits it, so a leaked worker on another thread stays
blocked.

The permit is also **revocable**: if the lifecycle call times out or is cancelled,
the host abandons the worker but its thread keeps running. The host revokes the
permit at that point, so an abandoned shutdown worker can no longer write to the
sealed runtime's storage.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


class WritePermit:
    """A revocable permit allowing one deliberate lifecycle call to write data."""

    __slots__ = ("_revoked",)

    def __init__(self) -> None:
        self._revoked = False

    def revoke(self) -> None:
        """Permanently revoke the permit (used when the call is abandoned)."""
        self._revoked = True

    def active(self) -> bool:
        """Whether the permit is still valid."""
        return not self._revoked


def retired_writes_allowed() -> bool:
    """Whether the current thread may write to a sealed runtime."""
    permit: WritePermit | None = getattr(_local, "permit", None)
    return permit is not None and permit.active()


@contextmanager
def use_write_permit(permit: WritePermit) -> Iterator[None]:
    """Make ``permit`` the active permit for this thread for the duration."""
    previous: WritePermit | None = getattr(_local, "permit", None)
    _local.permit = permit
    try:
        yield
    finally:
        _local.permit = previous


__all__ = ["WritePermit", "retired_writes_allowed", "use_write_permit"]
