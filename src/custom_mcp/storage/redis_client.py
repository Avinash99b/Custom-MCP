"""Redis client construction behind narrow structural protocols.

Exposes only the commands the application needs. Keeping the surface small lets
tests inject fake clients and avoids leaking redis-py's sync/async typing quirks
into the rest of the codebase. This module is owned exclusively by the Phase 2
storage/logs unit.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Iterator
from typing import Protocol, cast, runtime_checkable

import redis
import redis.asyncio as aioredis


@runtime_checkable
class AsyncRedis(Protocol):
    """The subset of an async Redis client used by the application."""

    async def ping(self) -> bool: ...

    async def get(self, name: str) -> str | None: ...

    async def getdel(self, name: str) -> str | None: ...

    async def set(self, name: str, value: str, *, ex: int | None = None) -> object: ...

    async def delete(self, *names: str) -> int: ...

    async def ttl(self, name: str) -> int: ...

    def scan_iter(self, match: str | None = None) -> AsyncIterator[str]: ...

    async def smembers(self, name: str) -> builtins.set[str]: ...

    async def sadd(self, name: str, *values: str) -> int: ...

    async def srem(self, name: str, *values: str) -> int: ...

    async def lpush(self, name: str, *values: str) -> int: ...

    async def ltrim(self, name: str, start: int, end: int) -> object: ...

    async def lrange(self, name: str, start: int, end: int) -> list[str]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SyncRedis(Protocol):
    """The subset of a synchronous Redis client used by plugin worker threads."""

    def ping(self) -> bool: ...

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str, *, ex: int | None = None) -> object: ...

    def delete(self, *names: str) -> int: ...

    def ttl(self, name: str) -> int: ...

    def pttl(self, name: str) -> int: ...

    def scan_iter(self, match: str | None = None) -> Iterator[str]: ...

    def lpush(self, name: str, *values: str) -> int: ...

    def ltrim(self, name: str, start: int, end: int) -> object: ...

    def lrange(self, name: str, start: int, end: int) -> list[str]: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...

    def close(self) -> None: ...


def create_async_redis(url: str, *, decode_responses: bool = True) -> AsyncRedis:
    """Create the async Redis client used by the core."""
    client = aioredis.Redis.from_url(url, decode_responses=decode_responses)
    return cast("AsyncRedis", client)


def create_sync_redis(url: str, *, decode_responses: bool = True) -> SyncRedis:
    """Create the synchronous Redis client used by plugin worker threads."""
    client = redis.Redis.from_url(url, decode_responses=decode_responses)
    return cast("SyncRedis", client)


__all__ = [
    "AsyncRedis",
    "SyncRedis",
    "create_async_redis",
    "create_sync_redis",
]
