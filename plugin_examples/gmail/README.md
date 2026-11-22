# Gmail connector plugin example

A Gmail connector that uses only the dependency-free SDK (`mcp_plugin_sdk`), the
Python standard library, and `requests`. It advertises exactly one auth mode,
`oauth`, and talks to the Gmail REST API with a Google access token obtained
through the OAuth 2.0 authorization-code + PKCE flow.

| Tool | Arguments | Result |
| --- | --- | --- |
| `whoami` | none | `{"mode": "oauth", "connected": <bool>, "token_present": <bool>, "refreshable": <bool>}` |
| `list_labels` | none | `{"labels": [{"id", "name", "type"}]}` |
| `list_messages` | `q` (string, optional), `maxResults` (integer, optional) | `{"messages": [{"id", "threadId"}], "resultSizeEstimate", "nextPageToken"?}` |
| `get_message` | `id` (string, required), `format` (`metadata`\|`full`, default `metadata`) | `{"id", "threadId", "labelIds", "snippet", "subject", "from", "date", "headers"}` |
| `create_draft` | `to`, `subject`, `body` (required), `cc`, `bcc` (optional) | `{"draft_id", "message_id"}` |
| `send_message` | `to`, `subject`, `body` (required), `cc`, `bcc` (optional) | `{"message_id", "threadId", "labelIds"}` |

`send_message` is marked **DESTRUCTIVE** in its description: it delivers real
mail immediately and cannot be undone. `create_draft` only stores a draft.
`whoami` never returns any part of a token — only whether one is present and
whether it is refreshable.

`maxResults` is bounded to `1..50` (default `10`) in two layers: the tool schema
declares `minimum`/`maximum`, so the host rejects an out-of-range value before
`invoke_tool` runs, and the plugin clamps defensively (a non-integer falls back to
the default). `get_message`'s `id` is restricted to `^[A-Za-z0-9_-]{1,128}$` (in
the schema and again in the plugin), so it can never reshape the upstream URL
path. Compose arguments are validated before any HTTP call: `to` must be
non-empty, `subject`/`body` must be strings, and no header value may contain `\r`
or `\n` (header-injection guard) — a rejected call makes no upstream request.

## The OAuth flow the core drives

The host owns the browser redirect and the single-use state; the plugin only
produces URLs and exchanges codes:

1. **`connect()`** returns `oauth_required` until a valid access token is stored
   (present and not past `gmail_expires_at`), then `connected`.
2. **`get_oauth_sign_in_url(state)`** mints a PKCE `code_verifier`
   (`secrets.token_urlsafe(64)`), stores it in scoped **data** under
   `pkce:{state}` with a 600-second TTL, and returns the Google authorize URL:

   ```text
   https://accounts.google.com/o/oauth2/v2/auth
     ?response_type=code
     &client_id=<core.oauth_client_id>
     &redirect_uri=<url-quoted ctx.callback_url>
     &state=<state>
     &scope=<space-joined SCOPES>
     &code_challenge=<BASE64URL(SHA256(verifier)) with padding stripped>
     &code_challenge_method=S256
     &access_type=offline
     &prompt=consent
     &include_granted_scopes=true
   ```

   Scopes requested: `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.compose` (compose covers creating
   drafts and sending).

3. **`on_oauth_result(code, state)`** reads the verifier back from `pkce:{state}`
   and POSTs form-encoded to the token endpoint:

   ```text
   grant_type=authorization_code
   code=<code>
   code_verifier=<verifier>
   redirect_uri=<ctx.callback_url>
   client_id=<core.oauth_client_id>
   client_secret=<core.oauth_client_secret>   # only when the core stored one
   ```

   Google's web-server apps require the client secret on the exchange, so the
   plugin reads the reserved `core.oauth_client_secret` and sends it. On success
   it stores the derived values in scoped **secrets** under non-reserved names,
   deletes the PKCE entry, and returns `connected`. A `400`/`401`/`403` maps to
   `auth_expired`; a network error or `5xx` maps to `error`.

4. **`refresh()`** POSTs `grant_type=refresh_token` (with the stored refresh
   token, `client_id`, and `client_secret` when present), rotates the stored
   access/refresh/expiry values, and returns `connected`. With no stored refresh
   token it returns `auth_expired`. The new refresh token is written **before**
   the new access token so the long-lived credential is never left stale.

