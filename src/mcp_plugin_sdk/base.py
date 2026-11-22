"""The plugin base class: required methods and optional hooks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mcp_plugin_sdk.context import PluginContext
from mcp_plugin_sdk.types import AuthMode, ConnectResult, Status, Tool, ToolResult


class MCPPlugin(ABC):
    """Base class for a trusted in-process synchronous plugin.

    The host constructs the plugin with its :class:`PluginContext` and calls all
    methods off the event loop on a worker thread. Required methods must be
    implemented; optional hooks have safe defaults.
    """

    ctx: PluginContext

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def auth_methods(self) -> list[AuthMode]:
        """Return the authentication modes this plugin supports (nonempty)."""

    @abstractmethod
    def list_tools(self) -> list[Tool]:
        """Return the tools this plugin exposes."""

    @abstractmethod
    def connect(self) -> ConnectResult:
        """Attempt to connect; return the resulting status."""

    @abstractmethod
    def ping(self) -> Status:
        """Check connection health and return the current status."""

    @abstractmethod
    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Invoke a tool by name with validated arguments."""

    # --- optional hooks -----------------------------------------------------

    def on_api_key_set(self, api_key: str) -> None:
        """Called after ``core.api_key`` has been stored for an api_key connect."""
        return None

    def on_oauth_credentials_set(self, client_id: str, client_secret: str | None) -> None:
        """Called after OAuth client credentials have been stored."""
        return None

    def get_oauth_sign_in_url(self, state: str) -> str:
        """Return a provider sign-in URL for ``state``."""
        raise NotImplementedError

    def on_oauth_result(self, code: str, state: str) -> ConnectResult:
        """Handle an OAuth authorization code callback."""
        raise NotImplementedError

    def refresh(self) -> Status:
        """Refresh authentication; defaults to reporting an expired auth."""
        return Status.AUTH_EXPIRED

    def on_restore(self) -> None:
        """Called after a backup restore has restored this plugin's state."""
        return None

    def on_disconnect(self) -> None:
        """Called when the owner disconnects the plugin."""
        return None

    def shutdown(self) -> None:
        """Called during host shutdown or plugin removal."""
        return None


__all__ = ["MCPPlugin"]
