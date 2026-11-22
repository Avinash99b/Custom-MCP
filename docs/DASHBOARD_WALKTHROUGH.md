# Dashboard walkthrough

A manual, browser-level walkthrough of the Custom MCP dashboard (Phases 5–6). It
drives the real static UI against the real REST API and the real MCP endpoint.

> **Verify every step by hand.** The automated suite asserts only that the served
> static dashboard/login source is CSP-compatible and DOM-only, that assets and
> integration endpoints behave, and that the MCP wire works. It does **not** drive
> a real browser. Redirects, rendering, copy buttons, dialogs, hash navigation and
> the OAuth banner described below must all be confirmed manually.

## 1. Prerequisites and start-up

You need:

- a Redis server reachable at `REDIS_URL` (default `redis://localhost:6379/0`);
- an owner secret of at least 16 characters, exported as `MCP_SECRET`;
- `BASE_URL` set to the origin you will open in the browser (for local use,
  `http://localhost:8000`);
- the three example packages (hello, apikey_demo, oauth_demo) built as ZIPs;
- an MCP client that can send a bearer-token header (for step 3).

Start the app:

```bash
export MCP_SECRET="a-strong-secret-value-please"
export BASE_URL="http://localhost:8000"
uv run custom-mcp
```

Build the example ZIPs with the Python standard library:

```bash
cd plugin_examples/hello
python - <<'PY'
import zipfile
with zipfile.ZipFile("hello.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("manifest.json", "plugin.py"):
        z.write(name, name)
print("wrote hello.zip")
PY

cd ../apikey_demo
python - <<'PY'
import zipfile
with zipfile.ZipFile("apikey_demo.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("manifest.json", "plugin.py"):
        z.write(name, name)
print("wrote apikey_demo.zip")
PY
```

The import endpoint uses the slug from the ZIP's `manifest.json`, not the file
name, so `apikey_demo.zip` installs the plugin with slug `apikey-demo`.

## 2. Sign in and land on the grid

1. Open `http://localhost:8000/` in the browser.
   **Expected:** there is no session cookie yet, so the server answers the
   browser GET with a `302` redirect. The address bar shows `/login`, and the
   **Sign in** page renders with an **Owner secret** password field.
2. Type the `MCP_SECRET` value and submit the form.
   **Expected:** the page replaces to `/` and the dashboard grid renders. You
   land on the grid route (`#/`; an empty hash is treated as the grid). The
   summary strip shows zero plugins and the grid shows its empty state. The only
   thing stored in the browser is the server-issued `session` cookie, which is
   `HttpOnly`.
3. Reload the page, and (optionally) open `/` in a private window.
   **Expected:** the reload keeps you signed in on the grid; the private window
   has no cookie and is redirected to `/login` again.

## 3. Import `hello`, inspect it and copy its endpoint

1. Use the dashboard's import control to select `plugin_examples/hello/hello.zip`
   and confirm.
   **Expected:** the browser POSTs the file to `POST /api/plugins/import`
   (multipart `file`, `mode=reject`) and the grid now lists a plugin named
   **Hello** (slug `hello`, version `1.0.0`). Because its only auth mode is
   `none`, it auto-connects on install: the row shows load state **loaded** and
   connection status **connected**, and the detail dialog lists the tools `echo`
   and `add`. The
   summary counts update (one plugin, one connected, zero needing attention).
2. Open the plugin's detail by selecting the row (or by navigating to
   `#/plugins/hello`).
   **Expected visible state:**
   - the manifest: slug `hello`, display name `Hello`, version `1.0.0`, auth
     mode `none`;
   - the MCP **endpoint URL** `http://localhost:8000/mcp/hello/` and the
     **callback URL** `http://localhost:8000/mcp/hello/oauth/callback`, each with
     a copy button;
   - two tools with pretty-printed JSON schemas: `echo` requires a string
     `text`; `add` requires numbers `a` and `b`;
   - **no** credential key names (a `none` plugin stores none);
   - any packaging/lint warnings, and the recent (initially short) log list.
3. Click the endpoint's copy button.
   **Expected:** the endpoint URL is placed on the clipboard.

## 4. Call the endpoint from an MCP client (not the dashboard)

> This step happens in your MCP client, not in the dashboard.

1. Add `http://localhost:8000/mcp/hello/` as an MCP server URL (both slash forms
   are accepted), and give it the owner bearer token, i.e. the header
   `Authorization: Bearer <MCP_SECRET>`. The dashboard session cookie does **not**
   authenticate MCP endpoints — MCP always uses the bearer token.
