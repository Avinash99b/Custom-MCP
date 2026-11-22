# Custom MCP

Custom MCP gateway.

The project foundation (Phase 0: configuration, authentication, Redis wiring and
tests), the plugin SDK/packaging/registry (Phase 1), the off-event-loop lifecycle
host (Phase 2), the streamable-HTTP MCP adapter and dynamic router (Phase 3),
API-key connect/disconnect with the `apikey_demo` example (Phase 4), the static
dashboard (Phase 5) and portable backup/restore with the dashboard settings area
(Phase 6) are complete:

- `POST /api/plugins/import` (multipart, `mode=reject|replace`) validates,
  stores and loads the package. A valid but unrunnable package is still
  accepted (201) and surfaced as `load_failed`.
- `GET /api/plugins` and `GET /api/plugins/{slug}` expose `load_state`
  (`loading`/`loaded`/`load_failed`/`unloading`), redacted errors, cached tool
  schemas, and credential key names only.
- `GET /api/plugins/{slug}/logs?limit=N` returns the newest redacted log entries
  (default 50, clamped to 200).
- `POST /api/plugins/{slug}/connect` and `/disconnect` select an auth mode,
  store or wipe credentials and run the plugin hooks.
- `POST /api/plugins/{slug}/reload` re-extracts the stored package, unloads the
  old runtime and loads a fresh generation.
- `DELETE /api/plugins/{slug}` shuts the plugin down (never `on_disconnect`) and
  removes only that plugin's code/state/logs.
- Each loaded plugin is exposed as a stateless streamable-HTTP MCP server at
  `/mcp/{slug}/` (both trailing-slash forms) using the owner bearer token.
