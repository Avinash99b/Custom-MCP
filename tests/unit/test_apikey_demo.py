"""Unit tests for the ``apikey_demo`` example plugin.

The example module is imported directly from ``plugin_examples/apikey_demo`` and
driven with an in-memory storage/secret context. Every HTTP call is served by a
monkeypatched ``_http_get``, so nothing touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

from mcp_plugin_sdk import AuthMode, PluginContext, Status

EXAMPLE_DIR = (
    Path(__file__).resolve().parents[2] / "plugin_examples" / "apikey_demo"
)
PLUGIN_PATH = EXAMPLE_DIR / "plugin.py"
MANIFEST_PATH = EXAMPLE_DIR / "manifest.json"


class MemoryStorage:
    """Minimal in-memory implementation of the SDK ``Storage`` protocol."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.values[key] = value

    def delete(self, key: str) -> bool:
        if key in self.values:
            del self.values[key]
            return True
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.values if key.startswith(prefix))


class FakeResponse:
    """A stand-in for ``requests.Response`` used by the HTTP helper."""

    def __init__(
        self, status_code: int, payload: Any = None, *, json_raises: bool = False
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("no json body")
        return self._payload


@pytest.fixture(scope="module")
def example() -> Any:
    """Import the example plugin module directly from its source file."""
    spec = importlib.util.spec_from_file_location("apikey_demo_plugin_under_test", PLUGIN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(autouse=True)
def _clear_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUSTOM_MCP_APIKEY_DEMO_BASE_URL", raising=False)


def _make_plugin(
    module: Any, *, key: str | None = None
) -> tuple[Any, MemoryStorage, MemoryStorage, list[tuple[str, str]]]:
    storage = MemoryStorage()
    secrets = MemoryStorage()
    if key is not None:
        secrets.set("core.api_key", key)
    logs: list[tuple[str, str]] = []
    ctx = PluginContext(
        slug="apikey-demo",
        storage=storage,
        secrets=secrets,
        log=lambda level, message: logs.append((level, message)),
        callback_url="https://mcp.example.com/oauth/apikey-demo/callback",
    )
    return module.ApiKeyDemoPlugin(ctx), storage, secrets, logs


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    response: FakeResponse | None = None,
    exc: BaseException | None = None,
    capture: list[dict[str, Any]] | None = None,
) -> None:
    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        if capture is not None:
            capture.append({"url": url, "headers": headers, "timeout": timeout})
        if exc is not None:
            raise exc
        assert response is not None
        return response

    monkeypatch.setattr(module, "_http_get", fake_get)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_is_valid_json_with_api_key_mode() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert parsed["name"] == "apikey-demo"
    assert parsed["display_name"] == "apikey_demo"
    assert parsed["entry"] == "plugin:ApiKeyDemoPlugin"
    assert parsed["auth_modes"] == ["api_key"]


def test_manifest_passes_production_validator() -> None:
    from custom_mcp.plugins.manifest import parse_manifest

    manifest = parse_manifest(MANIFEST_PATH.read_bytes())
    assert manifest.name == "apikey-demo"
    assert manifest.display_name == "apikey_demo"
    assert manifest.entry_class == "ApiKeyDemoPlugin"
    assert [mode.value for mode in manifest.auth_modes] == ["api_key"]


# ---------------------------------------------------------------------------
# Auth methods and the changed tool set
# ---------------------------------------------------------------------------


def test_auth_methods_is_api_key_only(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert plugin.auth_methods() == [AuthMode.API_KEY]


def test_list_tools_without_key_has_only_whoami(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    tools = plugin.list_tools()
    assert [tool.name for tool in tools] == ["whoami"]


def test_list_tools_with_key_adds_fetch_item(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    tools = plugin.list_tools()
    assert [tool.name for tool in tools] == ["whoami", "fetch_item"]
    fetch = next(tool for tool in tools if tool.name == "fetch_item")
    assert fetch.input_schema["required"] == ["item_id"]
    assert fetch.input_schema["properties"]["item_id"]["type"] == "string"


def test_list_tools_returns_independent_schemas(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    first = plugin.list_tools()
    fetch = next(tool for tool in first if tool.name == "fetch_item")
    fetch.input_schema["properties"]["item_id"]["type"] = "integer"
    second = plugin.list_tools()
    fetch_again = next(tool for tool in second if tool.name == "fetch_item")
    assert fetch_again.input_schema["properties"]["item_id"]["type"] == "string"


def test_tool_set_changes_once_key_is_stored(example: Any) -> None:
    plugin, _, secrets, _ = _make_plugin(example)
    assert [tool.name for tool in plugin.list_tools()] == ["whoami"]
    secrets.set("core.api_key", "k")
    assert [tool.name for tool in plugin.list_tools()] == ["whoami", "fetch_item"]


# ---------------------------------------------------------------------------
# on_api_key_set observes the core-stored credential
# ---------------------------------------------------------------------------


def test_on_api_key_set_observes_core_stored_secret(example: Any) -> None:
    plugin, storage, secrets, _ = _make_plugin(example, key="k")
    assert secrets.get("core.api_key") == "k"
    plugin.on_api_key_set("k")
    assert storage.get("api_key_hook") == {"stored": True, "matched": True}


def test_on_api_key_set_records_mismatch_without_raising(example: Any) -> None:
    plugin, storage, _, _ = _make_plugin(example, key="stored-key")
    plugin.on_api_key_set("different-key")
    assert storage.get("api_key_hook") == {"stored": True, "matched": False}


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_without_key_is_not_connected(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTP call expected without a key")

    monkeypatch.setattr(example, "_http_get", fail)
    result = plugin.connect()
    assert result.status is Status.NOT_CONNECTED
    assert result.message == "No API key."


def test_connect_with_key_and_200_is_connected(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(200, {}))
    assert plugin.connect().status is Status.CONNECTED


def test_connect_with_401_is_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(401))
    assert plugin.connect().status is Status.AUTH_EXPIRED


def test_connect_with_timeout_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, exc=requests.Timeout("slow"))
    assert plugin.connect().status is Status.ERROR


def test_connect_with_5xx_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(503))
    assert plugin.connect().status is Status.ERROR


# ---------------------------------------------------------------------------
# ping()
# ---------------------------------------------------------------------------


def test_ping_without_key_is_not_connected(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTP call expected without a key")

    monkeypatch.setattr(example, "_http_get", fail)
    assert plugin.ping() is Status.NOT_CONNECTED


def test_ping_connected_after_successful_connect(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(200, {}))
    assert plugin.connect().status is Status.CONNECTED
    assert plugin.ping() is Status.CONNECTED


def test_ping_after_401_is_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(401))
    assert plugin.ping() is Status.AUTH_EXPIRED


# ---------------------------------------------------------------------------
# invoke_tool: whoami
# ---------------------------------------------------------------------------


def test_whoami_reports_presence_without_key_value(example: Any) -> None:
    secret = "super-secret-key-123"
    plugin, _, _, _ = _make_plugin(example, key=secret)
    result = plugin.invoke_tool("whoami", {})
    assert result.ok is True
    assert result.result == {
        "mode": "api_key",
        "connected": False,
        "credential_present": True,
    }
    assert secret not in json.dumps(result.result)


def test_whoami_without_key(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("whoami", {})
    assert result.result == {
        "mode": "api_key",
        "connected": False,
        "credential_present": False,
    }


# ---------------------------------------------------------------------------
# invoke_tool: fetch_item
# ---------------------------------------------------------------------------


def test_fetch_item_200_returns_success_dict(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-key-123"
    plugin, _, _, _ = _make_plugin(example, key=secret)
    capture: list[dict[str, Any]] = []
    _install_http(
        monkeypatch,
        example,
        response=FakeResponse(200, {"id": "42", "name": "Widget"}),
        capture=capture,
    )
    result = plugin.invoke_tool("fetch_item", {"item_id": "42"})
    assert result.ok is True
    assert result.result == {"id": "42", "name": "Widget"}
    assert capture[0]["url"] == "https://api.example.invalid/items/42"
    assert capture[0]["headers"]["Authorization"] == f"Bearer {secret}"
    assert secret not in json.dumps(result.result)


def test_fetch_item_401_is_non_retryable_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(401))
    result = plugin.invoke_tool("fetch_item", {"item_id": "42"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "auth_expired"
    assert result.error.retryable is False


def test_fetch_item_timeout_is_retryable_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, exc=requests.Timeout("slow"))
    result = plugin.invoke_tool("fetch_item", {"item_id": "42"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"
    assert result.error.retryable is True


def test_fetch_item_5xx_is_retryable_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    _install_http(monkeypatch, example, response=FakeResponse(503))
    result = plugin.invoke_tool("fetch_item", {"item_id": "42"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"
    assert result.error.retryable is True


def test_fetch_item_base_url_comes_from_environment(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    monkeypatch.setenv("CUSTOM_MCP_APIKEY_DEMO_BASE_URL", "https://upstream.test/")
    capture: list[dict[str, Any]] = []
    _install_http(
        monkeypatch,
        example,
        response=FakeResponse(200, {"id": "42", "name": "Widget"}),
        capture=capture,
    )
    plugin.invoke_tool("fetch_item", {"item_id": "42"})
    assert capture[0]["url"] == "https://upstream.test/items/42"


def test_unknown_tool_returns_not_found(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, key="k")
    result = plugin.invoke_tool("missing", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "not_found"
