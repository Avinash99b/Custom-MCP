# Hello plugin example

The smallest complete custom MCP plugin. It uses only the dependency-free SDK
(`mcp_plugin_sdk`) and the Python standard library, advertises auth mode `none`,
and exposes two tools:

| Tool | Arguments | Result |
| --- | --- | --- |
| `echo` | `text` (string, required) | `{"text": "<text>"}` |
| `add` | `a`, `b` (numbers, required) | `{"sum": a + b}` |

Every **successful** tool call increments the scoped storage key `call_count`
read-modify-write under a lock, so the counter stays correct when the host runs
calls concurrently on worker threads. Unknown tool names return a
`not_found` failure without touching the counter.

`connect()` and `ping()` both report `Status.CONNECTED`. The optional hooks
(`refresh`, `shutdown`, `on_disconnect`, …) use the SDK defaults; they are not
overridden here.

## Manifest

```json
{
  "name": "hello",
  "display_name": "Hello",
  "description": "Minimal example plugin: echo and add tools plus a persisted call counter.",
  "version": "1.0.0",
  "entry": "plugin:HelloPlugin",
  "auth_modes": ["none"],
  "min_core_version": "0.1.0"
}
```

## Build a package ZIP (Python standard library, no Node)

The host installs a plugin as a ZIP whose root contains `manifest.json` and the
entry module. Build `hello.zip` from this directory with the standard library:

```bash
cd plugin_examples/hello
python - <<'PY'
import zipfile
from pathlib import Path

with zipfile.ZipFile("hello.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "plugin.py"):
        archive.write(name, Path(name))
print("wrote hello.zip")
PY
```

`hello.zip` is what the dashboard import endpoint (`POST /api/plugins/import`, multipart
file with `mode=reject|replace`) or the CLI uploads.

## Try it locally (before the host is wired up)

`plugin.py` is a plain module. Import it directly and drive it with a small
fake context:

```bash
python - <<'PY'
import importlib.util

spec = importlib.util.spec_from_file_location("hello_plugin", "plugin.py")
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
    slug="hello",
    storage=MemoryStorage(),
    secrets=MemoryStorage(),
    log=lambda level, message: print(level, message),
    callback_url="http://localhost:8000/oauth/hello/callback",
)
plugin = module.HelloPlugin(ctx)
plugin.connect()
print(plugin.invoke_tool("echo", {"text": "hi"}))
print(plugin.invoke_tool("add", {"a": 2, "b": 40}))
PY
```

## Notes

- **Install and call it over MCP.** Import `hello.zip` through the dashboard or
  `POST /api/plugins/import`, then point any MCP client at
  `{BASE_URL}/mcp/hello/` with `Authorization: Bearer <MCP_SECRET>`
  (`initialize`, `tools/list`, `tools/call`). Because the auth mode is `none` it
  connects automatically on install and startup. This is the core example all
  other examples build on.
- Auth mode `none` means the plugin connects without credentials; it never sees
  the owner secret, Redis, or core settings.
- Storage values are JSON-serializable and scoped to `hello`; keys that start
  with `core.` are reserved and rejected for plugin writes.
