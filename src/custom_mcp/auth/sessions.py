"""Dashboard session storage and hardened cookie construction.

Sessions are random, server-side, and stored in Redis under ``core:session:{id}``
with a sliding-free absolute TTL. Only this module knows the cookie name and
attributes.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

from custom_mcp.storage.redis_client import AsyncRedis

SESSION_COOKIE_NAME = "session"
SESSION_KEY_PREFIX = "core:session:"


@dataclass(frozen=True, slots=True)
class Session:
    """A stored dashboard session."""

    id: str
    created_at: float


def _build_cookie(
    session_id: str,
    *,
    max_age: int,
    secure: bool,
    expires: str | None = None,
) -> str:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE_NAME] = session_id
    morsel = cookie[SESSION_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["max-age"] = str(max_age)
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    if secure:
        morsel["secure"] = True
    if expires is not None:
        morsel["expires"] = expires
    return morsel.OutputString()


class SessionService:
    """Create, look up and revoke dashboard sessions and their cookies."""

    def __init__(self, redis: AsyncRedis, *, ttl_seconds: int, cookie_secure: bool) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._cookie_secure = cookie_secure

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{SESSION_KEY_PREFIX}{session_id}"

    async def create(self) -> str:
        """Create a new session and return its opaque id."""
        session_id = secrets.token_urlsafe(32)
        payload = json.dumps({"created_at": time.time()})
        await self._redis.set(self._key(session_id), payload, ex=self._ttl_seconds)
        return session_id

    async def get(self, session_id: str) -> Session | None:
        """Return the session for ``session_id``, or ``None`` if missing/expired."""
        if not session_id:
            return None
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            created_at = float(data["created_at"])
        except (ValueError, TypeError, KeyError):
            return None
        return Session(id=session_id, created_at=created_at)

    async def delete(self, session_id: str) -> None:
        """Revoke a session if it exists."""
        if not session_id:
            return
        await self._redis.delete(self._key(session_id))

    def build_cookie(self, session_id: str) -> str:
        """Build a hardened ``Set-Cookie`` value for a new session."""
        return _build_cookie(
            session_id, max_age=self._ttl_seconds, secure=self._cookie_secure
        )

    def clear_cookie(self) -> str:
        """Build a ``Set-Cookie`` value that clears the session cookie."""
        return _build_cookie(
            "",
            max_age=0,
            secure=self._cookie_secure,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
        )


__all__ = ["SESSION_COOKIE_NAME", "SESSION_KEY_PREFIX", "Session", "SessionService"]