2. Initialize the session and list tools.
   **Expected:** `echo` and `add` are listed with the same input schemas shown in
   the detail dialog. Calling `echo` with `{"text":"hi"}` returns
   `{"text":"hi"}`; calling `add` with `{"a":2,"b":3}` returns `{"sum":5}`.
   Calling with missing or wrong-typed arguments returns a uniform
   `[invalid_arguments] Invalid arguments for tool '<name>': ...` error.

## 5. Import `apikey_demo` and connect it with an API key

1. Import `plugin_examples/apikey_demo/apikey_demo.zip`.
   **Expected:** the grid lists a plugin with display name **apikey_demo** and
   slug `apikey-demo`, auth mode `api_key`, load state **loaded** and connection
   status **not_connected** (no credentials are stored yet). Its tool list
   contains only `whoami`; it does **not** auto-connect.
2. Open its detail and click **Connect**.
   **Expected:** the connect dialog is mode-aware. Because the plugin supports
   only `api_key`, it shows a single key field (a password-style input). Enter any
   non-empty key and submit; the browser sends
   `POST /api/plugins/apikey-demo/connect` with
   `{"mode":"api_key","credentials":{"api_key":"..."}}`.
   For an `oauth` plugin the same dialog instead shows optional **client id** and
   **client secret** fields and submits
   `{"mode":"oauth","credentials":{"client_id":"...","client_secret":"..."}}`.
   An empty client id reuses the stored one; on success the response carries a
   `redirect_url` and the dashboard **navigates the browser to the provider** to
   sign in. The provider must be registered with exactly the plugin's callback
   URL (`{BASE_URL}/mcp/{slug}/oauth/callback`, shown in the detail); after
   sign-in the browser returns to `#/plugins/{slug}?oauth=success|failed`.
3. Observe the refreshed detail.
   **Expected:** the tool list now contains `whoami` **and** `fetch_item` — the
   plugin lists `fetch_item` only once a key has been stored, so the live tool set
   changes when it connects. The **credential key names** section lists
   `core.api_key`: a *name only*, never a value.
   The connection status depends on the upstream the plugin probes
   (`CUSTOM_MCP_APIKEY_DEMO_BASE_URL`, default `https://api.example.invalid`): the
   default is unreachable, so the status shows **error** (or `auth_expired` if the
   upstream answers `401`/`403`). To see **connected**, point that environment
   variable at an upstream that answers `2xx` *before* starting the app. Either
   way, the stored key makes `fetch_item` appear.
4. Confirm the credential value is absent from the UI.
   **Expected:** the key value never appears in the dialog, the page, or the log
   list; only the key *name* (`core.api_key`) and redacted text (`***`) are shown.
   The value also never appears in `localStorage`, `sessionStorage` or any
   JS-written cookie.

## 6. Disconnect and observe the status change

1. In the detail, click **Disconnect**.
   **Expected:** the plugin's disconnect hook runs, its stored secrets are wiped,
   the connection status resets to **not_connected**, and the selected auth mode
   clears. The **credential key names** section becomes empty. The summary counts
   update (the plugin is no longer connected, and if it was showing an `error`
   status it also leaves the attention count).
2. Look at the tool list.
   **Expected:** tools stay listed for a loaded plugin even while it is
   disconnected — `whoami` remains. The credential-gated `fetch_item` disappears
   again because the key it depends on was wiped, which is exactly the tool-set
   change first seen on connect. If you call `whoami` over MCP while
   disconnected, the plugin returns `[not_connected] Plugin 'apikey-demo' is not
   connected. Reconnect it in the dashboard.`

## 7. Reload, replace and remove

1. **Reload.** Click **Reload**.
   **Expected:** `POST /api/plugins/apikey-demo/reload` re-extracts the stored ZIP
   and loads a fresh generation. The load state stays **loaded** and the tools are
   re-listed; the log list may show reload activity.
2. **Replace with the same slug.** Import `apikey_demo.zip` again (or a copy whose
   `manifest.json` you edited, for example bumping the version to `2.0.0`, keeping
   the slug `apikey-demo`).
   **Expected:** because the slug already exists, the import is rejected with
   `plugin_exists` (HTTP 409) and the dashboard shows a **replacement
   confirmation**. Confirm it.
   **Expected after confirming:** the dashboard retries with `mode=replace`
   (`POST /api/plugins/import?mode=replace`). The plugin is replaced from the new
   ZIP; on success stored data and credentials are preserved and the grid/detail
   update. If the new package fails to load, the previous version is restored.
3. **Remove.** Open the detail and click **Remove**.
   **Expected:** a destructive confirmation is shown first. Confirm it.
   **Expected after confirming:** the plugin is shut down (its `shutdown` hook,
   not `on_disconnect`), the row disappears from the grid, the summary counts
   drop, and a call to `http://localhost:8000/mcp/apikey-demo/` from your MCP
   client now returns `404` with `not_found`.

