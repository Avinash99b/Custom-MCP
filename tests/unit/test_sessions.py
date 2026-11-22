"""Unit tests for session storage and cookie hardening."""

from __future__ import annotations

from custom_mcp.auth.sessions import SESSION_KEY_PREFIX, SessionService
from custom_mcp.storage.redis_client import AsyncRedis


async def test_create_and_get_round_trip(
    sessions: SessionService, redis_async: AsyncRedis
) -> None:
    session_id = await sessions.create()
    assert session_id

    session = await sessions.get(session_id)
    assert session is not None
    assert session.id == session_id

    ttl = await redis_async.ttl(f"{SESSION_KEY_PREFIX}{session_id}")
    assert 0 < ttl <= sessions.ttl_seconds


async def test_get_missing_session_returns_none(sessions: SessionService) -> None:
    assert await sessions.get("does-not-exist") is None
    assert await sessions.get("") is None


async def test_delete_removes_session(sessions: SessionService) -> None:
    session_id = await sessions.create()
    await sessions.delete(session_id)
    assert await sessions.get(session_id) is None


def test_build_cookie_is_hardened(sessions: SessionService) -> None:
    header = sessions.build_cookie("session-value")
    lowered = header.lower()
    assert lowered.startswith("session=session-value")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "max-age=604800" in lowered
    assert "secure" in lowered


def test_clear_cookie_expires_immediately(sessions: SessionService) -> None:
    lowered = sessions.clear_cookie().lower()
    assert lowered.startswith('session="')
    assert "max-age=0" in lowered
    assert "expires=thu, 01 jan 1970" in lowered
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
