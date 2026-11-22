# `tests/real_redis` — isolated real-Redis integration tests

Focused integration tests that run against a **dedicated** `redis-server`
process on a private unix socket. They never connect to, import state from, or
flush the pre-existing Redis on `localhost:6379` (or any other shared instance).

## Requirements

- `redis-server` on `PATH`, version **>= 6.2** (needed for the Lua/`PXAT`
  storage-commit path used elsewhere in Phase 2).
- The project's `.venv` (redis-py sync + async clients).

If `redis-server` is missing, every test in this package is skipped
(`@pytest.mark.skipif(shutil.which("redis-server") is None)`), so the default
suite still passes on machines without the binary. A future CI phase can add a
dedicated real-Redis job; these tests reuse that job unchanged.

## Running

```bash
.venv/bin/python -m pytest -q tests/real_redis
.venv/bin/ruff check tests/real_redis
.venv/bin/mypy --warn-return-any --strict tests/real_redis
```

## What the fixtures do (`conftest.py`)

`redis_server` starts one owned process per test:

```text
redis-server --port 0 --bind 127.0.0.1 \
  --unixsocket <owned-tmp>/redis.sock --unixsocketperm 700 \
  --dir <owned-tmp> --appendonly yes --appendfsync always --save "" \
  --logfile <owned-tmp>/redis.log --daemonize no
```

- TCP is disabled (`--port 0`); only the unix socket is used.
- RDB snapshots are disabled (`--save ""`) so restart tests prove durability
  from the **AOF** (`--appendfsync always`).
- The socket/data/log live in an owned `tempfile.mkdtemp` dir under
  `/tmp/opencode` (fallback: the system temp dir). The socket path is asserted
  `<= 100` chars for the os-level limit.
- Readiness is a bounded monotonic-timeout poll (`ping`) inside the fixture; the
  redis log tail is included if startup fails.
- Teardown stops only the process this instance spawned (`SIGTERM`, then
  `SIGKILL` after a timeout) and removes only its owned directory.
- `RealRedisServer.restart()` stops and restarts on the exact same owned dir,
  socket and AOF, then waits for a fresh ping.

Fixtures: `redis_server` (process), `redis_sync`, `redis_async`, `manager`
(`StorageManager` on the real server). `redis_sync`/`redis_async`/`manager`
intentionally shadow the fakeredis fixtures from the top-level `conftest.py`
for this directory only.

## Coverage today

- Slug `rredisexample` matches the pattern; server version `>= 6.2`.
- Generic `EVAL` and absolute `PXAT` availability (not the commit script).
- Scoped data round-trip, listing, delete and exact-slug isolation.
- Core reserved `core.` secret round-trip plus the write/delete guard.
- TTL bound via `PTTL` (positive, no sleeps).
- `PluginContext` scoping of data vs secrets.
- AOF restart: data, secrets and a TTL key survive a process restart, with the
  remaining TTL still bounded.
- `PluginLogs` sync callback redacts current secrets and the log list is capped
  at `LOG_CAP` (200).
- Candidate storage transactions against the real server: staged reads/writes
  stay detached; discard plus late writes leave live data/secrets untouched and
  block commit; a successful commit applies the data+secret delta and rebinds
  the context proxy to live storage; the `read_commit_outcome` receipt is
  `committed` with a bounded (`<= 600s`) TTL; an untouched key keeps its TTL
  while a staged TTL is honoured; and a staged key whose 1s deadline passes
  before commit is deleted, never revived (bounded, server-`TIME`-based wait).

All values are deterministic dummies; no real credentials are stored, and the
server has no password.

## Transaction tests

The `StorageTransaction` public surface is stable, so `test_storage.py` now
exercises `begin_transaction` / `context` / `commit` / `discard` / `state` /
`commit_id` against the real server, using `read_commit_outcome` /
`read_commit_receipt` for receipt assertions. These do not inspect or depend on
the Lua script internals; they assert the observable contract only. Fault
injection (dropped replies, OOM, ACL, overload) remains in the fakeredis unit
tests and is intentionally out of scope here.

