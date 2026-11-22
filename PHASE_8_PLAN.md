# Phase 8 plan — OAuth state, callback, PKCE example and mock-provider integration

Goal: implement the OAuth connect flow for `oauth`-auth plugins — client
credential storage/reuse, single-use slug-bound state, a public slug-bound
callback, PKCE, an `oauth_demo` example that talks to a local mock provider, and
health-scheduler refresh (token rotation).

## Acceptance gate (TEST_MATRIX row 8)
Local mock provider authorize/code/token exchange with PKCE; correct callback
`redirect_uri`; stored client credential reuse; state single use/expiry/slug
binding; provider error requires valid state; forged/reused callback cannot
mutate status; fixed relative redirect; refresh rotation and scheduler
integration.

## Contract (ARCHITECTURE.md "Health and OAuth", lines 158–174; lines 25, 63, 68–69, 112)
- Auth modes are `none | api_key | oauth`; statuses include `oauth_required` and
  `auth_expired`.
- `POST /api/plugins/{slug}/connect` with `mode="oauth"` validates the supported
  mode, stores the selected mode and supplied credentials, calls hooks, then
  `connect`. Omitting `client_id` reuses the stored one, otherwise 422.
- `oauth_required` (from `connect`) creates state, then either gets a sign-in URL
  (`get_oauth_sign_in_url(state)`) or **inserts/validates** `state` in the
  plugin-provided `redirect_url`.
- Core OAuth credentials: `core.oauth_client_id`, optional
  `core.oauth_client_secret` (written before the credential hook).
- `core:oauth_state:{state}` = `{slug, created_at}` JSON, `EX 600`, **GETDEL
  single use**.
- Callback route `GET /mcp/{slug}/oauth/callback` is public, **slug-bound**, and
  must precede catchall MCP dispatch. It validates/consumes state bound to the
  path slug, calls `on_oauth_result(code, state)` under the lifecycle lock,
  persists/re-lists tools if connected, and redirects **only** to the fixed
  relative `/#/plugins/{slug}?oauth=success|failed`.
- Provider-declared errors require a valid state; a forged/reused callback cannot
  alter plugin status.
- PKCE verifier lives in plugin data `pkce:{state}` with `TTL 600`. Token exchange
  uses `callback_url`; derived tokens/expiry are stored in secrets.
- OAuth demo talks to the mock provider in tests; `refresh` rotates tokens.

## Current state (already present)
- `auth/middleware.py::is_public_path` already treats
  `^/mcp/[a-z0-9][a-z0-9-]{0,39}/oauth/callback$` as public.
- `PluginManager` has `_OAUTH_STATE_PREFIX`, `_delete_oauth_states` (cleanup on
  `remove`), `_callback_url(slug)` = `{BASE_URL}/mcp/{slug}/oauth/callback`.
- `connect` currently validates only `api_key`; `oauth` mode falls through to
  `host.call(runtime, "connect")` with **no** credential storage, state creation
  or sign-in URL. No public callback route exists.
- The SDK already exposes `on_oauth_credentials_set`, `get_oauth_sign_in_url`,
  `on_oauth_result`, `refresh`.

## Config
Reuse `BASE_URL` (already required) and the existing `PLUGIN_CALL_TIMEOUT_SECONDS`.
Add a module constant `_OAUTH_STATE_TTL_SECONDS = 600` (not a new setting) and,
if needed for the mock provider, test-only env/`secrets` values. No new
production setting unless a test proves it necessary.

## Slices (non-overlapping ownership)
### 8a OAuth backend (one agent) — owns these files only
- `src/custom_mcp/plugins/manager.py`: extend `connect` for `oauth` mode via a new
  private path; add `create_oauth_state(slug) -> str`, `consume_oauth_state(state,
  slug)` (GETDEL, slug check), and `complete_oauth(slug, code, state) -> dict`
  that runs under the lifecycle lock: consume+validate state, call
  `on_oauth_result` through the host, set status/auth_mode, refresh tools,
  re-mount, redact descriptions via the existing paths. Store
  `core.oauth_client_id`/`core.oauth_client_secret` before
  `on_oauth_credentials_set`; reuse the stored id when omitted, else 422; retain
  stored client secrets for redaction exactly like the api-key path.
