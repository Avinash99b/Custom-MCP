# Security model and checklist

Custom MCP exposes hot-swappable ZIP plugins as streamable-HTTP MCP endpoints
behind a single owner secret. This document maps every security control that the
code actually implements to the file that implements it and the test that
exercises it. Every claim below was read from the source; where a control has no
test because it is architectural, that is stated explicitly.

See the end of this document for **Accepted MVP limitations** — the honest list
of non-goals, including the plugin trust boundary, which is the single most
important thing an operator must understand.

## Trust model in one paragraph

There is exactly one authenticated principal (the owner) and one running
instance. Plugins are **trusted in-process Python code**: they run in the same
process as the core, with the same OS permissions, and can read the process
environment (including `MCP_SECRET`, `REDIS_URL` and any other secret). The
packaging and storage layers protect the *core's* state and the host filesystem
from a malformed or buggy package, but they do not sandbox a malicious plugin.
Never install a plugin you do not trust.

## Control checklist

| # | Control | Implemented in | Tested in |
|---|---------|----------------|-----------|
| 1 | Public-route allowlist (only `/healthz`, `/login`, `/static/*`, public OAuth callback are unauthenticated) | `src/custom_mcp/auth/middleware.py` (`_PUBLIC_EXACT`, `_OAUTH_CALLBACK_RE`, `is_public_path`); route order in `src/custom_mcp/main.py` (`_routes`) | `tests/unit/test_public_paths.py`; `tests/integration/test_app.py::test_login_page_is_public`, `::test_oauth_callback_is_public` |
| 2 | Bearer-only MCP (a session cookie never authenticates an MCP request) | `src/custom_mcp/auth/middleware.py` (`_is_protected_mcp` + `method != "bearer"` → 401) | `tests/integration/test_app.py::test_mcp_rejects_cookie_only`, `::test_mcp_accepts_valid_bearer` |
| 3 | Cookie CSRF: unsafe methods need a matching `Origin`, else a matching `Referer` | `src/custom_mcp/auth/middleware.py` (`UNSAFE_METHODS`, `_csrf_ok`, `normalize_origin`) | `tests/unit/test_origin.py`; `tests/integration/test_app.py` CSRF block (`::test_cookie_unsafe_request_without_origin_is_forbidden`, `::test_cookie_unsafe_request_wrong_origin_is_forbidden`, `::test_mismatching_origin_rejected_even_with_valid_referer`, `::test_null_origin_is_rejected`, `::test_referer_fallback_only_used_without_origin`, `::test_bearer_skips_csrf`, …) |
| 4 | Constant-time secret comparison (UTF-8 bytes, Unicode-safe), exact — no trimming | `src/custom_mcp/auth/secret.py` (`SecretAuthenticator.verify_login_secret`, `_verify_bearer`, `extract_bearer_token`, all `hmac.compare_digest`) | `tests/unit/test_secret.py` (`::test_verify_login_secret_constant_time_compare`, `::test_unicode_bearer_does_not_raise`, `::test_bearer_token_is_compared_without_stripping`, `::test_secret_with_surrounding_spaces_compares_exact`) |
| 5 | Login rate limiting (per peer IP, fixed window, monotonic clock) | `src/custom_mcp/auth/ratelimit.py` (`LoginRateLimiter`, 5 failures / 60 s, bounded map); used in `src/custom_mcp/api/auth.py` (`login`) | `tests/unit/test_ratelimit.py`; `tests/integration/test_app.py::test_login_rate_limit_after_five_failures`, `::test_login_rate_limit_ignores_spoofed_forwarding_headers`, `::test_login_success_clears_failure_counter` |
| 6 | Hardened session cookie (`HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` when HTTPS) | `src/custom_mcp/auth/sessions.py` (`_build_cookie`, `SessionService.build_cookie`); `Secure` derived from `BASE_URL` scheme by `src/custom_mcp/config.py` (`COOKIE_SECURE`) | `tests/unit/test_sessions.py::test_build_cookie_is_hardened`, `::test_clear_cookie_expires_immediately`; `tests/integration/test_app.py::test_login_success_sets_hardened_cookie`; `tests/unit/test_config.py` |
| 7 | Security headers (`nosniff`, CSP `default-src 'self'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer`) on every response; `Cache-Control: no-store` on APIs, MCP, health/login/logout and all JSON | `src/custom_mcp/api/errors.py` (`_SECURITY_HEADERS`, `security_headers`, `error_response`); injected by `src/custom_mcp/auth/middleware.py` (`_forward`, `_should_no_store`, `_message_is_json`) | `tests/integration/test_app.py::test_security_and_security_headers_on_api`, `::test_static_assets_are_public_with_csp`, `::test_healthz_ok`, `::test_unauthenticated_api_returns_401`, `::test_unhandled_exception_is_generic_500`; `tests/integration/test_dashboard.py` |
| 8 | Secret redaction before logging, in tool descriptions, in API JSON and in tool-call errors | `src/custom_mcp/plugins/logs.py` (`PluginLogs`, `RETAINED_CAP=64`, `pkce:` data redaction); `src/custom_mcp/plugins/manager.py` (`_refresh_tools`, `_redact_description`, `_redact`, `retain_secrets`); `src/custom_mcp/mcp/adapter.py` (`_PluginTool._redact_failure`, fails closed); `src/custom_mcp/api/errors.py` (generic 500); `src/custom_mcp/logging.py` (bodies never logged) | `tests/unit/test_plugin_logs.py`, `tests/unit/test_logging.py`; `tests/integration/test_phase4_containment.py`, `tests/integration/test_phase4_containment_2.py`; `tests/integration/test_phase8_containment.py` |
| 9 | ZIP validation before any file is written (traversal, absolute/drive/backslash/NUL paths, symlinks/special files, zip bombs via declared-vs-actual size + CRC, entry count, per-entry 100:1 and total 5× ratios, duplicate destinations, file/dir conflicts) | `src/custom_mcp/plugins/packaging.py` (`validate_package`, `_normalize_entry_path`, `_validate_entry_metadata`, `_decode_entry`, `_verify_entries`, `_check_conflicts`, `extract_package`) | `tests/unit/test_packaging.py`; `tests/unit/test_packaging_security_cases.py`; `tests/security_cases/test_archive_cases.py` |
| 10 | Scoped plugin storage + reserved `core.*` guard (plugins get only their own namespaced view; `core.` keys readable but not writable by plugin code) | `src/custom_mcp/storage/manager.py` (`ScopedStorage`, `_StorageFacade`, `_guard_reserved`, `RESERVED_PREFIX`, `set_core_secret`) | `tests/unit/test_storage.py::test_reserved_read_allowed_write_denied`, `::test_allow_reserved_bypass`, `::test_namespace_isolation_between_slugs`, `::test_data_and_secret_namespaces_separate`, `::test_set_core_secret_requires_core_prefix` |
| 11 | Sealed retired plugin generations (a leaked worker from an old timing-out call cannot recreate a wiped secret or log a cached credential) | `src/custom_mcp/plugins/seal.py` (`WritePermit`, `retired_writes_allowed`); `src/custom_mcp/storage/manager.py` (`_StorageFacade._sealed`); `src/custom_mcp/plugins/manager.py` (`_retire_seal`, `_sealed_log`) | `tests/integration/test_phase4_containment.py::test_timed_out_key_hook_late_worker_is_contained`; `tests/integration/test_phase4_containment_2.py` (`::test_reconnect_after_disconnect_contains_leaked_worker`, `::test_shutdown_cannot_repersist_wiped_secret`, `::test_sealed_shutdown_log_cannot_leak_cached_credential`, `::test_timed_out_shutdown_worker_cannot_recreate_removed_state`, `::test_disconnect_redacts_cached_credential_in_descriptions`) |
| 12a | Single-use, slug-bound OAuth state (`GETDEL`, `EX 600`) | `src/custom_mcp/plugins/manager.py` (`create_oauth_state`, `consume_oauth_state`, `complete_oauth`); public route in `src/custom_mcp/api/oauth.py` | `tests/unit/test_oauth_backend.py` (`::test_create_oauth_state_stores_slug_and_expiry`, `::test_consume_oauth_state_is_single_use`, `::test_consume_oauth_state_rejects_slug_mismatch`); `tests/integration/test_oauth.py` (`::test_callback_state_is_single_use`, `::test_forged_state_never_calls_the_provider_or_mutates_status`, `::test_state_is_bound_to_the_callback_slug`, `::test_expired_or_deleted_state_fails_without_mutation`) |
| 12b | PKCE S256 verifier held in plugin data `pkce:{state}` with a 600 s TTL, retained for redaction | `src/custom_mcp/plugins/manager.py` (`_retain_pkce_verifier`); redacted by `src/custom_mcp/plugins/logs.py` (`_PKCE_DATA_PREFIX`) | `tests/integration/test_oauth.py::test_full_pkce_happy_path_connects_lists_me_and_leaks_nothing`; `tests/integration/test_phase8_containment.py::test_pkce_verifier_is_redacted_from_logs`; `tests/unit/test_oauth_demo.py` |
| 13 | Plugin trust boundary (plugins are trusted code that can read the environment) | Architectural: `ARCHITECTURE.md` ("Scope and boundaries"); `src/custom_mcp/plugins/loader.py` loads plugin code in-process | **Not testable** — a deliberate documented non-goal (see limitations) |

