# Phase 7 plan — concurrent health scheduler and state-machine coverage

Goal: a background scheduler that periodically pings loaded, connected plugins
concurrently and drives the connection state machine (transient vs hard failures,
auth-expired refresh), persisting metadata and logs.

## Acceptance gate (TEST_MATRIX row 7)
All ping result rows; transient failures never disconnect or count as hard; hard
threshold; refresh success and failure; last ping/meta/log updates; no pings for
disconnected/OAuth-required; hung plugin does not block another; no overlapping
cycles; stale runtime observations do not undo disconnect/replacement.

## Contract (ARCHITECTURE.md "Health and OAuth")
- Only loaded plugins whose status is `connected` or `auth_expired` are pinged.
- Cycles never overlap; pings are concurrent and bounded by the host's per-plugin
  semaphore/timeout. A hung plugin must not block another.
- `connected` → reset `fail_count` and `transient_error_count`, clear `last_error`.
- `error`/timeout/exception → **transient**: keep status, increment only
  `transient_error_count`; never disconnect, never a hard failure.
- `not_connected` (a ping result) → **hard**: increment `fail_count`.
- `auth_expired` → refresh under the lifecycle lock; `connected` resets, otherwise
  hard.
- At `PING_FAIL_THRESHOLD` hard failures set status `not_connected` (or
  `auth_expired` per cause).
- Persist `last_ping_at`, all metadata and transitions, and log the transition.
- A result from a runtime that is no longer current (replaced/disconnected while
  the ping was in flight) is discarded.

## Config
`PING_INTERVAL_SECONDS` (60), `PING_TIMEOUT_SECONDS` (10), `PING_FAIL_THRESHOLD`
(3) already exist in `Settings`.

## Slices (non-overlapping ownership)
### 7a health scheduler (one agent)
- `src/custom_mcp/health.py`: `HealthScheduler` (start/stop, `run_cycle`, per-plugin
  `_ping_one`, state-machine application, stale-runtime guard).
- `src/custom_mcp/plugins/manager.py`: minimal public helpers to read/update health
  metadata and status safely (no behaviour change to existing methods).
- `src/custom_mcp/main.py`: construct and start the scheduler in the lifespan after
  startup/mounting; stop it first on shutdown.
- `tests/unit/test_health.py`, `tests/integration/test_health_scheduler.py`.
- `README.md`: a short health/state-machine section.
