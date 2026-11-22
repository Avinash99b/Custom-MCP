"""Contract tests for the dependency-free plugin SDK."""

from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

import mcp_plugin_sdk
from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Storage,
    Tool,
    ToolError,
    ToolResult,
)


class MemoryStorage:
    """Minimal structural implementation of the ``Storage`` protocol."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._data[key] = value

    def delete(self, key: str) -> bool:
        if key in self._data:
            del self._data[key]
            return True
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return [key for key in self._data if key.startswith(prefix)]


@pytest.fixture
def context() -> PluginContext:
    return PluginContext(
        slug="demo",
        storage=MemoryStorage(),
        secrets=MemoryStorage(),
        log=lambda level, message: None,
        callback_url="https://mcp.example.com/mcp/demo/oauth/callback",
    )


class EchoPlugin(MCPPlugin):
    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.NONE]

    def list_tools(self) -> list[Tool]:
        return [Tool("echo", "Echo input")]

    def connect(self) -> ConnectResult:
        return ConnectResult(Status.CONNECTED)

    def ping(self) -> Status:
        return Status.CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"name": name, "args": args})


# --- exports and enums ------------------------------------------------------


def test_exports_exact() -> None:
    assert set(mcp_plugin_sdk.__all__) == {
        "AuthMode",
        "ConnectResult",
        "MCPPlugin",
        "PluginContext",
        "Status",
        "Storage",
        "Tool",
        "ToolError",
        "ToolResult",
    }


def test_enums_are_str_enum() -> None:
    from enum import Enum, StrEnum

    for enum_cls in (AuthMode, Status):
        assert issubclass(enum_cls, str)
        assert issubclass(enum_cls, Enum)
        assert not issubclass(enum_cls, StrEnum)


def test_enum_values() -> None:
    assert [m.value for m in AuthMode] == ["none", "api_key", "oauth"]
    assert [s.value for s in Status] == [
        "connected",
        "not_connected",
        "oauth_required",
        "auth_expired",
        "error",
    ]
    assert AuthMode.NONE.value == "none"
    assert Status.NOT_CONNECTED.value == "not_connected"


def test_sdk_is_dependency_free() -> None:
    package_dir = Path(mcp_plugin_sdk.__file__).parent
    allowed = set(sys.stdlib_module_names) | {"mcp_plugin_sdk"}
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in allowed, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module is not None
                top = node.module.split(".")[0]
                assert top in allowed, f"{path.name} imports {node.module}"


# --- Tool -------------------------------------------------------------------


def test_tool_default_schema_is_independent() -> None:
    first = Tool("a", "d")
    second = Tool("b", "d")
    assert first.input_schema == {"type": "object", "properties": {}}
    assert first.input_schema["properties"] is not second.input_schema["properties"]
    first.input_schema["properties"]["x"] = {"type": "string"}
    assert second.input_schema["properties"] == {}


def test_tool_is_frozen() -> None:
    tool = Tool("a", "d")
    with pytest.raises(FrozenInstanceError):
        tool.name = "b"  # type: ignore[misc]


# --- results ----------------------------------------------------------------


def test_tool_result_mutable_and_constructors() -> None:
    ok = ToolResult.success({"value": 1})
    assert ok.ok is True
    assert ok.result == {"value": 1}
    assert ok.error is None

    bad = ToolResult.failure("boom", "failed", retryable=True)
    assert bad.ok is False
    assert bad.error == ToolError(code="boom", message="failed", retryable=True)

    ok.ok = False  # mutable
    assert ok.ok is False


def test_tool_result_success_requires_result() -> None:
    with pytest.raises(TypeError):
        ToolResult.success()  # type: ignore[call-arg]


def test_connect_result_is_mutable() -> None:
    result = ConnectResult(status=Status.OAUTH_REQUIRED)
    assert result.redirect_url is None
    result.redirect_url = "https://provider.example.com/authorize"
    assert result.redirect_url == "https://provider.example.com/authorize"


# --- Storage protocol -------------------------------------------------------


def test_storage_signatures() -> None:
    get = inspect.signature(Storage.get)
    assert list(get.parameters) == ["self", "key", "default"]
    assert get.parameters["default"].default is None

    set_sig = inspect.signature(Storage.set)
    assert list(set_sig.parameters) == ["self", "key", "value", "ttl"]
    assert set_sig.parameters["ttl"].default is None

    delete = inspect.signature(Storage.delete)
    assert list(delete.parameters) == ["self", "key"]
    assert delete.return_annotation in (bool, "bool")

    list_keys = inspect.signature(Storage.list_keys)
    assert list(list_keys.parameters) == ["self", "prefix"]
    assert list_keys.parameters["prefix"].default == ""


def test_memory_storage_handles_stored_none() -> None:
    storage = MemoryStorage()
    storage.set("none", None)
    assert storage.get("none") is None
    # A stored None is a real value and wins over the caller-supplied default.
    assert storage.get("none", "fallback") is None
    assert storage.get("absent", "fallback") == "fallback"
    assert storage.list_keys() == ["none"]
    assert storage.delete("none") is True
    assert storage.delete("none") is False
    assert storage.list_keys() == []


# --- MCPPlugin --------------------------------------------------------------


def test_plugin_uses_ctx_attribute(context: PluginContext) -> None:
    plugin = EchoPlugin(context)
    assert plugin.ctx is context
    assert not hasattr(plugin, "context")


def test_plugin_required_signatures() -> None:
    assert list(inspect.signature(MCPPlugin.auth_methods).parameters) == ["self"]
    assert list(inspect.signature(MCPPlugin.list_tools).parameters) == ["self"]
    assert list(inspect.signature(MCPPlugin.connect).parameters) == ["self"]
    assert list(inspect.signature(MCPPlugin.ping).parameters) == ["self"]
    invoke = inspect.signature(MCPPlugin.invoke_tool)
    assert list(invoke.parameters) == ["self", "name", "args"]

    assert list(inspect.signature(MCPPlugin.__init__).parameters) == ["self", "ctx"]


def test_plugin_abstract_methods_enforced() -> None:
    class Incomplete(MCPPlugin):
        pass

    with pytest.raises(TypeError):
        Incomplete(None)  # type: ignore[abstract, arg-type]


def test_optional_hooks_accept_arguments(context: PluginContext) -> None:
    plugin = EchoPlugin(context)
    plugin.on_api_key_set("secret-key")
    plugin.on_oauth_credentials_set("client-id", "client-secret")
    plugin.on_oauth_credentials_set("client-id", None)
    plugin.on_restore()
    plugin.on_disconnect()
    plugin.shutdown()


def test_hook_signatures() -> None:
    assert list(inspect.signature(MCPPlugin.on_api_key_set).parameters) == ["self", "api_key"]
    assert list(inspect.signature(MCPPlugin.on_oauth_credentials_set).parameters) == [
        "self",
        "client_id",
        "client_secret",
    ]
    assert list(inspect.signature(MCPPlugin.get_oauth_sign_in_url).parameters) == ["self", "state"]
    assert list(inspect.signature(MCPPlugin.on_oauth_result).parameters) == [
        "self",
        "code",
        "state",
    ]


def test_default_refresh_returns_auth_expired(context: PluginContext) -> None:
    plugin = EchoPlugin(context)
    assert plugin.refresh() == Status.AUTH_EXPIRED


def test_default_oauth_hooks_raise_not_implemented(context: PluginContext) -> None:
    plugin = EchoPlugin(context)
    with pytest.raises(NotImplementedError):
        plugin.get_oauth_sign_in_url("state-token")
    with pytest.raises(NotImplementedError):
        plugin.on_oauth_result("code", "state-token")
