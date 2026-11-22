"""Dependency-free plugin SDK shared by the host and plugin authors.

Only the Python standard library may be imported by this package.
"""

from __future__ import annotations

from mcp_plugin_sdk.base import MCPPlugin
from mcp_plugin_sdk.context import PluginContext, Storage
from mcp_plugin_sdk.types import (
    AuthMode,
    ConnectResult,
    Status,
    Tool,
    ToolError,
    ToolResult,
)

__version__ = "0.1.0"

__all__ = [
    "AuthMode",
    "ConnectResult",
    "MCPPlugin",
    "PluginContext",
    "Status",
    "Storage",
    "Tool",
    "ToolError",
    "ToolResult",
]
