# Custom MCP implementation contract

This repository implements the owner's supplied Architecture & Build
Specification. That specification is authoritative; this file summarizes the
cross-phase contracts so delegated work does not accidentally diverge.

## Scope and boundaries

Single owner, single instance, trusted in-process synchronous Python plugins.
Python 3.11+, Starlette/Uvicorn, exactly pinned FastMCP, persistent Redis >=6.2,
`requests`, `jsonschema`, `python-multipart`. Dashboard is static HTML/vanilla
JavaScript/CSS with no Node/build step. Never sandbox or introduce multi-tenancy
as part of the MVP. Backups and Redis credentials are plaintext in the MVP.

Packages are independent ZIPs exposing one stateless streamable-HTTP MCP server
per slug at `/mcp/{slug}/`; both trailing-slash forms work. Tools remain listed
for loaded plugins even when disconnected. Calls while disconnected return:

```text
[not_connected] Plugin '{slug}' is not connected. Reconnect it in the dashboard.
```

Authentication is exclusively in `auth/`, through a swappable `Authenticator`
ABC and pure ASGI middleware. Other handlers read `request.state.principal`.
Only healthz, login, static assets, and slug-bound OAuth callbacks are public.
MCP uses owner bearer; dashboard uses Redis sessions; cookie unsafe requests
require a matching Origin or Referer. Secret comparison uses `hmac.compare_digest`
on bytes. No credentials in browser persistence, API JSON, or logs.

## SDK

Top-level dependency-free `mcp_plugin_sdk` exports `MCPPlugin`, `PluginContext`,
`Storage`, `AuthMode`, `Status`, `Tool`, `ConnectResult`, `ToolError`, `ToolResult`.
Auth modes: `none`, `api_key`, `oauth`. Connection statuses: `connected`,
`not_connected`, `oauth_required`, `auth_expired`, `error` (transient).

Tool is a frozen dataclass: name, description, input_schema (default empty object
schema). Names match `[a-zA-Z0-9_-]{1,64}`, unique per plugin, schema must explicitly
have `type: object` and pass Draft202012Validator.check_schema. ToolResult has
`ok`, optional dictionary `result`, optional ToolError (`code`, `message`,
`retryable`) and success/failure constructors. ConnectResult has status, optional
message and redirect_url. Context is frozen: slug, scoped storage/secrets,
log(level,message), callback_url. Storage provides get/set/delete/list_keys.

Required synchronous plugin methods: auth_methods, list_tools, connect, ping,
invoke_tool. Optional hooks: on_api_key_set, on_oauth_credentials_set,
get_oauth_sign_in_url, on_oauth_result, refresh (default AUTH_EXPIRED), on_restore,
on_disconnect, shutdown. No raw Redis or core settings passed to plugin code.

## Disk and Redis

`DATA_DIR/packages/{slug}.zip` is source of truth. Regenerate extracted
`DATA_DIR/plugins/{slug}/` on every startup. Scratch is `DATA_DIR/tmp/`, cleaned
on startup. Preserve original package bytes in backups and rollback.

Redis JSON string namespaces:

- `plugin:{slug}:data:{key}` and `plugin:{slug}:secret:{key}` (optional TTL).
- `core:plugin:{slug}:meta`: version, installed_at, selected auth_mode or null,
  status, fail_count, transient_error_count, last_ping_at, last_error.
- `core:plugins` installed slug set.
- `core:logs:{slug}` newest-first list, LPUSH/LTRIM capped at 200.
- `core:oauth_state:{state}` slug/created_at JSON, EX600, GETDEL single-use.
- `core:session:{id}` session JSON with configured lifetime.

Storage keys match `[A-Za-z0-9_.:-]{1,128}`, serialized JSON values <=1MiB.
Plugin code can read, not set/delete, keys starting `core.`. Core credentials are
`core.api_key`, `core.oauth_client_id`, optional `core.oauth_client_secret`;
write before calling credential hooks. Plugin tokens use nonreserved names.
Use SCAN, never KEYS. Any TTL key is transient and excluded from backups.
Before persisted logs, redact all current string secrets of length >=6 to `***`.

## Packages and runtime

Manifest: slug `name` matching `[a-z0-9][a-z0-9-]{0,39}`, display_name defaults
name, description defaults empty, semantic MAJOR.MINOR.PATCH version, dotted
`module:ClassName` entry, nonempty auth_modes subset; none cannot coexist with
others. Optional min_core_version must not exceed installed core version.

ZIP root contains manifest.json or one enclosing top-level directory to strip.
Validate all entries before writing: compressed <=configured plugin limit,
uncompressed <=5x, <=2000 entries, entry ratio <=100:1; reject absolute paths,
dot-dot segments, drive letters, backslashes, NUL, symlinks/special files and any
resolved destination outside scratch. Validate manifest before live publication.
AST import lint warns for imports outside stdlib, requests/dependencies, SDK,
and package-local modules. Warnings never reject otherwise valid imports.

Load into `mcp_plugin_{slug}` isolated package, supporting relative imports.
Verify entry subclass and manifest/SDK auth equality. Host mediates every hook:
shared ThreadPoolExecutor, per-plugin semaphore, timeouts, normalized exceptions
and malformed returns. Track timed-out running workers; if leaked count reaches
per-plugin concurrency, fail fast plugin_overloaded until workers complete.
Workers cannot be killed; plugin authors must use HTTP timeouts.