- `GET /api/backup` streams a portable backup ZIP (optionally filtered with
  `?plugins=a,b`) and `POST /api/restore` imports one (multipart `file`,
  `mode=skip|replace`, optional `slugs=a,b`); see
  [Backup and restore](#backup-and-restore).

Auto-connect policy: `none`-auth plugins connect on install and startup, and a
previously selected stored auth mode reconnects on startup using already-stored
credentials. Connect failures keep the plugin `loaded` with a connection
`status` of `error`; they never mark it `load_failed`. An `oauth` plugin reports
`oauth_required` until its single-use callback completes the sign-in (see
[OAuth](#oauth)).

## Health checks and connection state

A background scheduler pings every **loaded** plugin whose persisted status is
`connected` or `auth_expired`; `not_connected`, `oauth_required`, `error` and
`load_failed` plugins are never probed. Cycles run every
`PING_INTERVAL_SECONDS` (default 60), never overlap, and ping plugins
concurrently so one hung worker does not block another. Each ping (and each
`auth_expired` refresh) is bounded by `PING_TIMEOUT_SECONDS` (default 10)
including any time spent waiting for a free per-plugin capacity slot, so a
plugin saturated by in-flight tool calls fails into a health observation at the
deadline instead of stalling the rest of the cycle.

- `connected` resets `fail_count` and `transient_error_count` and clears
  `last_error`.
- `error`, a timeout, a plugin exception or an overloaded host is **transient**:
  the status stays as it was and only `transient_error_count` increases. A
  transient failure never disconnects the plugin.
- `not_connected` is a **hard** failure: `fail_count` increases. An
  `auth_expired` result refreshes the plugin under its lifecycle lock; a
  `connected` refresh counts as success, anything else is a hard failure that
  eventually resolves to `auth_expired`.
- At `PING_FAIL_THRESHOLD` (default 3) hard failures set the status to
  `not_connected` (or `auth_expired` when the cause is a failed refresh).

Every attempt persists `last_ping_at` and the health metadata in
`core:plugin:{slug}:meta`, and status transitions and hard failures are logged.
A result is discarded if its runtime was replaced or the plugin was disconnected
while the ping was in flight, so health checks cannot undo a lifecycle action.

The dashboard is served at `/` to an authenticated browser session (see
[Dashboard](#dashboard)); an unauthenticated browser request is redirected to
`/login`.

## OAuth

Plugins declare `oauth` in `manifest.json` and expose the optional hooks
`get_oauth_sign_in_url`/`on_oauth_result`/`refresh`. The core owns the browser
redirect, the per-plugin state and PKCE; the plugin only builds the provider URL
and exchanges the code.

- **Connect.** `POST /api/plugins/{slug}/connect` with
  `{"mode":"oauth","credentials":{"client_id":"...","client_secret":"..."}}`.
  Both credentials are optional: an omitted/empty `client_id` reuses the stored
  `core.oauth_client_id`, and when none is stored the request is rejected with
  `422 invalid_request`. The client id/secret are stored as reserved `core.*`
  secrets before the `on_oauth_credentials_set` hook runs. The 200 detail is
  `oauth_required` and adds `redirect_url`: the absolute provider sign-in URL
  with the freshly created `state` inserted.
- **Sign-in.** The dashboard follows `redirect_url` to the provider. The plugin
  generated an RFC 7636 PKCE `code_verifier`, stored it in scoped **data** under
  `pkce:{state}` with a 600 second TTL, and sent the S256 `code_challenge`,
  `client_id`, `state` and `redirect_uri = {BASE_URL}/mcp/{slug}/oauth/callback`
  on the authorize request.
- **Callback.** The public, slug-bound route
  `GET /mcp/{slug}/oauth/callback?code=...&state=...` (also `error=...`) is
  registered before the `/mcp` catchall, so a provider redirect never needs the
  owner bearer. It validates and **consumes** `state` exactly once via Redis
  `GETDEL` (`core:oauth_state:{state}` = `{slug, created_at}`, `EX 600`), calls
  `on_oauth_result(code, state)` under the lifecycle lock, persists the status
  and re-lists tools. It then `303`-redirects **only** to the fixed relative
  `/#/plugins/{slug}?oauth=success|failed`; an unknown slug is `404`.
- **State safety.** The state is single-use and bound to its path slug: a
  missing, forged, replayed, expired or slug-mismatched state — and a
  provider-declared `error` — all end in `?oauth=failed` and can never change a
  plugin's status. The token exchange sends the same `redirect_uri` and the PKCE
  verifier; derived access/refresh tokens and their expiry are stored in scoped
  **secrets**.
- **Refresh.** The health scheduler treats `auth_expired` as a refresh
  opportunity: it calls `refresh()` under the plugin's lifecycle lock, which
  rotates the refresh token (the new refresh token is written before the new
  access token). A successful rotation returns the plugin to `connected`.
- **No secrets in JSON.** Credential values, the PKCE verifier, the authorization
  code and every access/refresh token are never returned by any API endpoint,
  placed in a redirect URL, or written to persisted logs; the dashboard shows
  credential **key names only**.

### Local mock provider

`tests/oauth_provider.py` is a real loopback OAuth 2.0 + PKCE server (authorize,
token exchange and refresh rotation). The `oauth_demo` example reads its provider
base URL from `CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL` at call time (default
`https://oauth.example.invalid`), so point that variable at the mock provider
before connecting to exercise the full flow locally:

```bash
# in tests: tests/integration/test_oauth.py drives the real app + callback route
uv run pytest -q tests/integration/test_oauth.py
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## Running

```bash
export MCP_SECRET="a-strong-secret-value"
export BASE_URL="https://mcp.example.com"
uv run custom-mcp
```

## Writing a plugin

A plugin is a ZIP whose root contains `manifest.json` and the entry module. It
uses the dependency-free SDK (`mcp_plugin_sdk`) and is loaded in-process, on
worker threads, as trusted code (see the trust-boundary warning below).

`manifest.json` fields:

| Field | Required | Rules |
| --- | --- | --- |
| `name` | yes | the slug: `^[a-z0-9][a-z0-9-]{0,39}$` (lowercase, hyphens, no underscores) |
| `version` | yes | `MAJOR.MINOR.PATCH`, ASCII digits, no leading zeros |
| `entry` | yes | `module:ClassName` (dotted module allowed; no Python keywords) |
| `auth_modes` | yes | non-empty subset of `none`, `api_key`, `oauth`; `none` cannot be combined |
| `display_name` | no | defaults to `name` |
| `description` | no | defaults to empty |
| `min_core_version` | no | `MAJOR.MINOR.PATCH`; must not exceed the core's version |
| `author`, `homepage` | no | free-form metadata |

The entry class subclasses `mcp_plugin_sdk.MCPPlugin` and implements the five
required methods — `auth_methods()`, `list_tools()`, `connect()`, `ping()` and
`invoke_tool(name, args)` — plus optional hooks such as `on_api_key_set`,
`on_oauth_result`, `refresh`, `on_restore`, `on_disconnect` and `shutdown`:

```python
from typing import Any

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, PluginContext, Status, Tool, ToolResult


class ExamplePlugin(MCPPlugin):
    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="echo",
                description="Echo the input text.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name != "echo":
            return ToolResult.failure("not_found", f"Unknown tool {name!r}.")
        return ToolResult.success({"text": args["text"]})
```

Authoring rules enforced by the host:

- `input_schema` must be a JSON Schema object that explicitly declares
  `type: object`; it is validated exactly as supplied (no rewriting), and
  arguments are checked against it before `invoke_tool` runs.
- Each tool schema must be a **fresh** object — do not share mutable nested
  defaults between `Tool` instances.
- `ctx.storage` / `ctx.secrets` are scoped to the slug and hold JSON values;
  keys beginning `core.` are readable but not writable by plugin code. `ttl`
  marks a value transient (excluded from backups).
- Return structured failures with `ToolResult.failure(code, message,
  retryable=False)`; the code and message are redacted against the plugin's
  stored secrets before they reach a client.
- Set your own HTTP client timeouts. The host bounds each call
  (`TOOL_TIMEOUT_SECONDS`, `PLUGIN_CALL_TIMEOUT_SECONDS`) and enforces
  `PER_PLUGIN_CONCURRENCY`, but it cannot kill a hung worker thread.

Build and install the ZIP (standard library only):

```bash
cd plugin_examples/hello
python - <<'PY'
import zipfile
from pathlib import Path

with zipfile.ZipFile("hello.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "plugin.py"):
        archive.write(name, Path(name))
PY
```

Then import it through the dashboard or `POST /api/plugins/import`, and call it
at `{BASE_URL}/mcp/{slug}/` with the owner bearer token. Complete, tested
examples live in `plugin_examples/`:

- [`hello`](plugin_examples/hello/README.md) — auth mode `none`, `echo`/`add`
  plus a persisted counter;
- [`apikey_demo`](plugin_examples/apikey_demo/README.md) — auth mode `api_key`,
  `whoami` and a key-gated `fetch_item` mapping `401` to `auth_expired`;
- [`oauth_demo`](plugin_examples/oauth_demo/README.md) — auth mode `oauth`,
  PKCE sign-in, token exchange and refresh rotation.

The exact SDK signatures are in [SDK_CONTRACT.md](SDK_CONTRACT.md).

## Deployment

The repository ships a non-root `Dockerfile` and a `docker-compose.yml` with a
private, AOF-persistent Redis. The full operator walkthrough — build, Compose,
an HTTPS nginx reverse proxy with MCP streaming buffering disabled, volumes,
backup, upgrades and logs — is in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

```bash
docker build -t custom-mcp .
cp .env.example .env          # set MCP_SECRET and BASE_URL
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/healthz   # {"status":"ok"}
```

`MCP_SECRET` (at least 16 characters) and `BASE_URL` (a bare `http(s)` origin)
are required; the service fails fast at startup without them. The health
endpoint is `GET /healthz`.

**Before going to production, note these explicitly:**

- Redis with no persistence **loses all credentials and sessions on restart**;
  the Compose volume `redis-data` enables AOF to prevent that.
- Plugins are **trusted in-process code** and can read the environment
  (including `MCP_SECRET`). Only install plugins you trust.
- Backups produced by `/api/backup` are **PLAINTEXT** and contain credentials —
  treat the file like a password.
- **NEVER expose Redis publicly.** It has no auth configured by the app; keep it
  on the private network.
- **HTTPS is required outside local use**; a reverse proxy must terminate TLS and
  disable buffering for MCP streams.

The security checklist mapping each control to its implementation and tests is in
[docs/SECURITY.md](docs/SECURITY.md).

## Dashboard

Open the dashboard in a browser at `/`. An unauthenticated browser request to
`/` is answered by the server with a redirect to `/login`, where you sign in with
the owner secret (the `MCP_SECRET` value). After signing in you land on the
dashboard at the `#/` route. The page is plain HTML/CSS/vanilla JavaScript
served from `/static` with no build step, and every view is rendered from
same-origin JSON endpoints using DOM APIs only.

### Plugin grid

The grid lists every installed plugin (sorted by slug) from `GET /api/plugins`
and shows the display name, version, load state, connection status and the
current tool count per row (the tool names and schemas are shown in the detail
dialog). A summary strip counts the plugins and flags the
ones that need **attention** (a failed load, or an unhealthy connection status
such as `error` or `auth_expired`). With no plugins installed the grid shows an
empty state; a failed request keeps the previous view and shows an error banner.

Use the import control to upload a plugin ZIP: the browser POSTs the file to
`POST /api/plugins/import`. If the package's slug is already installed the
import is rejected with `plugin_exists`, and the dashboard prompts you to
confirm a replacement before retrying with `mode=replace`.

### Plugin detail

Selecting a row opens the detail dialog (also addressable directly as
`#/plugins/{slug}`), which renders `GET /api/plugins/{slug}`:

- the manifest (slug, display name, version, description, entry, declared auth
  modes) plus load, status and auth information;
- the MCP **endpoint URL** (`{BASE_URL}/mcp/{slug}/`) and the OAuth **callback
  URL**, each with a copy button;
- every tool with its pretty-printed JSON `input_schema`;
- credential **key names only** — never values;
- packaging/lint warnings; and
- the newest 50 redacted log entries, with a refresh button.

### Connect and lifecycle actions

- **Connect** is mode-aware. `none` needs no credentials and connects
  immediately; `api_key` asks for the key in a field and submits
  `{"mode":"api_key","credentials":{"api_key":"..."}}`; `oauth` asks for an
  optional client id and client secret and then redirects the browser to the
  provider to sign in (see [OAuth](#oauth)).
- **Disconnect** runs the plugin's disconnect hook, wipes its stored secrets and
  resets its connection status.
- **Reload** re-extracts the stored package, unloads the old runtime and loads a
  fresh generation.
- **Remove** deletes the plugin after a destructive confirmation.

### Security

The browser never stores credentials: the only state it persists is the
server-issued `HttpOnly` session cookie. Secret values are never sent to or
rendered by the dashboard — the detail view lists credential **key names only**,
and plugin logs and diagnostics are redacted server-side.

### Backup and restore

The **Settings** area backs up and restores the whole install.

> **The backup ZIP is a secret.** It contains every plugin's stored credentials
> and the reserved `core.*` secrets in **plaintext**, so treat the file like a
> password: store it encrypted and never commit or share it.

**Download.** Tick the plugins to include (with none ticked, everything is
exported) and click **Download backup**. The browser navigates to `/api/backup`
(adding `?plugins=a,b` when a subset is ticked), which answers with an
attachment ZIP named `custom-mcp-backup-<utc>.zip`. Each ZIP contains:

```text
backup.json                       # format_version, created_at, core_version, plugins
plugins/{slug}/plugin.zip         # byte-identical to the installed package
plugins/{slug}/data.json          # the plugin's stored data values
plugins/{slug}/secrets.json       # stored secrets, including reserved core.* keys
plugins/{slug}/meta.json          # {"auth_mode": ..., "version": ...}
```

Keys with a TTL (transient values) are **excluded** from `data.json` and
`secrets.json`, so a backup never resurrects expired state.

**Restore.** Choose the backup `.zip`, pick a mode, optionally type a
comma-separated slug filter, and click **Restore**. The browser POSTs multipart
`file` to `/api/restore?mode=...&slugs=...`:

- **skip** (the default) leaves an already-installed plugin untouched;
- **replace** replaces an existing plugin with a clean, exact restore of the
  backup's code, data and secrets (the plugin is restored as `not_connected` and
  then reconnects according to its stored auth mode).

The results area lists every plugin's outcome (**restored** / **skipped** /
**failed**), its resulting connection status and any detail, plus a
restored/skipped/failed count. Invalid or encrypted archives are rejected with
`422` and nothing on disk is changed; after a successful restore the plugin grid
refreshes.