## Control details

### 1. Public-route allowlist

`is_public_path` in `src/custom_mcp/auth/middleware.py` is an exact-match
allowlist: `/healthz`, `/login`, `/static` and `/static/…`, and a single regex
`^/mcp/[a-z0-9][a-z0-9-]{0,39}/oauth/callback$`. Everything else — including `/`,
`/logout`, `/api/*`, `/mcp`, and `/mcp/{slug}` — requires authentication. There
are no prefix surprises: `/loginx`, `/healthz/`, `/staticx` and
`/mcp/DEMO/oauth/callback` are all rejected as public (`tests/unit/test_public_paths.py`).
The public OAuth callback is itself slug-bound, and an unknown slug returns a
uniform JSON `404` (`src/custom_mcp/api/oauth.py`).

### 2. Bearer-only MCP

After authentication, any path under `/mcp` requires the `bearer` method
(`src/custom_mcp/auth/middleware.py`). A valid dashboard cookie authenticates the
dashboard but is rejected with `401` (and `WWW-Authenticate: Bearer`) for MCP
(`tests/integration/test_app.py::test_mcp_rejects_cookie_only`). The router
(`src/custom_mcp/mcp/router.py`) never re-implements authentication; it only
dispatches already-authenticated requests.

### 3. Cookie CSRF (Origin / Referer)

