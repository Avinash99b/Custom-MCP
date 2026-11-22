"""Hello example plugin: dependency-free use of the custom MCP plugin SDK.

The host loads this module as the isolated package ``mcp_plugin_hello`` and
resolves ``HelloPlugin`` from the manifest entry ``plugin:HelloPlugin``. Every
method runs on a host worker thread, so the call counter is guarded by a lock to
stay correct under concurrent tool calls.
"""

from __future__ import annotations

import threading
from typing import Any

from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)

_CALL_COUNT_KEY = "call_count"


def _echo_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for the ``echo`` tool (no shared nested objects)."""
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to echo back."},
        },
        "required": ["text"],
        "additionalProperties": False,
    }


def _add_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for the ``add`` tool."""
    return {
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "First addend."},
            "b": {"type": "number", "description": "Second addend."},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    }


class HelloPlugin(MCPPlugin):
    """Echo/add example that counts successful tool calls in scoped storage."""

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        # Serialize the read-modify-write of the counter across worker threads.
        self._counter_lock = threading.Lock()

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="echo",
                description="Return the supplied text unchanged.",
                input_schema=_echo_schema(),
            ),
            Tool(
                name="add",
                description="Add two numbers and return their sum.",
                input_schema=_add_schema(),
            ),
        ]

    def connect(self) -> ConnectResult:
        return ConnectResult(status=Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "echo":
            result = ToolResult.success({"text": args["text"]})
        elif name == "add":
            result = ToolResult.success({"sum": args["a"] + args["b"]})
        else:
            return ToolResult.failure("not_found", f"Unknown tool: {name}")
        self._increment_call_count()
        return result

    def _increment_call_count(self) -> None:
        with self._counter_lock:
            count = self.ctx.storage.get(_CALL_COUNT_KEY, 0)
            self.ctx.storage.set(_CALL_COUNT_KEY, int(count) + 1)
