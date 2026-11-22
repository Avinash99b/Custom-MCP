# OAuth 2.0 + PKCE plugin example

A small custom MCP plugin that uses only the dependency-free SDK
(`mcp_plugin_sdk`), the Python standard library, and `requests`. It advertises
exactly one auth mode, `oauth`, and exposes:

| Tool | Arguments | Result |
| --- | --- | --- |
| `whoami` | none | `{"mode": "oauth", "connected": <bool>, "token_present": <bool>, "refreshable": <bool>}` |
| `me` | none | `{"sub": <subject>, "name": <name>}` fetched from the provider |

`me` is listed **only after a token is stored**, so signing in or disconnecting
swaps the plugin's live tool set. Neither tool ever returns any part of a token —
only whether one is present and which subject it authenticates.

## The OAuth flow the core drives

The host owns the browser redirect and the single-use state; the plugin only
produces URLs and exchanges codes:

1. **`connect()`** returns `oauth_required` until a valid access token is stored
   (present and not past `oauth_expires_at`), then `connected`.
2. **`get_oauth_sign_in_url(state)`** mints a PKCE `code_verifier`
   (`secrets.token_urlsafe(64)`, 86 characters, inside RFC 7636's 43–128 range),
   stores it in scoped **data** under `pkce:{state}` with a 600-second TTL, and
   returns the provider authorize URL:

   ```text
   {provider}/authorize
     ?response_type=code
     &client_id=<core.oauth_client_id>
     &redirect_uri=<url-quoted ctx.callback_url>
     &state=<state>
     &code_challenge=<BASE64URL(SHA256(verifier)) with padding stripped>
     &code_challenge_method=S256
   ```

3. **`on_oauth_result(code, state)`** reads the verifier back from
   `pkce:{state}` and POSTs form-encoded to `{provider}/token`:

   ```text
   grant_type=authorization_code
   code=<code>
   code_verifier=<verifier>
   redirect_uri=<ctx.callback_url>
   client_id=<core.oauth_client_id>
   ```

   On success it stores the derived values in scoped **secrets** under
   non-reserved names, deletes the PKCE entry, and returns `connected`. A
   `400`/`401`/`403` maps to `auth_expired`; a network error or `5xx` maps to
   `error`. Token material is never returned or logged.

4. **`refresh()`** POSTs `grant_type=refresh_token` (with the stored refresh
   token and `client_id`), rotates the stored access/refresh/expiry values, and
   returns `connected`. With no stored refresh token it returns `auth_expired`.
   The health scheduler calls this under its lifecycle lock, so the new refresh
   token is written before the new access token.

5. **`invoke_tool`** maps provider failures: `401`/`403` → non-retryable
   `auth_expired`; timeout/connection error/`5xx` → retryable
   `upstream_unavailable`; an unknown tool → `not_found`.

### Stored keys

| Scope | Key | Purpose |
| --- | --- | --- |
| data | `pkce:{state}` | PKCE verifier, TTL 600 (deleted after the exchange) |
| data | `oauth_credentials_hook` | what `on_oauth_credentials_set` observed |
| secrets | `oauth_access_token` | bearer token used by `me` |
| secrets | `oauth_refresh_token` | rotating token used by `refresh` |
| secrets | `oauth_expires_at` | absolute epoch seconds derived from `expires_in` |

The reserved key `core.oauth_client_id` (and optional `core.oauth_client_secret`)
is written by the **core** before the credential hook runs. The plugin reads the
client id but never reads, stores, or logs the client secret.

## Callback URL

The core supplies `ctx.callback_url`; it is the plugin's redirect target, for
example `https://mcp.example.com/mcp/oauth-demo/oauth/callback`. The provider's
app registration must list exactly that URL — the plugin sends the same value as
`redirect_uri` in the authorize request and in the token exchange.

## Provider base URL

All HTTP calls route through a single module-level helper. The provider base URL
comes from the environment variable `CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL` and
defaults to `https://oauth.example.invalid`:

```bash
export CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL=https://accounts.my-provider.example
```

Endpoints used: `GET {provider}/authorize`, `POST {provider}/token`, and
`GET {provider}/me` (with `Authorization: Bearer <access_token>`), all with a
short timeout.

## Manifest

```json
{
  "name": "oauth-demo",
  "display_name": "oauth_demo",
  "description": "OAuth 2.0 + PKCE example plugin: a sign-in URL with an S256 challenge, authorization-code token exchange, rotating refresh, and a bearer-authenticated me tool.",
  "version": "1.0.0",
  "entry": "plugin:OAuthDemoPlugin",
  "auth_modes": ["oauth"],
  "min_core_version": "0.1.0"
}
```

The slug `name` must match `[a-z0-9][a-z0-9-]{0,39}` (no underscores);
`oauth_demo` is only the display name.

## Build a package ZIP (Python standard library, no Node)

```bash
cd plugin_examples/oauth_demo
python - <<'PY'
import zipfile
from pathlib import Path

with zipfile.ZipFile("oauth_demo.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "plugin.py"):
        archive.write(name, Path(name))
print("wrote oauth_demo.zip")
PY
```

`oauth_demo.zip` is what the dashboard import endpoint (`POST /api/plugins/import`,
multipart file with `mode=reject|replace`) or the CLI uploads.

## Testing against the mock provider

`tests/oauth_provider.py` ships a real loopback OAuth server so the plugin's
synchronous `requests` calls are exercised end to end. Use it as a context
manager (or as the `mock_oauth_provider` pytest fixture):

```python
import importlib.util

spec = importlib.util.spec_from_file_location("oauth_demo_plugin", "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

from tests.oauth_provider import MockOAuthProvider

with MockOAuthProvider() as provider:
    import os
    os.environ["CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL"] = provider.base_url
    # ... build a PluginContext whose secrets hold "core.oauth_client_id"
    # equal to provider.client_id, then:
    url = plugin.get_oauth_sign_in_url("state-1")       # -> provider /authorize
    code = requests.get(url, allow_redirects=False).headers["Location"]  # ?code=...
    plugin.on_oauth_result(code, "state-1")             # -> provider /token
```

See `tests/unit/test_oauth_demo.py` for the full set of direct plugin tests.

## Notes

- **Connect is driven by the dashboard/API.** The owner sets `client_id` (and
  optionally `client_secret`) through the connect dialog or
  `POST /api/plugins/oauth-demo/connect`; the core stores them, creates the
  state, calls `get_oauth_sign_in_url`, and handles the public callback.
- No token, verifier, code, or client secret is ever placed in a tool result, a
  log line, or plugin data. Storage keys that start with `core.` are reserved and
  readable-only for plugin code.
