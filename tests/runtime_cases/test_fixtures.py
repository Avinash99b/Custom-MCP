"""Structural fixture tests plus direct execution of trusted fixture packages.

Fixture ZIPs are built, inspected structurally, and then executed directly in an
isolated, test-owned temp package (never the production loader). Only our own
trusted fixture sources are imported; no untrusted code runs. The hello example
is driven the same way. No network, Redis, subprocess or live server is used.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
import re
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_plugin_sdk import AuthMode, MCPPlugin, PluginContext, Status, ToolResult
from tests.runtime_cases.builders import fixture_cases, fixture_names, make_runtime_zip

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "plugin_examples" / "hello"
HELLO_MANIFEST = HELLO_DIR / "manifest.json"
HELLO_PLUGIN = HELLO_DIR / "plugin.py"

FIXTURE_CASES = fixture_cases()

# name -> (slug, entry, auth_modes)
FIXTURE_EXPECTATIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "hello_clone": ("hello", "plugin:FixturePlugin", ("none",)),
    "relative_import_subpackage": ("rel-import", "plugin:FixturePlugin", ("none",)),
    "crashing_constructor": ("crash-ctor", "plugin:FixturePlugin", ("none",)),
    "crashing_auth": ("crash-auth", "plugin:FixturePlugin", ("none",)),
    "crashing_list": ("crash-list", "plugin:FixturePlugin", ("none",)),
    "crashing_invoke": ("crash-invoke", "plugin:FixturePlugin", ("none",)),
    "crashing_ping": ("crash-ping", "plugin:FixturePlugin", ("none",)),
    "slow_method": ("slow-plugin", "plugin:FixturePlugin", ("none",)),
    "malformed_tool_result": ("bad-result", "plugin:FixturePlugin", ("none",)),
    "auth_manifest_mismatch": ("auth-mismatch", "plugin:FixturePlugin", ("none",)),
    "duplicate_tool_names": ("dup-tools", "plugin:FixturePlugin", ("none",)),
    "invalid_tool_name": ("bad-toolname", "plugin:FixturePlugin", ("none",)),
    "schema_not_object": ("bad-schema-type", "plugin:FixturePlugin", ("none",)),
    "invalid_schema": ("invalid-schema", "plugin:FixturePlugin", ("none",)),
    "changing_tools_after_connect": ("changing-tools", "plugin:FixturePlugin", ("none",)),
    "shutdown_marker": ("shutdown-mark", "plugin:FixturePlugin", ("none",)),
    "constructor_thread_id": ("thread-ids", "plugin:FixturePlugin", ("none",)),
}


def _read_member(data: bytes, path: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(path).decode("utf-8")


def _read_manifest(data: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parsed = json.loads(archive.read("manifest.json"))
    assert isinstance(parsed, dict)
    return parsed


def _zip_hello_example() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (HELLO_MANIFEST, HELLO_PLUGIN):
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


class MemoryStorage:
    """Minimal in-memory implementation of the SDK ``Storage`` protocol."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._values[key] = value

    def delete(self, key: str) -> bool:
        if key in self._values:
            del self._values[key]
            return True
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self._values if key.startswith(prefix))


def _make_ctx(slug: str = "hello") -> tuple[PluginContext, MemoryStorage, list[tuple[str, str]]]:
    storage = MemoryStorage()
    logs: list[tuple[str, str]] = []
    ctx = PluginContext(
        slug=slug,
        storage=storage,
        secrets=MemoryStorage(),
        log=lambda level, message: logs.append((level, message)),
        callback_url=f"https://mcp.example.com/oauth/{slug}/callback",
    )
    return ctx, storage, logs


