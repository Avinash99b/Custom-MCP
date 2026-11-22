# API-key plugin example

A small custom MCP plugin that uses only the dependency-free SDK
(`mcp_plugin_sdk`), the Python standard library, and `requests`. It advertises
exactly one auth mode, `api_key`, and exposes:

| Tool | Arguments | Result |
| --- | --- | --- |
| `whoami` | none | `{"mode": "api_key", "connected": <bool>, "credential_present": <bool>}` |
| `fetch_item` | `item_id` (string, required) | `{"id": <id>, "name": <name>}` from the upstream |

`fetch_item` is listed **only after a key is stored**, so connecting or
disconnecting swaps the plugin's live tool set. `whoami` never returns any part
of the credential — only whether one is present and whether the last connect
succeeded.

`on_api_key_set(api_key)` runs *after* the core has stored the credential in the
reserved scoped key `core.api_key`. The hook confirms that store by reading it
back and records the observation in the scoped storage key `api_key_hook`
(`{"stored": bool, "matched": bool}`); a mismatch is recorded, never raised.

Upstream failures map to structured results:

- `401`/`403` → `connect()` reports `auth_expired`; `fetch_item` returns the
  non-retryable `auth_expired` failure.
- Timeout, connection error, or `5xx` → `connect()` reports `error`;
  `fetch_item` returns the retryable `upstream_unavailable` failure.
- An unknown tool name returns the `not_found` failure.

## Upstream URL

`requests.get(...)` is routed through a single module-level helper. The base URL
comes from the environment variable `CUSTOM_MCP_APIKEY_DEMO_BASE_URL` and
defaults to `https://api.example.invalid`:

```bash
export CUSTOM_MCP_APIKEY_DEMO_BASE_URL=https://api.my-service.example
```

The plugin calls `GET {base}/whoami` for connect/ping and
`GET {base}/items/{item_id}` for `fetch_item`, both with
`Authorization: Bearer <key>` and a short timeout.

## Manifest

```json
{
  "name": "apikey-demo",
  "display_name": "apikey_demo",
  "description": "API-key example plugin: a non-secret whoami tool and an upstream fetch_item tool mapped to auth-expired or transient failures.",
  "version": "1.0.0",
  "entry": "plugin:ApiKeyDemoPlugin",
  "auth_modes": ["api_key"],
  "min_core_version": "0.1.0"
}
```

The slug `name` must match `[a-z0-9][a-z0-9-]{0,39}` (no underscores);
`apikey_demo` is only the display name.

## Build a package ZIP (Python standard library, no Node)

The host installs a plugin as a ZIP whose root contains `manifest.json` and the
entry module. Build `apikey_demo.zip` from this directory with the standard
library:

```bash
cd plugin_examples/apikey_demo
python - <<'PY'
import zipfile
from pathlib import Path

with zipfile.ZipFile("apikey_demo.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "plugin.py"):
        archive.write(name, Path(name))
print("wrote apikey_demo.zip")
PY
```

`apikey_demo.zip` is what the dashboard import endpoint (`POST /api/plugins/import`,
multipart file with `mode=reject|replace`) or the CLI uploads.

## Try it locally (before the host is wired up)

`plugin.py` is a plain module. Import it directly and drive it with a small
fake context; the snippet below stubs the one HTTP helper so nothing touches the
network:

```bash
python - <<'PY'
import importlib.util

spec = importlib.util.spec_from_file_location("apikey_demo_plugin", "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class MemoryStorage:
    def __init__(self):
        self.values = {}
    def get(self, key, default=None):
        return self.values.get(key, default)
    def set(self, key, value, ttl=None):
        self.values[key] = value
    def delete(self, key):
        if key in self.values:
            del self.values[key]
            return True
        return False
    def list_keys(self, prefix=""):
        return [k for k in self.values if k.startswith(prefix)]

from mcp_plugin_sdk import PluginContext

ctx = PluginContext(
    slug="apikey-demo",
    storage=MemoryStorage(),
    secrets=MemoryStorage(),
    log=lambda level, message: print(level, message),
    callback_url="http://localhost:8000/oauth/apikey-demo/callback",
)
plugin = module.ApiKeyDemoPlugin(ctx)

# The core stores the credential before calling the hook.
ctx.secrets.set("core.api_key", "demo-key")
plugin.on_api_key_set("demo-key")
print([tool.name for tool in plugin.list_tools()])   # ['whoami', 'fetch_item']

class FakeResponse:
    status_code = 200
    def json(self):
        return {"id": "42", "name": "Widget"}

module._http_get = lambda url, headers, timeout: FakeResponse()
print(plugin.connect().status)                        # connected
print(plugin.invoke_tool("whoami", {}))
print(plugin.invoke_tool("fetch_item", {"item_id": "42"}))
PY
```

## Notes

- **Connect/disconnect are driven by the dashboard/API.** The owner supplies the
  key through the connect dialog or `POST /api/plugins/apikey-demo/connect`; the
  core stores it, then calls `on_api_key_set` and `connect()`. `ping` reports
  `connected` only while a key is stored and the last connect succeeded,
  `auth_expired` after a `401`, and `not_connected` otherwise.
- **Call it over MCP.** Once installed and connected, the plugin is served at
  `{BASE_URL}/mcp/apikey-demo/` for any MCP client using
  `Authorization: Bearer <MCP_SECRET>`. Before a key is stored `tools/list`
  shows only `whoami`; connecting swaps in `fetch_item`.
- No secret value, or any part of it, is ever placed in a tool result, a log
  line, or a storage write. Storage keys that start with `core.` are reserved
  and readable-only for plugin code.