- `src/custom_mcp/api/oauth.py` (new): the public callback Starlette route handler
  (parse `code`/`state`/`error`, call the manager, always `RedirectResponse` to
  the fixed relative `/#/plugins/{slug}?oauth=success|failed`). Invalid/missing/
  expired/mismatched state and provider errors must not mutate status.
- `src/custom_mcp/api/plugins.py`: pass `client_id`/`client_secret` through the
  `oauth` connect path and surface the sign-in/`redirect_url` in the returned
  detail (no secrets in JSON).
- `src/custom_mcp/main.py`: register the callback `Route` **before**
  `Mount("/mcp", ...)`.
- `tests/unit/test_oauth_backend.py`: state create/consume/expiry/mismatch,
  credential store/reuse/422, sign-in URL vs redirect_url state insertion,
  forged/reused callback cannot change status.

### 8b oauth_demo example + mock provider (one agent) — owns these files only
- `plugin_examples/oauth_demo/**` (`manifest.json` slug `oauth-demo`, single
  `oauth` auth mode, `plugin.py`, short author README): `connect` returns
  `oauth_required`; `get_oauth_sign_in_url(state)` generates a PKCE verifier,
  stores it under `pkce:{state}` (`ttl=600`), and returns the provider authorize
  URL with `code_challenge`, `client_id`, `redirect_uri = ctx.callback_url` and
  `state`; `on_oauth_result(code, state)` exchanges the code (+verifier) for
  tokens using `ctx.callback_url`, stores derived tokens/expiry in secrets, and
  returns `connected`; `refresh` rotates the token and returns `connected` or
  `auth_expired`; at least one `invoke_tool` that uses a stored token.
- `tests/oauth_provider.py` (new, reused by 8c): a local mock provider —
  `/authorize` (validates `code_challenge`/`redirect_uri`, issues a code) and
  `/token` (validates the PKCE verifier + `redirect_uri`, issues/rotates tokens);
  a real loopback HTTP server so synchronous `requests` calls are exercised.
- `tests/unit/test_oauth_demo.py`: URL shape/PKCE, verifier storage, token
  exchange, derived-secret storage, refresh rotation, redaction.

### 8c integration + docs (one agent, after 8a/8b interfaces freeze) — owns these files only
- `tests/integration/test_oauth.py`: full app + real router + mock provider:
  install `oauth-demo`, connect (oauth) returns the sign-in URL with correct
  `redirect_uri`; simulate the provider callback (real HTTP GET to the callback
  route); state single use (second callback fails), expiry, slug binding; a
  provider `error` callback needs valid state; forged/reused callback leaves
  status unchanged; redirect is the fixed relative URL; refresh rotation via the
  health scheduler (`run_cycle`) returns `connected`; stored client credential
  reuse on a later connect.
- `README.md` + `docs/` OAuth section: setup, callback URL, mock provider usage,
  credential/secret handling and warning.

### Manager-owned (no overlap)
- `src/custom_mcp/static/{index.html,app.js}`: mode-aware connect dialog OAuth
  fields (`client_id`/`client_secret`) and a "Sign in" link when the connect
  response carries a redirect/sign-in URL; keep the existing
  `#/plugins/{slug}?oauth=success|failed` banner. DOM-only, CSP-clean.
- Joined-tree verification, `BUILD_PROGRESS.md`, `TEST_MATRIX.md` evidence, and
  the Phase 8 gate critic.

## Definition of done
- All Phase 8 acceptance rows pass with real wire-level evidence (mock provider,
  real callback route, real health cycle), plus the cumulative suite, Ruff, mypy,
  `uv lock --check` and the 15 real-Redis tests.
- No secret (client secret, code, tokens, verifier) appears in any API JSON,
  persisted log, `tools/list`, or the redirect URL.
- A forged/reused/expired/mismatched callback never changes plugin status.
- Independent Phase 8 gate critic scores strictly above 8.5 (MVP-only policy).