@pytest.fixture
def load_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], Any]]:
    """Import a trusted fixture package from an isolated, test-owned temp dir."""
    loaded: list[str] = []

    def _load(name: str) -> Any:
        package = "runtime_fixture_" + re.sub(r"\W", "_", name)
        root = tmp_path / package
        root.mkdir()
        with zipfile.ZipFile(io.BytesIO(FIXTURE_CASES[name])) as archive:
            archive.extractall(root)
        (root / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        module = importlib.import_module(f"{package}.plugin")
        loaded.append(package)
        return module

    yield _load

    for package in loaded:
        for key in [k for k in sys.modules if k == package or k.startswith(f"{package}.")]:
            sys.modules.pop(key, None)


@pytest.fixture(scope="module")
def hello_module() -> Iterator[Any]:
    spec = importlib.util.spec_from_file_location("hello_plugin_under_test", HELLO_PLUGIN)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# Fixture package structure (no execution)
# ---------------------------------------------------------------------------


def test_fixture_registry_matches_expectations() -> None:
    assert len(FIXTURE_CASES) == 17
    assert list(FIXTURE_CASES) == fixture_names()
    assert set(FIXTURE_CASES) == set(FIXTURE_EXPECTATIONS)


def test_fixture_cases_are_deterministic() -> None:
    assert fixture_cases() == FIXTURE_CASES


@pytest.mark.parametrize("name", sorted(FIXTURE_CASES))
def test_fixture_package_structure(name: str) -> None:
    data = FIXTURE_CASES[name]
    assert data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = set(archive.namelist())
        assert "manifest.json" in members
        assert archive.testzip() is None

    slug, entry, auth_modes = FIXTURE_EXPECTATIONS[name]
    manifest = _read_manifest(data)
    assert manifest["name"] == slug
    assert manifest["version"] == "1.0.0"
    assert manifest["entry"] == entry
    assert tuple(manifest["auth_modes"]) == auth_modes

    module = entry.split(":", 1)[0]
    assert f"{module}.py" in members


@pytest.mark.parametrize("name", sorted(FIXTURE_CASES))
def test_fixture_python_sources_are_syntactically_valid(name: str) -> None:
    with zipfile.ZipFile(io.BytesIO(FIXTURE_CASES[name])) as archive:
        python_members = [m for m in archive.namelist() if m.endswith(".py")]
        assert python_members
        for member in python_members:
            ast.parse(archive.read(member).decode("utf-8"), filename=member)


def test_make_runtime_zip_uses_supplied_manifest_fields() -> None:
    data = make_runtime_zip(
        "custom",
        {"plugin.py": "VALUE = 1\n"},
        auth_modes=("none",),
        version="2.3.4",
        entry="plugin:CustomPlugin",
        display_name="Custom",
        min_core_version="0.1.0",
    )
    manifest = _read_manifest(data)
    assert manifest["name"] == "custom"
    assert manifest["version"] == "2.3.4"
    assert manifest["entry"] == "plugin:CustomPlugin"
    assert manifest["display_name"] == "Custom"
    assert manifest["min_core_version"] == "0.1.0"
    assert _read_member(data, "plugin.py") == "VALUE = 1\n"


# ---------------------------------------------------------------------------
# Every fixture imports and exposes a base-class entry (in isolated temp dirs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURE_CASES))
def test_every_fixture_imports_with_entry_class(
    load_fixture: Callable[[str], Any], name: str
) -> None:
    module = load_fixture(name)
    assert hasattr(module, "FixturePlugin")
    assert issubclass(module.FixturePlugin, MCPPlugin)


# ---------------------------------------------------------------------------
# Behavioral defect tests (direct execution of trusted fixtures)
# ---------------------------------------------------------------------------


def test_crashing_constructor_raises(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("crashing_constructor")
    with pytest.raises(RuntimeError, match="constructor boom"):
        module.FixturePlugin(_make_ctx("crash-ctor")[0])


def test_crashing_auth_raises(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("crashing_auth")
    plugin = module.FixturePlugin(_make_ctx("crash-auth")[0])
    with pytest.raises(RuntimeError, match="auth boom"):
        plugin.auth_methods()


def test_crashing_list_tools_raises(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("crashing_list")
    plugin = module.FixturePlugin(_make_ctx("crash-list")[0])
    with pytest.raises(RuntimeError, match="list boom"):
        plugin.list_tools()


def test_crashing_ping_raises(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("crashing_ping")
    plugin = module.FixturePlugin(_make_ctx("crash-ping")[0])
    with pytest.raises(RuntimeError, match="ping boom"):
        plugin.ping()


def test_crashing_invoke_exposes_echo_then_raises(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("crashing_invoke")
    plugin = module.FixturePlugin(_make_ctx("crash-invoke")[0])
    assert [tool.name for tool in plugin.list_tools()] == ["echo"]
    with pytest.raises(RuntimeError, match="invoke boom"):
        plugin.invoke_tool("echo", {"text": "hi"})


def test_malformed_tool_result_exposes_echo_then_returns_dict(
    load_fixture: Callable[[str], Any],
) -> None:
    module = load_fixture("malformed_tool_result")
    plugin = module.FixturePlugin(_make_ctx("bad-result")[0])
    assert [tool.name for tool in plugin.list_tools()] == ["echo"]
    result = plugin.invoke_tool("echo", {"text": "hi"})
    assert not isinstance(result, ToolResult)
    assert result == {"text": "hi"}


def test_auth_manifest_mismatch_disagrees(load_fixture: Callable[[str], Any]) -> None:
    manifest = _read_manifest(FIXTURE_CASES["auth_manifest_mismatch"])
    module = load_fixture("auth_manifest_mismatch")
    plugin = module.FixturePlugin(_make_ctx("auth-mismatch")[0])
    assert manifest["auth_modes"] == ["none"]
    assert plugin.auth_methods() == [AuthMode.API_KEY]


def test_duplicate_tool_names_are_returned(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("duplicate_tool_names")
    plugin = module.FixturePlugin(_make_ctx("dup-tools")[0])
    names = [tool.name for tool in plugin.list_tools()]
    assert names == ["dup", "dup"]
    assert len(set(names)) < len(names)


def test_invalid_tool_name_violates_pattern(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("invalid_tool_name")
    plugin = module.FixturePlugin(_make_ctx("bad-toolname")[0])
    names = [tool.name for tool in plugin.list_tools()]
    assert names == ["bad name!"]
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", names[0]) is None


def test_schema_not_object_is_rejected(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("schema_not_object")
    plugin = module.FixturePlugin(_make_ctx("bad-schema-type")[0])
    schema = plugin.list_tools()[0].input_schema
    assert schema.get("type") != "object"
    assert schema["type"] == "string"


def test_invalid_schema_fails_draft2020_check(load_fixture: Callable[[str], Any]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    module = load_fixture("invalid_schema")
    plugin = module.FixturePlugin(_make_ctx("invalid-schema")[0])
    schema = plugin.list_tools()[0].input_schema
    with pytest.raises(jsonschema.exceptions.SchemaError):
        jsonschema.Draft202012Validator.check_schema(schema)
    # Sanity: an object schema still passes the same checker.
    jsonschema.Draft202012Validator.check_schema({"type": "object", "properties": {}})


def test_changing_tools_after_connect(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("changing_tools_after_connect")
    plugin = module.FixturePlugin(_make_ctx("changing-tools")[0])
    assert [tool.name for tool in plugin.list_tools()] == ["before"]
    assert plugin.connect().status is Status.CONNECTED
    assert [tool.name for tool in plugin.list_tools()] == ["after"]


def test_shutdown_marker_writes_storage(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("shutdown_marker")
    ctx, storage, _ = _make_ctx("shutdown-mark")
    plugin = module.FixturePlugin(ctx)
    plugin.shutdown()
    assert storage.get("shutdown") is True


def test_constructor_thread_ids_are_recorded(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("constructor_thread_id")
    ctx, storage, _ = _make_ctx("thread-ids")
    module.FixturePlugin(ctx)
    assert storage.get("constructor_thread_id") == threading.get_ident()
    assert storage.get("import_thread_id") == module.IMPORT_THREAD_ID
    assert isinstance(storage.get("import_thread_id"), int)


def test_relative_import_subpackage_resolves(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("relative_import_subpackage")
    plugin = module.FixturePlugin(_make_ctx("rel-import")[0])
    assert [tool.name for tool in plugin.list_tools()] == ["greet"]
    result = plugin.invoke_tool("greet", {"name": "Ada"})
    assert result.ok is True
    assert result.result == {"text": "Hello, Ada!"}


def test_slow_method_release_event_path(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("slow_method")
    plugin = module.FixturePlugin(_make_ctx("slow-plugin")[0])
    assert module.SLOW_SECONDS == 0.5
    module.RELEASE_EVENT.set()
    started = time.monotonic()
    assert plugin.ping() is Status.CONNECTED
    assert time.monotonic() - started < module.SLOW_SECONDS


def test_hello_clone_echo_round_trip(load_fixture: Callable[[str], Any]) -> None:
    module = load_fixture("hello_clone")
    plugin = module.FixturePlugin(_make_ctx("hello")[0])
    assert [tool.name for tool in plugin.list_tools()] == ["echo"]
    result = plugin.invoke_tool("echo", {"text": "hi"})
    assert result.ok is True
    assert result.result == {"text": "hi"}


# ---------------------------------------------------------------------------
# Hello example: direct execution with an in-memory scoped storage
# ---------------------------------------------------------------------------


def test_hello_manifest_parses_with_production_validator() -> None:
    from custom_mcp.plugins.manifest import parse_manifest

    manifest = parse_manifest(HELLO_MANIFEST.read_bytes())
    assert manifest.name == "hello"
    assert manifest.display_name == "Hello"
    assert manifest.version == "1.0.0"
    assert manifest.entry == "plugin:HelloPlugin"
    assert [mode.value for mode in manifest.auth_modes] == ["none"]
    assert manifest.min_core_version == "0.1.0"


def test_hello_example_passes_phase1_package_validation() -> None:
    from custom_mcp.plugins.packaging import validate_package

    result = validate_package(_zip_hello_example(), max_size_bytes=20 * 1024 * 1024)
    assert result.manifest.name == "hello"
    assert result.warnings == ()
    files = {entry.path for entry in result.entries if not entry.is_dir}
    assert {"manifest.json", "plugin.py"} <= files


def test_hello_basic_lifecycle(hello_module: Any) -> None:
    ctx, _, _ = _make_ctx()
    plugin = hello_module.HelloPlugin(ctx)
    assert isinstance(plugin, MCPPlugin)
    assert plugin.ctx is ctx
    assert plugin.auth_methods() == [AuthMode.NONE]
    assert plugin.connect().status is Status.CONNECTED
    assert plugin.ping() is Status.CONNECTED


def test_hello_tool_schemas_are_exact_and_valid(hello_module: Any) -> None:
    plugin = hello_module.HelloPlugin(_make_ctx()[0])
    tools = plugin.list_tools()
    assert [tool.name for tool in tools] == ["echo", "add"]

    echo = next(tool for tool in tools if tool.name == "echo")
    add = next(tool for tool in tools if tool.name == "add")

    assert echo.input_schema == {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo back."}},
        "required": ["text"],
        "additionalProperties": False,
    }
    assert add.input_schema["type"] == "object"
    assert add.input_schema["required"] == ["a", "b"]
    assert add.input_schema["additionalProperties"] is False
    assert set(add.input_schema["properties"]) == {"a", "b"}
    assert all(prop["type"] == "number" for prop in add.input_schema["properties"].values())

    jsonschema = pytest.importorskip("jsonschema")
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)


def test_list_tools_returns_independent_schemas(hello_module: Any) -> None:
    plugin = hello_module.HelloPlugin(_make_ctx()[0])
    first = plugin.list_tools()
    first[0].input_schema["properties"]["text"]["type"] = "integer"
    second = plugin.list_tools()
    assert second[0].input_schema["properties"]["text"]["type"] == "string"


def test_hello_echo_add_results_and_counter(hello_module: Any) -> None:
    ctx, storage, _ = _make_ctx()
    plugin = hello_module.HelloPlugin(ctx)
    assert storage.get("call_count", 0) == 0

    echo = plugin.invoke_tool("echo", {"text": "hi"})
    assert echo.ok is True
    assert echo.result == {"text": "hi"}
    assert echo.error is None
    assert storage.get("call_count") == 1

    add = plugin.invoke_tool("add", {"a": 2, "b": 40})
    assert add.ok is True
    assert add.result == {"sum": 42}
    assert storage.get("call_count") == 2


def test_unknown_tool_returns_not_found_without_counting(hello_module: Any) -> None:
    ctx, storage, _ = _make_ctx()
    plugin = hello_module.HelloPlugin(ctx)
    failed = plugin.invoke_tool("missing", {})
    assert failed.ok is False
    assert failed.error is not None
    assert failed.error.code == "not_found"
    assert storage.get("call_count", 0) == 0


def test_counter_is_thread_safe(hello_module: Any) -> None:
    ctx, storage, _ = _make_ctx()
    plugin = hello_module.HelloPlugin(ctx)
    threads = [
        threading.Thread(target=plugin.invoke_tool, args=("echo", {"text": "x"}))
        for _ in range(50)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert storage.get("call_count") == 50


def test_hello_default_hooks_are_inherited(hello_module: Any) -> None:
    plugin = hello_module.HelloPlugin(_make_ctx()[0])
    assert plugin.refresh() is Status.AUTH_EXPIRED
    assert plugin.shutdown() is None
    assert plugin.on_restore() is None
    assert plugin.on_disconnect() is None
    assert plugin.on_api_key_set("key") is None
    assert plugin.on_oauth_credentials_set("client-id", None) is None
    with pytest.raises(NotImplementedError):
        plugin.get_oauth_sign_in_url("state")
    with pytest.raises(NotImplementedError):
        plugin.on_oauth_result("code", "state")


def test_memory_storage_supports_sdk_protocol() -> None:
    storage = MemoryStorage()
    assert storage.get("missing", 7) == 7
    storage.set("count", 1)
    storage.set("count", 2)
    assert storage.get("count") == 2
    storage.set("temp", "v", ttl=60)
    assert storage.list_keys() == ["count", "temp"]
    assert storage.list_keys("te") == ["temp"]
    assert storage.delete("temp") is True
    assert storage.delete("temp") is False
    # A stored None must still be deletable (key presence, not value truthiness).
    storage.set("explicit-none", None)
    assert storage.delete("explicit-none") is True
    assert storage.delete("explicit-none") is False