Runtime holds manifest, loading/loaded/load_failed/unloading, load_error,
instance, cached tools, scoped context, semaphore, stable lifecycle lock and
mounted app. Failed plugin remains visible/removable and does not block others.
Shutdown hook gets 5s, then remove isolated package sys.modules entries.

Install reject mode conflicts409. Replace retains old files/state, unloads old,
loads new, rolls back on load failure, preserves credentials/data/auth mode on
success, reconnects. Reload re-extracts stored package. Remove unmounts, shutdowns
(not on_disconnect), deletes only target code/state/logs/registry. Disconnect
calls on_disconnect, wipes all secrets, sets not_connected and auth_mode=null.
Lifecycle operations serialize. Tools/pings use semaphore, not lifecycle lock.

FastMCP adapter must preserve raw inputSchema exactly, validate arguments
explicitly with jsonschema, map successful dictionaries to structured content
and JSON text, errors to `[code] message` (+ ` (retryable)` when applicable).
Dynamic ASGI router enters each mounted lifespan, atomically swaps lookup, drains
retired app ~5s before closing lifespan, rewrites path/root_path correctly.
OAuth callback Starlette route precedes catchall MCP dispatch.

**Verified pinned-library constraint:** FastMCP 2.14.7's lifespan owns AnyIO
cancel scopes and context-variable tokens. It must be entered and exited in the
same task/context. The specification's illustrative cross-task exit-stack
retirement cannot be used literally. Each mounted app needs a dedicated lifespan
owner task: enter its AsyncExitStack, signal readiness, await retirement/drain,
then exit the stack in that same task. Mount/swap/unmount coordinate with this
task without entering its lifespan themselves. Add lifecycle regression tests.

## REST and UI

All `/api/*` have Cache-Control:no-store. JSON errors:
`{"error":{"code":"snake_case","message":"safe human description"}}`.
Unhandled exceptions -> generic500 internal_error; server diagnostics retained.

Routes: GET plugins list/detail/logs; POST import multipart file with
mode=reject|replace; DELETE plugin; POST connect/disconnect/reload; GET backup
optional plugins=a,b; POST restore multipart file mode=skip|replace optional
slugs=a,b; public OAuth callback; login/logout/session; dashboard/static/healthz.
Details expose manifest, load/status/auth info, tools with schemas, credential
key names only, endpoint/callback URLs, lint warnings and last50 redacted logs.

Static SPA uses textContent/DOM APIs, not innerHTML with server data. CSP:
default-src 'self'; frame-ancestors 'none', nosniff. Dashboard counts and attention;
plugin grid/import replacement prompt; detail dialog URLs/copy, tools/schema,
credential names, warnings/log refresh, lifecycle actions; mode-aware connect
dialog; settings backup warning/select/export, restore modes/results/version.

## Backup

Deflated ZIP built using SpooledTemporaryFile then streamed. backup.json contains
format_version1, UTC created_at, core_version, encrypted=false, plugin entries
slug/version/has_secrets. Each `plugins/{slug}/` has byte-identical plugin.zip,
data.json and secrets.json flat key/value objects (including reserved core keys),
meta.json with auth_mode/version. Snapshot each plugin under lifecycle lock.
Dashboard warns prominently: contains plaintext credentials, treat as password.

Restore A validates complete outer ZIP and all listed packages/JSON/key/value
schemas without modifying live disk/Redis; encrypted backup unsupported422.
Restore B applies independently: skip existing or transactional replace, install
without auto-connect, wipe replaced data/secrets for exact snapshot, restore no
TTL, status not_connected, on_restore nonfatal, reconnect and persist status.
Valid archive ->200 per-plugin restored/skipped/failed results and connection;
invalid archive ->422 and no changes.

## Health and OAuth

Concurrent periodic pings only for loaded CONNECTED/AUTH_EXPIRED; no overlapping
cycles. CONNECTED resets both counts/error. ERROR/timeout/exception transient:
keep status, increment transient count only. NOT_CONNECTED hard failure.
AUTH_EXPIRED refreshes under lock; CONNECTED resets; otherwise hard failure.
At hard threshold set not_connected or auth_expired per cause. Persist last_ping,
all metadata and transitions/logs. Do not disconnect on network/5xx failures.

Connect validates supported mode, stores selected mode and supplied credentials,
calls hooks then connect. OAuth omitted client_id reuses stored one else422.
OAUTH_REQUIRED creates state then gets sign-in URL or inserts/validates state in
plugin redirect_url. Callback validates/consumes state bound to path slug,
calls on_oauth_result under lock, persists/re-lists tools if connected, redirects
only to `/#/plugins/{slug}?oauth=success|failed`. Provider errors need valid state.
PKCE verifier in data `pkce:{state}` TTL600. Token exchange uses callback_url;
derived tokens/expiry in secrets. OAuth demo talks to mock provider in tests.

## Release

Three examples: hello (echo/add/counter, none), apikey_demo (whoami/fetch_item,
core.api_key, upstream401 expired/network5xx transient), oauth_demo (PKCE/token
exchange/refresh). Each has manifest, plugin.py, short author README.
Non-root Docker, app persistent DATA_DIR, Redis AOF named volume and private
network; single instance behind HTTPS reverse proxy with MCP buffering disabled.
README explicitly warns Redis without persistence loses credentials on restart,
trusted code can read environment, backups plaintext, no public Redis, HTTPS
required outside local use. CI full pytest/fake and isolated real Redis tests,
ruff and type checks. Startup validates settings/Redis, cleans scratch, reextracts,
loads/mounts/reconnects, starts scheduler. Shutdown stops scheduler, shuts plugins,
closes mounted lifespans then Redis.
