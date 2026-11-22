# OAuth: operator and author walkthrough

How the Custom MCP core completes an `oauth` plugin's sign-in, and how to try it
end to end against the local mock provider. The browser redirect steps must be
confirmed by hand; the automated suite drives the real callback route with `httpx`
and the real health scheduler, but does **not** run a real browser.

## The flow and the states you should see

1. **Import** the `oauth_demo` package (`POST /api/plugins/import`, engine slug
   `oauth-demo`). It has one auth mode, `oauth`, so it does **not** auto-connect.
   Expected detail: `load_state = loaded`, `status = not_connected`, tool list
   `[whoami]`.
2. **Connect.** In the detail dialog click **Connect** and choose `oauth`. The
   dialog shows an optional **client id** and **client secret**. Submit; the
   browser POSTs
   `{"mode":"oauth","credentials":{"client_id":"...","client_secret":"..."}}` to
   `POST /api/plugins/oauth-demo/connect`.
   - With a client id (or a previously stored one) the response is
     `status = oauth_required`, `auth_mode = oauth`, and carries a
     `redirect_url` — the absolute provider authorize URL. The dashboard then
     navigates the browser to it.
   - With no client id and none stored, the request is rejected `422
     invalid_request` and nothing changes.
3. **Provider sign-in.** The provider validates the PKCE `code_challenge` and the
   quoted `redirect_uri` (exactly
   `{BASE_URL}/mcp/oauth-demo/oauth/callback`) and redirects back to that
   callback with `?code=...&state=...`.
4. **Callback.** The public `GET /mcp/oauth-demo/oauth/callback` validates and
   **consumes** the single-use, slug-bound state, runs `on_oauth_result(code,
   state)` (the authorization-code + verifier exchange), persists the status and
   re-lists tools, then `303`-redirects to the fixed relative dashboard target:
   - `/#/plugins/oauth-demo?oauth=success` on success, or
   - `/#/plugins/oauth-demo?oauth=failed` on any failure.

   The dashboard shows a one-off success/failure toast and clears the query.
   Expected detail after success: `status = connected`, `auth_mode = oauth`, tool
   list `[whoami, me]` (the `me` tool exists only once a token is stored), and
   credential **key names only** (`core.oauth_client_id`, `oauth_access_token`,
   `oauth_refresh_token`, `oauth_expires_at`).
5. **Refresh.** When the access token expires the plugin pings `auth_expired`;
   the health scheduler calls `refresh()` under the lifecycle lock, which rotates
   the refresh token and returns the plugin to `connected`. No manual action is
   needed.

### What must never change status

A missing, forged, replayed, expired or slug-mismatched `state`, and a
provider-declared `error=...`, always redirect to `?oauth=failed`. None of them
call `on_oauth_result` or alter `core:plugin:{slug}:meta`. The state is
single-use (`GETDEL`) and bound to the callback's path slug.

## Stored material

| Where | Key | Notes |
| --- | --- | --- |
| secret | `core.oauth_client_id` | reused when a later connect omits the id |
| secret | `core.oauth_client_secret` | optional; never returned or logged |
| data | `pkce:{state}` | PKCE verifier, TTL 600, deleted after the exchange |
| secret | `oauth_access_token` / `oauth_refresh_token` / `oauth_expires_at` | derived by the example plugin |

No credential value, PKCE verifier, authorization code or token appears in any API
JSON, `tools/list`, redirect URL or persisted log line.

## Try it against the local mock provider

`tests/oauth_provider.py` runs a genuine loopback OAuth 2.0 + PKCE server
(`/authorize`, `/token`, `/me`). The example plugin reads its provider base URL
from `CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL` at call time.

Run the automated end-to-end suite (real app + real callback route + real health
cycle + loopback provider + fake Redis):

```bash
.venv/bin/python -m pytest -q tests/integration/test_oauth.py
```

To reproduce by hand with the app running, export the provider origin before
starting the app and import `plugin_examples/oauth_demo`:

```bash
export CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL=http://127.0.0.1:<port>
```

Then connect `oauth-demo` from the dashboard and complete the provider
redirect. The browser returns to `/#/plugins/oauth-demo?oauth=success`.