For cookie-authenticated (non-bearer) `POST`/`PUT`/`PATCH`/`DELETE`, the
middleware requires the request `Origin` to equal `BASE_URL` exactly; if `Origin`
is absent it falls back to `Referer`. An explicit but mismatching `Origin` is
rejected even when a valid `Referer` is present, and `Origin: null` is rejected.
`normalize_origin` lowercases scheme/host and applies scheme-default ports so
`https://host` and `https://host:443` compare equal. Bearer requests are exempt
because the token is not ambient — a browser will not attach it automatically
(`::test_bearer_skips_csrf`).

### 4. Constant-time secret comparison

`SecretAuthenticator` compares the candidate against the configured secret with
`hmac.compare_digest` over UTF-8 bytes, for both the login secret and the bearer
token. The bearer candidate is used byte-for-byte after removing only the single
separator space, so leading/trailing whitespace and double spaces do not match. A
non-ASCII candidate is encoded as UTF-8 and therefore cannot raise
(`tests/unit/test_secret.py::test_unicode_bearer_does_not_raise`).

### 5. Login rate limiting

`LoginRateLimiter` counts failed logins per IP in a fixed window (5 failures /
60 s) using a monotonic clock, and bounds the number of tracked IPs. The IP is
taken from `request.client` (the transport peer), never from
`X-Forwarded-For`, so the limit cannot be bypassed by a spoofed header
(`::test_login_rate_limit_ignores_spoofed_forwarding_headers`). Blocked logins
get `429` with `Retry-After`; a successful login clears the counter.

> **Proxy note.** Because the peer IP is used deliberately, running behind a
> reverse proxy that does not preserve the client address (or that this app does
> not trust for `X-Forwarded-For`) means all logins share one rate-limit bucket.
> See `docs/DEPLOYMENT.md`.

### 6. Hardened session cookie

`_build_cookie` sets `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age`, and
`Secure` when sessions are configured secure. `SessionService` is constructed
with `cookie_secure=settings.COOKIE_SECURE`, which defaults to `BASE_URL`'s
scheme being `https` (`src/custom_mcp/config.py`). Sessions are random
(`secrets.token_urlsafe(32)`), server-side in Redis under `core:session:{id}`
with an absolute TTL, and are revoked on logout.

### 7. Security headers and `no-store`

