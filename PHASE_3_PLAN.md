# Phase 3 plan — MCP adapter, dynamic router, hot-swap

Goal: mount each loaded plugin as a streamable-HTTP MCP server at `/mcp/{slug}/`,
list/call its cached tools over the wire with bearer auth, and swap/unmount live
without disturbing in-flight calls.

## Acceptance gate (TEST_MATRIX row 3)
Actual streamable HTTP MCP initialize/list/call using bearer at both slash forms;
cached tools listed while disconnected; exact `inputSchema` including refs /
defaults / `additionalProperties`; structured and JSON text output; uniform
argument errors; mounted lifespan entered/exited; live swap removes old tools;
in-flight old call remains bound to the old instance; failed upgrade restores old
package/runtime/state.

## Pinned-library facts (verified)
- FastMCP **2.14.7**; `FastMCP.http_app(path="/", stateless_http=True,
  json_response=True)` returns a `StarletteWithLifespan` with an explicit
  `router.lifespan_context`.
- `FastMCP.add_tool(tool)` accepts a `fastmcp.tools.Tool`; subclasses implement
  async `run(arguments)`; the tool carries a raw `parameters` schema.
- `fastmcp.tools.tool.ToolResult` is the result type (not `fastmcp.tools`).
- A FastMCP app's lifespan owns AnyIO cancel scopes / contextvars and **must be
  entered and exited in the same task**. Each mounted app needs a dedicated
  lifespan-owner task (enter → signal ready → await retire/drain → exit).

## Interfaces
- `custom_mcp.mcp.adapter`: build one FastMCP app per immutable `PluginRuntime`.
  - One fastmcp tool per cached SDK `Tool`, preserving `input_schema` byte-for-
    byte as the raw `parameters`.
  - `run(arguments)` validates arguments explicitly with `Draft202012Validator`
    against the raw schema; invalid args raise a uniform MCP argument error.
  - Execution goes through `PluginHost.call(runtime, "invoke_tool", name, args)`
    so it stays off the event loop and bounded by the per-plugin semaphore.
  - Success dict -> structured content **and** JSON text; failure ->
    `[code] message` (+ ` (retryable)` when applicable).
- `custom_mcp.mcp.router`: ASGI app served at `/mcp`.
  - Resolve `{slug}/...`, forward to that runtime's app with correct
    `path`/`root_path` rewriting (both trailing-slash forms).
  - Keep an immutable per-slug lookup of the current generation; a swap is atomic.
  - Each mounted app runs under a dedicated lifespan-owner task; retire drains
    ~5s before the lifespan exits in the same task.
  - Mount/unmount on load/unload/replace; `runtime.mounted` holds the app.
- `main.py`: replace the `/mcp/{plugin}[/{rest:path}]` placeholder with the
  router; keep bearer auth enforced (never public).

## Ownership
- Adapter slice owns `src/custom_mcp/mcp/{__init__,adapter}.py` and its unit tests.
- Router slice owns `src/custom_mcp/mcp/router.py`, `main.py` wiring, `runtime.py`
  mounted field usage, and wire/integration tests.
- Interfaces above are frozen before the router slice starts.

## Evidence required
- MCP-client wire tests (streamable HTTP) at both slash forms with bearer.
- Exact-schema tests: nested `$defs`/`$ref`, defaults, `additionalProperties`.
- Hot-swap test: old tools disappear; an in-flight old call completes on the old
  instance; failed replace restores old code/runtime/state.
- Focused tests, then full suite, Ruff, mypy.
