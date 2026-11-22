"""Real Redis test fixtures for a dedicated, owned server process.

These fixtures start a *separate* ``redis-server`` bound to ``127.0.0.1`` with
TCP disabled (``--port 0``) and a private unix socket inside an owned temp
directory. They never connect to, import state from, or flush an existing Redis
on ``localhost:6379`` or anywhere else.

The server runs with AOF enabled (``--appendonly yes --appendfsync always``) and
RDB snapshots disabled (``--save ""``), so restart tests can prove durability
from the append-only file. The socket path is kept short (``<= 100`` chars) and
the process is only ever stopped via the handle we created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import redis

from custom_mcp.storage.manager import StorageManager
from custom_mcp.storage.redis_client import (
    AsyncRedis,
    SyncRedis,
    create_async_redis,
    create_sync_redis,
)

#: Absolute path to the redis-server binary, or ``None`` when unavailable.
REDIS_SERVER_BIN: str | None = shutil.which("redis-server")

_MAX_UNIX_SOCKET_PATH = 100
_READY_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.02
_STOP_TIMEOUT_SECONDS = 10.0
_LOG_TAIL_CHARS = 2000


def _pick_owned_base_dir() -> str:
    """Create a short, owned temp directory for one server's socket and data."""
    preferred = Path("/tmp/opencode")
    root = preferred if preferred.is_dir() and os.access(preferred, os.W_OK) else Path(
        tempfile.gettempdir()
    )
    return tempfile.mkdtemp(prefix="real-redis-", dir=str(root))


class RealRedisServer:
    """A dedicated redis-server process on a private unix socket.

    Owns its temp directory, unix socket and log file. ``start``/``stop``/
    ``restart`` only ever act on the process this instance spawned.
    """

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.socket_path = os.path.join(base_dir, "redis.sock")
        self.log_path = os.path.join(base_dir, "redis.log")
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _command(self) -> list[str]:
        if REDIS_SERVER_BIN is None:  # pragma: no cover - guarded by the fixture
            raise RuntimeError("redis-server binary is not available")
        return [
            REDIS_SERVER_BIN,
            "--port",
            "0",
            "--bind",
            "127.0.0.1",
            "--unixsocket",
            self.socket_path,
            "--unixsocketperm",
            "700",
            "--dir",
            self.base_dir,
            "--appendonly",
            "yes",
            "--appendfsync",
            "always",
            "--save",
            "",
            "--logfile",
            self.log_path,
            "--daemonize",
            "no",
        ]

    def _url(self) -> str:
        return f"unix://{self.socket_path}"

    def start(self) -> None:
        """Spawn the process and block until a ping on the socket succeeds."""
        if self.running:
            raise RuntimeError("real Redis server is already running")
        if len(self.socket_path) > _MAX_UNIX_SOCKET_PATH:
            raise RuntimeError(
                f"unix socket path is too long ({len(self.socket_path)} > "
                f"{_MAX_UNIX_SOCKET_PATH}): {self.socket_path}"
            )
        self._process = subprocess.Popen(
            self._command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        client = redis.Redis(unix_socket_path=self.socket_path, decode_responses=True)
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        try:
            while True:
                if self._process is None or self._process.poll() is not None:
                    raise RuntimeError(
                        self._diagnostics("redis-server exited before becoming ready")
                    )
                try:
                    if client.ping():
                        return
                except redis.RedisError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        self._diagnostics("redis-server did not become ready in time")
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            client.close()

    def _diagnostics(self, reason: str) -> str:
        tail = ""
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                tail = handle.read()[-_LOG_TAIL_CHARS:]
        except OSError:
            tail = "<no redis log available>"
        return f"{reason}; owned redis log tail:\n{tail}"

    def version(self) -> str:
        """Return the running server's ``redis_version`` string."""
        client = redis.Redis(unix_socket_path=self.socket_path, decode_responses=True)
        try:
            return str(client.info("server")["redis_version"])
        finally:
            client.close()

    def stop(self) -> None:
        """Stop only the process this instance started (SIGTERM, then SIGKILL)."""
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def restart(self) -> None:
        """Stop and start again on the exact same owned dir, socket and AOF."""
        self.stop()
        self.start()

    def sync_client(self) -> SyncRedis:
        """Return a fresh synchronous client bound to this server's socket."""
        return create_sync_redis(self._url())

    def async_client(self) -> AsyncRedis:
        """Return a fresh asyncio client bound to this server's socket."""
        return create_async_redis(self._url())

    def close(self) -> None:
        """Stop the owned process and remove only its owned directory."""
        self.stop()
        shutil.rmtree(self.base_dir, ignore_errors=True)


@pytest.fixture
def redis_server() -> Iterator[RealRedisServer]:
    """A dedicated real redis-server on a private unix socket (function scope)."""
    if REDIS_SERVER_BIN is None:
        pytest.skip("redis-server binary is not available")
    server = RealRedisServer(_pick_owned_base_dir())
    server.start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def redis_sync(redis_server: RealRedisServer) -> Iterator[SyncRedis]:
    """A sync client for the dedicated server (overrides the fakeredis fixture)."""
    client = redis_server.sync_client()
    try:
        yield client
    finally:
        client.close()


@pytest_asyncio.fixture
async def redis_async(redis_server: RealRedisServer) -> AsyncIterator[AsyncRedis]:
    """An async client for the dedicated server (overrides the fakeredis fixture)."""
    client = redis_server.async_client()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def manager(redis_sync: SyncRedis, redis_async: AsyncRedis) -> AsyncIterator[StorageManager]:
    """A StorageManager bound to the dedicated real Redis."""
    yield StorageManager(redis_sync, redis_async)


__all__ = ["REDIS_SERVER_BIN", "RealRedisServer"]
