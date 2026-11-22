"""Unit tests for the in-memory per-IP login rate limiter."""

from __future__ import annotations

import asyncio

from custom_mcp.auth.ratelimit import LoginRateLimiter


async def test_below_threshold_not_blocked() -> None:
    limiter = LoginRateLimiter()
    for _ in range(4):
        await limiter.record_failure("10.0.0.1")
    assert await limiter.retry_after("10.0.0.1") is None


async def test_blocks_at_threshold() -> None:
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60)
    for _ in range(5):
        await limiter.record_failure("10.0.0.2")
    retry_after = await limiter.retry_after("10.0.0.2")
    assert retry_after is not None
    assert 0 < retry_after <= 60


async def test_reset_clears_counter() -> None:
    limiter = LoginRateLimiter()
    for _ in range(5):
        await limiter.record_failure("10.0.0.3")
    await limiter.reset("10.0.0.3")
    assert await limiter.retry_after("10.0.0.3") is None


async def test_isolated_per_ip() -> None:
    limiter = LoginRateLimiter()
    for _ in range(5):
        await limiter.record_failure("10.0.0.4")
    assert await limiter.retry_after("10.0.0.5") is None


async def test_window_expires_on_monotonic_clock() -> None:
    now = [1000.0]
    limiter = LoginRateLimiter(window_seconds=60, clock=lambda: now[0])
    for _ in range(5):
        await limiter.record_failure("10.0.0.6")
    assert await limiter.retry_after("10.0.0.6") is not None

    now[0] += 61
    assert await limiter.retry_after("10.0.0.6") is None
    # A new attempt starts a fresh window rather than inheriting the old count.
    await limiter.record_failure("10.0.0.6")
    assert await limiter.retry_after("10.0.0.6") is None


async def test_capacity_is_bounded() -> None:
    limiter = LoginRateLimiter(max_entries=3, clock=lambda: 0.0)
    for index in range(10):
        await limiter.record_failure(f"10.0.0.{index}")
    assert limiter.tracked_count <= 3


async def test_concurrent_failures_are_counted_atomically() -> None:
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60)
    await asyncio.gather(*(limiter.record_failure("10.0.0.7") for _ in range(5)))
    assert await limiter.retry_after("10.0.0.7") is not None

    under = LoginRateLimiter(max_failures=5, window_seconds=60)
    await asyncio.gather(*(under.record_failure("10.0.0.8") for _ in range(4)))
    assert await under.retry_after("10.0.0.8") is None
