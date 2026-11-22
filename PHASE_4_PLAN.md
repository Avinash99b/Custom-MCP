# Phase 4 plan — API-key connect/disconnect and the example plugin

Goal: implement `POST /api/plugins/{slug}/connect` and `/disconnect`, persist the
selected auth mode + connection status, swap the live tool set, and ship the
`apikey_demo` example plugin.

## Acceptance gate (TEST_MATRIX row 4)
API-key hook observes `core.api_key` already stored; supported mode/body
validation; connect status persisted; changed tool set swapped; secrets absent
from every JSON response and public diagnostic; disconnect hook and secret wipe;
API-key demo exercises credential rejection / transient upstream mapping.

## Contract (ARCHITECTURE.md)
- Connect validates the supported mode, stores the selected mode and supplied
  credentials, calls the credential hook(s) then `connect()`; the selected mode and
  status are persisted in `core:plugin:{slug}:meta`.
- Disconnect calls `on_disconnect`, wipes **all** secrets, sets status
  `not_connected` and `auth_mode` null.
- Credential hooks receive their arguments **after** the core stores the same
  credentials in reserved scoped keys (`core.api_key`).
- Secrets never appear in API JSON or diagnostics (key names only).

## Slices (non-overlapping ownership)
### 4a — connect/disconnect
- `src/custom_mcp/plugins/manager.py`: add `connect(slug, *, mode, credentials)`
  and `disconnect(slug)` under the stable slug lock, plus a domain
  `PluginConnectError(code, message)`. On connect: validate mode against the
  manifest; for `api_key` require a non-empty key, store `core.api_key`, call
  `on_api_key_set(api_key)` then `connect()`; set `runtime.status`; persist
  `auth_mode` + status; re-list tools; re-mount the MCP app so the live tool set
  swaps (`_notify_runtime_change`). On disconnect: call `on_disconnect`, wipe
  `plugin:{slug}:secret:*` (incl. core), set status `not_connected` and
  `auth_mode=null`, re-list tools, re-mount.
- `src/custom_mcp/api/plugins.py`: `POST /api/plugins/{slug}/connect` and
  `/disconnect` handlers with bounded JSON body parsing; map `PluginConnectError`
  to `plugin_error(code, message)`; return the plugin detail JSON.
- `src/custom_mcp/api/errors.py`: add `conflict: 409` (and any needed code).
- `tests/integration/test_connect.py`: end-to-end connect/disconnect via the real
  app with an api_key fixture plugin.

### 4b — apikey_demo example
- `plugin_examples/apikey_demo/{manifest.json,plugin.py,README.md}`: `whoami`
  always, `fetch_item` only once a key is stored (changed tool set); `whoami`
  returns non-secret metadata; `fetch_item` calls an upstream with the stored key
  and maps 2xx → success, 401/403 → `auth_expired` (non-retryable),
  timeout/network/5xx → retryable `upstream_unavailable`.
- `tests/unit/test_apikey_demo.py`: hook sees `core.api_key` already stored;
  401 → `auth_expired`; timeout/5xx → retryable; tool set changes with the key.

## Evidence required
- Connect persists selected mode + status and swaps the live tool set.
- Disconnect wipes every secret and resets mode/status; `on_disconnect` observed.
- No secret value in any JSON response or log.
- Demo maps credential rejection vs transient failure.
- Focused tests, then full suite, Ruff, mypy.
