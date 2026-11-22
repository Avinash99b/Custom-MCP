"""Shared fixtures: paired fake Redis clients, settings, app and HTTP client."""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from custom_mcp.auth.sessions import SessionService
from custom_mcp.config import Settings
from custom_mcp.logging import JsonFormatter
from custom_mcp.main import create_app
from custom_mcp.storage.redis_client import AsyncRedis, SyncRedis
from tests.helpers import TEST_SECRET, JsonLogCapture, build_env


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(build_env(DATA_DIR=str(tmp_path / "data")))


@pytest.fixture
def fake_server() -> fakeredis.FakeServer:
    """A single in-process Redis server shared by a sync and an async client."""
    return fakeredis.FakeServer()


@pytest.fixture
def redis_async(fake_server: fakeredis.FakeServer) -> AsyncRedis:
    client = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)
    return cast("AsyncRedis", client)


@pytest.fixture
def redis_sync(fake_server: fakeredis.FakeServer) -> SyncRedis:
    client = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    return cast("SyncRedis", client)


@pytest.fixture
def sessions(settings: Settings, redis_async: AsyncRedis) -> SessionService:
    return SessionService(
        redis_async,
        ttl_seconds=settings.session_ttl_seconds,
        cookie_secure=settings.COOKIE_SECURE,
    )


@pytest.fixture
def app(settings: Settings, redis_async: AsyncRedis, redis_sync: SyncRedis) -> Starlette:
    return create_app(settings=settings, redis_async=redis_async, redis_sync=redis_sync)


@pytest_asyncio.fixture
async def client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as http:
        yield http


@pytest.fixture
def secret() -> str:
    return TEST_SECRET


@pytest.fixture
def json_logs() -> Iterator[JsonLogCapture]:
    """Capture ``custom_mcp.*`` log records as JSON lines via a StreamHandler."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("custom_mcp")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield JsonLogCapture(stream)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