`_SECURITY_HEADERS` in `src/custom_mcp/api/errors.py` defines
`X-Content-Type-Options: nosniff`,
`Content-Security-Policy: default-src 'self'; frame-ancestors 'none'` and
`Referrer-Policy: no-referrer`. `AuthMiddleware._forward` adds these to the
response-start of *every* HTTP response (public and authenticated), and sets
`Cache-Control: no-store` when the path is `/healthz`, `/login`, `/logout`, an
`/api`/`/mcp` path, or the response is JSON. Error responses set `no-store`
unconditionally. The static SPA contains no inline script (`<script src=…` only),
so the `default-src 'self'` policy needs no nonce.

### 8. Secret redaction

Redaction happens in four places, each covered by tests:

- **Logs.** `PluginLogs.write_sync`/`write` redact every message and metadata
  tree against the slug's current secret string leaves (and `pkce:` data values)
  *before* the entry is written to Redis or echoed to stdout. Rotated or
  data-stored secrets stay redacted via a bounded retained set
  (`RETAINED_CAP = 64`, oldest out).
- **Tool descriptions.** `PluginManager._refresh_tools` / `_redact_listed_tools`
  redact plugin-supplied descriptions — including the loader's initial listing, so
  a description is redacted *before* it is ever published to `GET
  /api/plugins/{slug}` — and fail closed to `"Redacted."` if the redactor is
  unavailable, so a credential a plugin cached in memory cannot leak through
  `list_tools` or the detail API.
- **Tool-call errors.** `_PluginTool._redact_failure` in
  `src/custom_mcp/mcp/adapter.py` captures the slug's secrets at the start of a
  call and redacts the whole rendered failure, degrading to a generic
  `[internal_error]` message if capture or redaction fails.
- **API JSON / errors.** `PluginManager._redact` fails closed to `None`; the
  generic `500` handler returns a fixed message with no internals; and
  `src/custom_mcp/logging.py` logs only method/path/status/duration — never
  request bodies or credentials.

OAuth-specific redaction (encoded secret forms and a decode ladder, with the
public `client_id` deliberately exempted) lives in
`PluginManager._redact_url` and is exercised by
`tests/integration/test_phase8_containment.py`.

### 9. ZIP validation

`validate_package` (`src/custom_mcp/plugins/packaging.py`) is read-only and
distrusts declared sizes: each entry's raw compressed range is inflated with a
bounded, chunked decompressor, and the *actual* expanded byte count and CRC32 are
compared to the declared values. It rejects absolute/`//` paths, backslashes,
NUL bytes, Windows drive paths, `.`/`..` segments, symlinks and special files,
encrypted entries, unsupported compression, duplicate destinations, file/dir
conflicts, too many entries (> 2000), and both the per-entry 100:1 and total 5×
expansion ratios. `extract_package` re-validates every path, refuses a symlinked
or non-empty destination, and checks each resolved target stays inside the
destination. The malicious-fixture corpus in `tests/security_cases/` proves each
case is genuinely hostile by re-reading raw ZIP header bytes.

### 10. Scoped storage and the reserved `core.*` guard

Plugins are handed a `ScopedStorage` view bound to `plugin:{slug}:data:*` or
`plugin:{slug}:secret:*`; they never receive a Redis client. Keys beginning
`core.` are readable but not writable by plugin code
(`_guard_reserved` raises `PermissionError`); only the core writes them via
`StorageManager.set_core_secret`. Values are strict JSON (no `NaN`/`Infinity`),
capped at 1 MiB; listing uses `SCAN`, never `KEYS`, and re-filters to the
namespace prefix. Replacements run against a detached copy-on-write snapshot so
a failed candidate cannot mutate live credentials.

### 11. Sealed retired generations

When a runtime is retired (disconnect, reload, replace, remove), its `seal` is
set. `_StorageFacade._sealed` then makes `set`/`delete` silent no-ops and blocks
the secret namespace entirely, and `_sealed_log` suppresses that generation's
logs — so a worker leaked from an earlier timed-out call cannot recreate a wiped
secret or emit a credential it cached before the wipe. A deliberate `shutdown`
hook may still checkpoint *data* via a thread-scoped, revocable
`WritePermit` (`src/custom_mcp/plugins/seal.py`), which never unblocks the secret
namespace. A reconnect never clears a seal; it rebuilds a fresh generation.

### 12. OAuth state and PKCE

