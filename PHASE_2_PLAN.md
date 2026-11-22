# Phase 2 boundaries (pending Phase 1 gate)

Phase 2 starts only after Phase 1's definition of done and independent reviews
pass. Its outcome is loaded synchronous plugins with scoped storage, bounded
host calls, cached validated tools, lifecycle cleanup and startup recovery.
FastMCP exposure remains Phase 3; API-key connect remains Phase 4.

## Parallel ownership

1. **Scoped storage and redacted logs**
   - New `storage/manager.py`, `plugins/logs.py`, focused tests.
   - Existing `storage/redis_client.py` belongs exclusively to this unit while
     required sync/async commands are added; other agents propose requirements.
   - Storage implements the exact SDK protocol in SDK_CONTRACT.md, protects
     reserved `core.` writes/deletes, validates keys and JSON <=1MiB, supports
     TTL and SCAN prefix listing. Core gets a separate explicit reserved-write
     interface; plugin contexts never get the manager or raw Redis.
   - Logger exposes synchronous plugin-thread callback and async core events,
     both with current-secret redaction and LPUSH/LTRIM 200-entry cap. Public
     diagnostics containing plugin-supplied messages need the same redaction.

2. **Runtime, host and loader**
   - New `plugins/runtime.py`, `host.py`, `loader.py`, focused tests.
   - Runtime uses SDK Status and one stable per-slug lifecycle lock retained by
     the manager. Generation/instance identity remains immutable for a retired
     runtime; a future MCP app must not call a swapped-in instance accidentally.
   - Host owns a shared ThreadPoolExecutor and handles every plugin method and
     construction/import execution off-loop where practical. Validate return
     shape and JSON serialization without trusting annotations.
   - Timed-out work is not killed. Release semaphore on timeout, track unfinished
     timed-out workers exactly, decrement on late completion, fail overloaded
     at the per-plugin leaked threshold. Handle caller cancellation deliberately.
   - Loader isolates `mcp_plugin_{slug}` packages and relative imports, validates
     subclass/auth/tool/schema contracts, performs five-second shutdown and
     removes all matching sys.modules entries. Failed load stays visible.

3. **Hello example and lifecycle fixtures**
   - Only `plugin_examples/hello/`, new `tests/runtime_cases/` fixtures/tests.
   - Exact SDK methods, echo/add tools, call counter, NONE auto-connect.
   - Self-contained fixture packages for crashes, malformed results, changing
     tools, relative imports, blocked thread barriers and failed auth contracts.
   - This unit must not edit global conftest or production SDK/runtime modules.

4. **Manager/API/lifespan integration**
   - Sequence assembly after storage/host/loader interfaces stabilize. Own
     `plugins/manager.py`, `api/plugins.py`, `main.py`, focused lifecycle
     integration tests, shared fixtures/helpers, README and pyproject/lock if
     type-stub/dev dependency integration is necessary.
   - Load on install/startup, expose cached tools/load states, reload, unload on
     removal/shutdown, isolated load failures, reconnect NONE or selected stored
     auth at startup. Credential submission is not implemented before Phase 4.
   - Preserve Phase 1 store-only transaction tests and scoped disk/Redis deletion.
     Runtime loading adds deliberate rollback ownership rather than bypassing
     publication safety. Full failed-upgrade MCP behavior is completed Phase 3.
   - JSON-schema validation uses Draft202012Validator.check_schema; all schemas
     must declare object type, tool names regex and uniqueness enforced.

## Interfaces to agree before assembly

- Runtime constructor fields, load state enum, metadata/status mirror, lock and
  semaphore ownership, logger/error callback, immutable plugin generation.
- `PluginHost.call(runtime, method, *args, timeout=None)` plus structured
  PluginCallError kinds (timeout, exception, plugin_overloaded, malformed result).
- Loader load/unload APIs and host-mediated constructor/import path.
- StorageManager context factory/core secret writer/snapshot/read names APIs.
- Logs synchronous callback/async core event/redact APIs.

Interfaces must follow the user's contracts, not remove credential/state hook
arguments, alter refresh return types or pass raw Redis to plugins.

## Evidence required

Hello loads/caches tools; isolated imports; invalid subclass/auth/schema/tool
contracts contained; slow/crashing/malformed plugin handling; per-plugin limiting
and leaked-worker race tests; scoped storage restrictions/TTL/SCAN; log cap and
redaction; module cleanup; restart fresh extraction and reconnect; API load-failed
visibility/reload/remove; shutdown resource ordering. Focused tests first, then
full tests/Ruff/type checks, critic >8.5 and manager integration verification.
