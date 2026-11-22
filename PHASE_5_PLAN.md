# Phase 5 plan — static dashboard and documented UI walkthrough

Goal: replace the placeholder dashboard with the full static HTML/vanilla-JS/CSS
panel that exercises the already-shipped REST API, and document a manual
walkthrough. No Node/build step, no innerHTML with server data.

## Acceptance gate (TEST_MATRIX row 5)
Static CSP-compatible login/dashboard; DOM-only rendering; hash navigation;
status counts/attention/grid; import replacement confirmation; detail
URLs/tools/schema/key names/warnings/logs; connect/disconnect/reload/remove; no
secret persistence in browser; manual walkthrough documented.

## Contract (ARCHITECTURE.md 122–139)
- All `/api/*` stay `Cache-Control: no-store`; errors are
  `{"error":{"code","message"}}`; CSP `default-src 'self'; frame-ancestors 'none'`
  plus `nosniff` already added by middleware — the dashboard must work under it
  (no inline scripts, no inline styles, no external origins, no `eval`).
- Rendering uses `textContent`/DOM APIs only. Never `innerHTML` with server data.
- Dashboard: counts and "attention" state, plugin grid, import with replacement
  prompt, detail dialog (endpoint/callback URLs + copy, tools with schemas,
  credential **key names only**, lint warnings, logs with refresh, lifecycle
  actions), mode-aware connect dialog, settings placeholder for backup (Phase 6).
- Hash navigation (e.g. `#/`, `#/plugins/{slug}`, `#/plugins/{slug}?oauth=success`)
  so the OAuth redirect target resolves. OAuth result query is displayed and then
  cleared, never persisted.
- No credentials in `localStorage`/`sessionStorage`/cookies written by JS; the
  session remains the HttpOnly cookie managed by the server.

## Slices (non-overlapping ownership)
### 5a — shell, routing and plugin grid
- `src/custom_mcp/static/index.html`: semantic shell (header nav, main region,
  dialog container, toast region). No inline handlers or styles.
- `src/custom_mcp/static/styles.css`: layout, grid, status badges, dialog,
  responsive breakpoints; attention styling.
- `src/custom_mcp/static/app.js`: hash router (`#/`, `#/plugins/{slug}`), session
  guard redirecting to `/login`, summary fetch + rendering, counts and attention,
  status/load badges, empty state, error banner. Strict DOM creation helpers
  (`el(tag, props, children)`) so no server string is ever parsed as HTML.
- `tests/integration/test_dashboard.py`: served shell under CSP; no inline
  script/style; dashboard requires a session (API 401 / browser redirect stays
  server-side); `/static` assets are served with the right content types; the
  shell contains no secret and no inline event attributes.

### 5b — detail dialog, connect flow and lifecycle actions
- `app.js` (same file, second pass after 5a is stable): detail dialog rendering
  manifest, load/status/auth, endpoint and callback URLs with copy buttons, tools
  with pretty-printed schemas, credential key names, warnings, last 50 logs with a
  refresh button, and actions: import (multipart, `mode` reject/replace with a
  confirmation when replacing), connect (mode-aware: `none` no body,
  `api_key` key field, `oauth` informational), disconnect, reload, remove with a
  destructive confirmation. Every write action re-fetches detail/summaries.
- `tests/unit/test_dashboard_source.py` (or an integration assertion): static
  source contains no `innerHTML`, no `eval`, no `localStorage`, no
  `sessionStorage`; only `textContent`/DOM construction; no absolute external URLs.
- Manifest cache-busting is avoided; assets are static and stable.

### 5c — documented walkthrough
- `README.md` (dashboard section) and `docs/DASHBOARD_WALKTHROUGH.md`: exact
  steps to log in, import `hello` and `apikey-demo`, connect, copy the endpoint
  into an MCP client, disconnect, reload, replace, remove, and the expected
  visible state at each step; explicit note that browser validation must be
  performed manually (source-only assertions do not count).

## Evidence required
- Shell loads under the enforced CSP; no inline script/style; assets served.
- Grid counts/attention derived from real API data; empty and error states.
- Detail dialog shows URLs, tools+schemas, key names only, warnings, logs.
- Import replace prompts before overwriting; connect dialog matches the mode.
- Lifecycle actions work end-to-end against the real app.
- No secret value reaches any DOM node, storage API or log.
- Focused tests, then full suite, Ruff, mypy; manual walkthrough documented.