## 8. Hash navigation and the OAuth result query

- Routing is hash-based. `#/` is the grid; `#/plugins/{slug}` opens that plugin's
  detail dialog directly. Reloading the browser on `#/plugins/hello` reopens the
  same detail, and the browser Back/Forward buttons move between the grid and the
  last-open detail.
- The OAuth callback redirects to
  `/#/plugins/{slug}?oauth=success` or `/#/plugins/{slug}?oauth=failed`. When the
  dashboard loads that hash it shows a one-off success/failure toast for the
  plugin and then **clears the query**, rewriting the hash back to
  `#/plugins/{slug}`. The result is therefore displayed once and never persisted.
  You can preview the toast by opening `#/plugins/{slug}?oauth=success` (or
  `?oauth=failed`) directly. The server-side public route
  `GET /mcp/{slug}/oauth/callback` is what a provider returns to (an unknown slug
  there is `404`); it is distinct from this hash banner.

## 9. Back up and restore

> Backup download and restore are browser behaviours; verify them by hand. The
> automated tests only assert the static settings markup, that the script names
> the backup/restore endpoints, and (under Node) the URL and query the controls
> build.

1. In the **Settings** area, read the warning banner.
   **Expected visible state:** a prominent warning near the top of the section
   explains that a backup contains plugin credentials and reserved core secrets
   in **plaintext** and must be stored like a password. It is visible without any
   interaction.
2. Back up the full install: leave every plugin unticked in the **Backup** panel
   and click **Download backup**.
   **Expected:** the browser sends a full-page navigation to `/api/backup`. The
   response is an attachment, so the dashboard stays put and the browser
   downloads `custom-mcp-backup-<utc>.zip`. The address bar does not leave the
   page.
3. Back up a subset: tick **hello** only and click **Download backup**.
   **Expected visible state:** the browser navigates to
   `/api/backup?plugins=hello` and the ZIP contains only `plugins/hello/...`
   entries plus `backup.json`. **Clear selection** (or unticking everything)
   goes back to backing up everything.
4. Inspect the downloaded ZIP.
   **Expected:** `backup.json` plus, for each included plugin,
   `plugins/{slug}/plugin.zip`, `plugins/{slug}/data.json`,
   `plugins/{slug}/secrets.json` and `plugins/{slug}/meta.json`. The data and
   secrets files hold plaintext values, and any key that had a TTL is absent.
5. Restore with **skip**: choose the ZIP in the **Restore** panel, leave **Mode**
   at *Skip plugins that already exist*, leave the slug filter blank and click
   **Restore**.
   **Expected visible state:** a **Restore results** area appears with one row
   per plugin: an already-installed plugin shows the badge **Skipped** together
   with its current connection status, and the summary reads, for example,
   `Restored 0, skipped 2, failed 0.` The grid refreshes and nothing changes.
6. Restore with **replace** and a filter: set **Mode** to *Replace plugins that
   already exist*, type `hello` in the **Only these slugs** field and click
   **Restore**.
   **Expected:** the browser POSTs multipart `file` to
   `/api/restore?mode=replace&slugs=hello`. The results area shows one row for
   `hello` with the badge **Restored** and its resulting status, and the summary
   reads `Restored 1, skipped 0, failed 0.` The grid then refreshes; the plugin is
   restored as `not_connected` and reconnects per its stored auth mode.
7. Restore an invalid or encrypted archive.
   **Expected:** the request is rejected with `422`, the dashboard shows the
   server's error message in the banner, the results area keeps its previous
   contents, and no plugin changes.

## What the automated tests do and do not cover

Automated tests cover:

- the **served static source**: the dashboard/login shells are CSP-compatible and
  contain no inline script/style, and the dashboard script uses DOM APIs only
  (no `innerHTML` with server data, no `eval`, no `localStorage`/`sessionStorage`);
  the **settings section** must declare the backup/restore ids and the plaintext
  warning, and the script must name `/api/backup` and `/api/restore`;
- the **integration endpoints**: import (reject/replace), connect/disconnect,
  reload, remove, the detail/list/logs read models, and the MCP wire over the
  routed `/mcp/{slug}/` endpoint, including redaction of stored credentials;
- the **backup/restore controls** under the Node DOM harness: the download URL
  (full and per-plugin subset) and the restore request's mode/slug query and
  rendered result rows.

They do **not** run a real browser. The visible states, the `/` → `/login`
redirect, sign-in, the grid/attention rendering, the copy buttons, the detail and
connect dialogs, the replacement and removal confirmations, hash navigation, the
OAuth banner and the backup download/restore file dialogs must all be verified
manually using the steps above.
