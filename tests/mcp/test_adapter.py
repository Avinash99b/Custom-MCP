"""Unit tests for the per-plugin FastMCP adapter (Phase 3, adapter slice).

These exercise the adapter in-process: no wire client is required. The fake host
records every call and proves it was awaited, and a plugin sentinel proves the
adapter never reaches the plugin instance directly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.tools.tool import Tool as FastMCPTool
from fastmcp.tools.tool import ToolResult as FastMCPToolResult

from custom_mcp.mcp.adapter import build_plugin_app
from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.manifest import Manifest
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)

NESTED_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "payload": {"$ref": "#/$defs/item"},
        "count": {"type": "integer", "default": 1},
    },
    "required": ["payload"],
    "additionalProperties": False,
    "$defs": {
        "item": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        }
    },
}

FLAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


class _Storage:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.data[key] = value

    def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.data if key.startswith(prefix))


class _ExplodingPlugin(MCPPlugin):
    """A plugin that must never be invoked directly by the adapter."""

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return []

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self) -> Status:
        return Status.NOT_CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("adapter bypassed the host and called the plugin directly")


class _StubHost:
    """A fake host that records calls and whether it was awaited."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[PluginRuntime, str, tuple[Any, ...]]] = []
        self.awaited = False

    async def call(
        self, runtime: PluginRuntime, method: str, *args: Any
    ) -> Any:
        self.awaited = True
        self.calls.append((runtime, method, args))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _context(slug: str = "demo") -> PluginContext:
    return PluginContext(
        slug=slug,
        storage=_Storage(),
        secrets=_Storage(),
        log=lambda level, message: None,
        callback_url=f"https://mcp.example.com/mcp/{slug}/oauth/callback",
    )


def _manifest(slug: str = "demo") -> Manifest:
    return Manifest(
        name=slug,
        version="1.0.0",
        entry="plugin:Plugin",
        auth_modes=(AuthMode.NONE,),
        display_name=slug,
        description="",
    )


def _runtime(
    tools: list[Tool],
    *,
    slug: str = "demo",
    status: Status = Status.CONNECTED,
) -> PluginRuntime:
    context = _context(slug)
    return PluginRuntime(
        slug=slug,
        manifest=_manifest(slug),
        context=context,
        lock=asyncio.Lock(),
        sem=asyncio.Semaphore(4),
        load_state=LoadState.LOADED,
        instance=_ExplodingPlugin(context),
        tools=tools,
        status=status,
    )


def _build(runtime: PluginRuntime, host: _StubHost) -> FastMCP[Any]:
    app = build_plugin_app(runtime, cast("PluginHost", host))
    assert isinstance(app, StarletteWithLifespan)
    assert app.state.path == "/"
    server = app.state.fastmcp_server
    assert isinstance(server, FastMCP)
    return server


async def _tool(server: FastMCP[Any], name: str) -> FastMCPTool:
    tools = await server.get_tools()
    return tools[name]


def _text(result: FastMCPToolResult) -> str:
    blocks = result.content
    assert blocks, "expected at least one content block"
    text = getattr(blocks[0], "text", None)
    assert isinstance(text, str)
    return text


async def test_schema_preserved_byte_for_byte() -> None:
    tool = Tool(name="put", description="Put an item.", input_schema=NESTED_SCHEMA)
    runtime = _runtime([tool])
    server = _build(runtime, _StubHost(ToolResult.success({})))

    registered = await _tool(server, "put")
    assert registered.name == "put"
    assert registered.description == "Put an item."
    assert registered.parameters == NESTED_SCHEMA
    # Key order is preserved too (no normalization / reordering).
    assert json.dumps(registered.parameters) == json.dumps(NESTED_SCHEMA)

    advertised = registered.to_mcp_tool()
    assert advertised.name == "put"
    assert advertised.description == "Put an item."
    assert advertised.inputSchema == NESTED_SCHEMA
    assert json.dumps(advertised.inputSchema) == json.dumps(NESTED_SCHEMA)


async def test_invalid_arguments_produce_uniform_error() -> None:
    first = Tool(name="put", description="d", input_schema=NESTED_SCHEMA)
    second = Tool(name="count", description="d", input_schema=FLAT_SCHEMA)
    host = _StubHost(ToolResult.success({}))
    server = _build(_runtime([first, second]), host)

    put = await _tool(server, "put")
    count = await _tool(server, "count")

    with pytest.raises(ToolError) as put_error:
        await put.run({"payload": {"label": 5}, "extra": True})
    with pytest.raises(ToolError) as count_error:
        await count.run({"value": "not-an-int"})

    assert str(put_error.value).startswith(
        "[invalid_arguments] Invalid arguments for tool 'put':"
    )
    assert str(count_error.value).startswith(
        "[invalid_arguments] Invalid arguments for tool 'count':"
    )
    assert "failed the" in str(put_error.value)
    # No argument values are echoed back.
    assert "5" not in str(put_error.value)
    assert "not-an-int" not in str(count_error.value)
    # Validation happens before any host call.
    assert host.calls == []


