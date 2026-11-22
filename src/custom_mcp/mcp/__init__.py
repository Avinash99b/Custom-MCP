"""Per-plugin FastMCP adapter and dynamic router (Phase 3).

This package is the single bridge between a plugin's cached SDK tools and the
MCP wire. :func:`build_plugin_app` returns a streamable-HTTP ASGI app for one
immutable :class:`PluginRuntime` generation; :class:`MCPRouter` mounts those apps
per slug at ``/mcp/{slug}`` and swaps/unmounts them live without touching the
plugin instance directly.
"""

from __future__ import annotations

from custom_mcp.mcp.adapter import build_plugin_app
from custom_mcp.mcp.router import MCPRouter

__all__ = ["MCPRouter", "build_plugin_app"]