5. **`invoke_tool`** maps Gmail API failures: `401`/`403` → non-retryable
   `auth_expired`; `404` → `not_found`; `429` → retryable `rate_limited`;
   timeout / connection error / `5xx` → retryable `upstream_unavailable`; an
   unknown tool → `not_found`.

### Stored keys

| Scope | Key | Purpose |
| --- | --- | --- |
| data | `pkce:{state}` | PKCE verifier, TTL 600 (deleted after the exchange) |
| data | `oauth_credentials_hook` | what `on_oauth_credentials_set` observed (presence booleans only) |
| secrets | `gmail_access_token` | bearer token used by every Gmail API call |
| secrets | `gmail_refresh_token` | rotating token used by `refresh` |
| secrets | `gmail_expires_at` | absolute epoch seconds derived from `expires_in` |

The reserved keys `core.oauth_client_id` (and optional `core.oauth_client_secret`)
are written by the **core** before the credential hook runs. The plugin reads
them but never copies them into a tool result, a log line, or plugin data.

## Callback URL

The core supplies `ctx.callback_url`; it is the plugin's redirect target, for
example `https://mcp.example.com/mcp/gmail/oauth/callback`. The Google OAuth
client's **Authorized redirect URI** must list exactly that URL — the plugin
sends the same value as `redirect_uri` in the authorize request and in the token
exchange.

## Endpoints and environment overrides

All HTTP calls route through a single module-level `_http_request` helper. Each
endpoint can be overridden for tests:

| Variable | Default |
| --- | --- |
| `CUSTOM_MCP_GMAIL_AUTHORIZE_URL` | `https://accounts.google.com/o/oauth2/v2/auth` |
| `CUSTOM_MCP_GMAIL_TOKEN_URL` | `https://oauth2.googleapis.com/token` |
| `CUSTOM_MCP_GMAIL_API_BASE_URL` | `https://gmail.googleapis.com` |

Gmail API calls go to
`{CUSTOM_MCP_GMAIL_API_BASE_URL}/gmail/v1/users/me/...` with
`Authorization: Bearer <access_token>` and `Accept: application/json`, all with a
short timeout.

## Manifest

```json
{
  "name": "gmail",
  "display_name": "gmail",
  "description": "Gmail connector: Google OAuth 2.0 + PKCE sign-in, read labels and messages, create drafts, and send mail.",
  "version": "1.0.0",
  "entry": "plugin:GmailPlugin",
  "auth_modes": ["oauth"],
  "min_core_version": "0.1.0"
}
```

The slug `name` must match `[a-z0-9][a-z0-9-]{0,39}` (no underscores).

## Build a package ZIP (Python standard library, no Node)

```bash
cd plugin_examples/gmail
python - <<'PY'
import zipfile
from pathlib import Path

with zipfile.ZipFile("gmail.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "plugin.py"):
        archive.write(name, Path(name))
print("wrote gmail.zip")
PY
```

`gmail.zip` is what the dashboard import endpoint (`POST /api/plugins/import`,
multipart file with `mode=reject|replace`) or the CLI uploads.

## Testing

`tests/unit/test_gmail_demo.py` imports the module directly from this directory.
The OAuth sign-in and exchange tests drive it against the real loopback mock
provider in `tests/oauth_provider.py` (point
`CUSTOM_MCP_GMAIL_AUTHORIZE_URL` / `CUSTOM_MCP_GMAIL_TOKEN_URL` at
`provider.base_url`), so the plugin's synchronous `requests` calls are genuinely
exercised. The Gmail API tools monkeypatch the single `_http_request` helper and
assert on the recorded calls.

```bash
.venv/bin/python -m pytest -q tests/unit/test_gmail_demo.py
```

## Notes

- **Connect is driven by the dashboard/API.** The owner sets `client_id` (and
  optionally `client_secret`) through the connect dialog or
  `POST /api/plugins/gmail/connect`; the core stores them, creates the state,
  calls `get_oauth_sign_in_url`, and handles the public callback.
- **Call it over MCP.** Once installed and connected, the plugin is served at
  `{BASE_URL}/mcp/gmail/` for any MCP client using
  `Authorization: Bearer <MCP_SECRET>`.
- No token, verifier, code, or client secret is ever placed in a tool result, a
  log line, or plugin data. Storage keys that start with `core.` are reserved and
  readable-only for plugin code.
- Google's `gmail.readonly` scope cannot modify the mailbox and `gmail.compose`
  cannot permanently delete; sending is the only irreversible action, which is
  why `send_message` is flagged destructive.