async def test_valid_arguments_proceed_to_host() -> None:
    tool = Tool(name="put", description="d", input_schema=NESTED_SCHEMA)
    host = _StubHost(ToolResult.success({"stored": True}))
    runtime = _runtime([tool])
    server = _build(runtime, host)

    registered = await _tool(server, "put")
    arguments = {"payload": {"label": "ok"}}
    result = await registered.run(arguments)

    assert isinstance(result, FastMCPToolResult)
    assert host.awaited is True
    assert host.calls == [(runtime, "invoke_tool", ("put", arguments))]


async def test_success_dict_maps_to_structured_and_json_text() -> None:
    payload = {"answer": 42, "nested": {"ok": True}}
    tool = Tool(name="put", description="d", input_schema=FLAT_SCHEMA)
    host = _StubHost(ToolResult.success(payload))
    server = _build(_runtime([tool]), host)

    registered = await _tool(server, "put")
    result = await registered.run({"value": 1})

    assert result.structured_content == payload
    assert json.loads(_text(result)) == payload


async def test_failure_maps_code_message_and_retryable() -> None:
    tool = Tool(name="put", description="d", input_schema=FLAT_SCHEMA)

    retry_host = _StubHost(ToolResult.failure("upstream", "Upstream is busy.", True))
    retry_server = _build(_runtime([tool]), retry_host)
    retry_tool = await _tool(retry_server, "put")
    with pytest.raises(ToolError) as retry_error:
        await retry_tool.run({"value": 1})
    assert str(retry_error.value) == "[upstream] Upstream is busy. (retryable)"

    plain_host = _StubHost(ToolResult.failure("denied", "Not allowed."))
    plain_server = _build(_runtime([tool]), plain_host)
    plain_tool = await _tool(plain_server, "put")
    with pytest.raises(ToolError) as plain_error:
        await plain_tool.run({"value": 1})
    assert str(plain_error.value) == "[denied] Not allowed."


async def test_host_timeout_is_retryable_and_exception_is_not() -> None:
    tool = Tool(name="put", description="d", input_schema=FLAT_SCHEMA)

    timeout_server = _build(
        _runtime([tool]),
        _StubHost(PluginCallError("timeout", "Plugin call timed out.")),
    )
    timeout_tool = await _tool(timeout_server, "put")
    with pytest.raises(ToolError) as timeout_error:
        await timeout_tool.run({"value": 1})
    assert str(timeout_error.value) == "[timeout] Plugin call timed out. (retryable)"

    exception_server = _build(
        _runtime([tool]),
        _StubHost(PluginCallError("exception", "Plugin raised an exception.")),
    )
    exception_tool = await _tool(exception_server, "put")
    with pytest.raises(ToolError) as exception_error:
        await exception_tool.run({"value": 1})
    assert str(exception_error.value) == "[exception] Plugin raised an exception."


async def test_disconnected_call_is_not_connected_but_tool_is_listed() -> None:
    tool = Tool(name="put", description="d", input_schema=FLAT_SCHEMA)
    runtime = _runtime([tool], slug="demo", status=Status.NOT_CONNECTED)
    server = _build(runtime, _StubHost(ToolResult.success({})))

    # The tool is still advertised while disconnected.
    tools = await server.get_tools()
    assert "put" in tools

    registered = await _tool(server, "put")
    with pytest.raises(ToolError) as error:
        await registered.run({"value": 1})
    assert str(error.value) == (
        "[not_connected] Plugin 'demo' is not connected. "
        "Reconnect it in the dashboard."
    )


async def test_plugin_exception_detail_never_reaches_client() -> None:
    tool = Tool(name="put", description="d", input_schema=FLAT_SCHEMA)
    leak = "super-secret-credential"
    host = _StubHost(
        PluginCallError(
            "exception",
            "Plugin raised an exception.",
            detail_log=f"RuntimeError: {leak}",
        )
    )
    server = _build(_runtime([tool]), host)
    registered = await _tool(server, "put")

    with pytest.raises(ToolError) as error:
        await registered.run({"value": 1})
    assert leak not in str(error.value)
    assert str(error.value) == "[exception] Plugin raised an exception."
