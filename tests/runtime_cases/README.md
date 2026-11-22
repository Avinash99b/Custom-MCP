# `tests/runtime_cases` — Phase 2 runtime fixtures

Dependency-free builders for plugin packages a loader/host can install and
execute, plus structural tests and direct hello-example tests.

## Files

- `builders.py` — `make_runtime_zip(...)`, the `*_SOURCE` code snippets and
  `fixture_cases()` / `fixture_names()`.
- `test_fixtures.py` — structural fixture checks (no fixture code is executed)
  and direct execution of `plugin_examples/hello`.

## Builder API (stable)

```python
from tests.runtime_cases.builders import fixture_cases, fixture_names, make_runtime_zip

data = make_runtime_zip(
    "hello",                       # slug (manifest "name")
    {"plugin.py": HELLO_CLONE_SOURCE},
    auth_modes=("none",),          # default
    version="1.0.0",               # default
    entry="plugin:FixturePlugin",  # default
    display_name=None,
    min_core_version=None,
    extra_manifest=None,           # merge extra manifest keys
)
```

`make_runtime_zip` writes a generated `manifest.json` plus `files` (values may be
`str` or `bytes`) into a deterministic ZIP (fixed 1980 timestamp) using only
`io`, `json`, `zipfile`. It never imports the SDK or production code.

## Fixture cases (`fixture_cases()` -> `name -> ZIP bytes`)

| Name | Intent |
| --- | --- |
| `hello_clone` | Minimal valid plugin (echo tool), generic happy path. |
| `relative_import_subpackage` | `plugin.py` does `from .helpers import greet`; ships `helpers/__init__.py` for isolated-loader relative imports. |
| `crashing_constructor` | `__init__` raises. |
| `crashing_auth` | `auth_methods` raises. |
| `crashing_list` | `list_tools` raises. |
| `crashing_invoke` | Valid `echo` catalog; `invoke_tool` raises. |
| `crashing_ping` | `ping` raises. |
| `slow_method` | `ping` blocks, bounded by `SLOW_SECONDS = 0.5`; releases early when the loaded module's `RELEASE_EVENT` (`threading.Event`) is `set()`. |
| `malformed_tool_result` | Valid `echo` catalog; `invoke_tool` returns a plain dict, not a `ToolResult`. |
| `auth_manifest_mismatch` | `auth_methods()` returns `[AuthMode.API_KEY]` while the manifest says `none`. |
| `duplicate_tool_names` | Two tools named `dup`. |
| `invalid_tool_name` | Tool name `bad name!` outside `[a-zA-Z0-9_-]{1,64}`. |
| `schema_not_object` | Tool schema is `{"type": "string"}` (no object type). |
| `invalid_schema` | Tool schema property type `not-a-real-type` fails Draft202012 `check_schema`. |
| `changing_tools_after_connect` | `list_tools()` returns `before` then `after` once `connect()` runs. |
| `shutdown_marker` | `shutdown()` writes `storage["shutdown"] = True`. |
| `constructor_thread_id` | Records module-global `IMPORT_THREAD_ID` and the constructor thread id into scoped storage. |

Host-side hooks for executing tests:

- `mcp_plugin_<slug>.RELEASE_EVENT` — set it to release `slow_method` early.
- `mcp_plugin_<slug>.IMPORT_THREAD_ID` — import-time thread id for
  `constructor_thread_id`.

All fixtures are valid ZIPs with valid manifests; the defect cases only fail
when a loader inspects or executes them. No network, Redis, subprocess or live
server is used, and nothing here writes into `plugin_examples/`.