`connect` with `oauth` creates `secrets.token_urlsafe(32)` state stored as
`core:oauth_state:{state}` = `{slug, created_at}` with `EX 600`. The public
callback (`src/custom_mcp/api/oauth.py`) consumes the state exactly once via
Redis `GETDEL` and accepts it only when the stored `slug` equals the path slug;
a missing, forged, replayed, expired or mismatched state, and any
provider-declared `error`, end in a `303` to the fixed relative
`/#/plugins/{slug}?oauth=failed` and never change plugin status. Success redirects
only to the fixed relative `…#oauth=success`; an unknown slug is `404`. PKCE
uses an S256 `code_verifier` stored in plugin **data** at `pkce:{state}` with a
600 s TTL; the token exchange sends the same `redirect_uri` and the verifier.
Credential values, the verifier, the authorization code and every access/refresh
token are never returned by an API, placed in a redirect URL, or written to
persisted logs.

### 13. Plugin trust boundary

This is the control with no code to point at, because it is a scope decision.
`ARCHITECTURE.md` states the MVP is "single owner, single instance, trusted
in-process synchronous Python plugins" and "never sandbox". `PluginLoader`
imports plugin code into the same process and event loop/worker pool; a plugin
therefore shares the core's OS permissions, memory and environment. Packaging
and storage protect the *core* from a malformed package and keep plugins from
reading each other's namespaced storage, but they are not a security boundary
against a hostile plugin. **Only install plugins you trust.**

## Accepted MVP limitations

These are deliberate non-goals, grounded in the source and `ARCHITECTURE.md`.
They are not bugs; treat them as operational constraints.

- **Plugins are trusted in-process code.** No sandbox, filesystem jail or
  per-plugin OS isolation. A plugin can read `MCP_SECRET`, `REDIS_URL`, `DATA_DIR`
  and every other environment variable, import any module present in the runtime
  (the ZIP AST lint only *warns* about unrecognized imports — it never rejects
  them), open network connections, and read/write files the runtime user can
  reach. There is no multi-tenancy. (`ARCHITECTURE.md` "Scope and boundaries";
  `packaging._lint` warnings are non-fatal.)
- **Single instance only.** Session storage is in Redis, but the login rate
  limiter and the plugin host/runtime registry are process-local
  (`src/custom_mcp/auth/ratelimit.py` docstring; `PluginHost`/`PluginManager` are
  per-process). Running two app replicas against one Redis is not a supported
  configuration for rate limiting or plugin lifecycle coherence.
- **Backups are plaintext.** `src/custom_mcp/backup.py` builds a ZIP containing
  `secrets.json` with reserved `core.*` credentials in readable JSON and records
  `"encrypted": false`; encrypted archives are never produced and are rejected on
  restore. Treat a backup exactly like a password.
- **Redis must be private and trusted.** The application configures no Redis
  authentication or TLS and treats Redis as a trusted backend. It stores
  sessions, credentials, OAuth state and plugin logs there. Publishing Redis (or
  exposing it to an untrusted network) is an owner error, not something the app
  defends against. `docker-compose.yml` deliberately publishes no Redis port.
- **Redis persistence is an operator responsibility.** With AOF enabled (as in
  `docker-compose.yml`) credentials and sessions survive a restart; without
  persistence, all `plugin:{slug}:secret:*` credentials and dashboard sessions
  are lost on restart. The app does not enforce or detect Redis persistence.
- **The application does not terminate TLS.** HTTPS is required outside local
  use; a reverse proxy must provide it. `BASE_URL` may be `http://` only for
  local use, and `COOKIE_SECURE` follows that scheme.
- **MCP transport is stateless HTTP(S) only.** The router ignores non-HTTP
  scopes and serves both `/mcp/{slug}` and `/mcp/{slug}/`; there is no separate
  authenticated admin API beyond the owner-secret endpoints.
- **Plugins cannot be killed.** The host bounds each call with timeouts and a
  per-plugin semaphore, and tracks leaked workers to fail fast with
  `plugin_overloaded`, but a hung worker thread keeps running; plugin authors
  must set their own HTTP client timeouts (`ARCHITECTURE.md`, "Packages and
  runtime").
- **The CSP allows no inline script by design.** `default-src 'self';
  frame-ancestors 'none'` with a no-build static SPA means no nonce/hash is
  needed; stricter directives (e.g. `form-action`, `object-src`) are not set.
- **Disconnected plugins still list their tools** (as required); calls return
  the explicit `[not_connected] …` error rather than a generic auth failure.
