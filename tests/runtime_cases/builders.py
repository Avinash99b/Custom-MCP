"""Reusable, dependency-free builders for Phase 2 runtime fixture packages.

These helpers produce in-memory plugin ZIPs that a future loader/host test can
install and execute.  Everything here uses only the Python standard library; the
SDK is intentionally *not* imported, so importing this module cannot pull in any
production package.  Fixtures use fixed ZIP timestamps so repeated builds are
byte-for-byte identical.

``make_runtime_zip(name, files, ...)`` is the stable builder: it writes a
generated ``manifest.json`` plus the supplied module snippets.  The individual
``*_SOURCE`` constants are the code snippets; ``fixture_cases()`` returns the
full mapping ``name -> ZIP bytes``.

Host-side notes for the executing tests (this module never runs plugin code):

* ``slow_method`` exposes a module-global ``RELEASE_EVENT`` (``threading.Event``)
  in the loaded package; a host test can ``set()`` it to release the blocked
  method early.  Blocking is bounded by ``SLOW_SECONDS`` so it can never hang.
* ``constructor_thread_id`` records ``IMPORT_THREAD_ID`` and the constructor
  thread id into the plugin's scoped storage.
* Defect fixtures (crashers, malformed results, bad tool/schema contracts,
  auth/manifest mismatch, changing tools) intentionally violate the load-time
  contract; they are valid ZIPs with valid manifests and only fail when a loader
  inspects or executes them.
"""

from __future__ import annotations

import io
import json
import textwrap
import zipfile
from collections.abc import Callable, Mapping, Sequence

FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

DEFAULT_ENTRY = "plugin:FixturePlugin"
DEFAULT_VERSION = "1.0.0"
DEFAULT_AUTH_MODES: tuple[str, ...] = ("none",)

_HEADER = """\
from __future__ import annotations

from typing import Any

from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)"""


def _method(signature: str, body: str) -> str:
    """Render an indented method definition from a dedented body block."""
    dedented = textwrap.dedent(body).strip("\n")
    indented = textwrap.indent(dedented, "        ")
    return f"    def {signature}:\n{indented}"


_DEFAULT_METHODS: dict[str, str] = {
    "auth_methods": _method(
        "auth_methods(self) -> list[AuthMode]",
        """
        return [AuthMode.NONE]
        """,
    ),
    "list_tools": _method(
        "list_tools(self) -> list[Tool]",
        """
        return []
        """,
    ),
    "connect": _method(
        "connect(self) -> ConnectResult",
        """
        return ConnectResult(status=Status.CONNECTED)
        """,
    ),
    "ping": _method(
        "ping(self) -> Status",
        """
        return Status.CONNECTED
        """,
    ),
    "invoke_tool": _method(
        "invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult",
        """
        return ToolResult.failure("not_found", f"Unknown tool: {name}")
        """,
    ),
}

_METHOD_ORDER = (
    "__init__",
    "auth_methods",
    "list_tools",
    "connect",
    "ping",
    "invoke_tool",
    "shutdown",
)


def _echo_list_tools() -> str:
    """Shared ``list_tools`` method exposing a single valid ``echo`` tool."""
    return _method(
        "list_tools(self) -> list[Tool]",
        """
        return [
            Tool(
                name="echo",
                description="Echo text back.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "Text to echo back."}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
        ]
        """,
    )


_ECHO_INVOKE_BODY = """
if name == "echo":
    return ToolResult.success({"text": args["text"]})
return ToolResult.failure("not_found", f"Unknown tool: {name}")
"""


def _plugin_source(
    *,
    class_name: str = "FixturePlugin",
    methods: Mapping[str, str] | None = None,
    module_body: str = "",
    imports: str = "",
    docstring: str | None = None,
) -> str:
    """Assemble a complete plugin module from a header plus overrides."""
    resolved = dict(_DEFAULT_METHODS)
    if methods:
        resolved.update(methods)

    chunks: list[str] = [_HEADER]
    if imports:
        chunks.append(textwrap.dedent(imports).strip("\n"))
    if module_body:
        chunks.append(textwrap.dedent(module_body).strip("\n"))

    class_lines = [f"class {class_name}(MCPPlugin):"]
    if docstring is not None:
        class_lines.append(f'    """{docstring}"""')
    for method_name in _METHOD_ORDER:
        if method_name in resolved:
            class_lines.append(resolved[method_name])
    chunks.append("\n".join(class_lines))

    return "\n\n\n".join(chunk.strip("\n") for chunk in chunks) + "\n"


# ---------------------------------------------------------------------------
# Source snippets
# ---------------------------------------------------------------------------

HELLO_CLONE_SOURCE = _plugin_source(
    docstring="Minimal valid fixture plugin exposing an echo tool.",
    methods={
        "list_tools": _echo_list_tools(),
        "invoke_tool": _method(
            "invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult",
            _ECHO_INVOKE_BODY,
        ),
    },
)

HELPERS_SOURCE = '''\
"""Relative-import helper package used by the isolated-loader fixture."""

from __future__ import annotations


def greet(name: str) -> str:
    """Return a greeting for ``name``."""
    return f"Hello, {name}!"
'''

RELATIVE_IMPORT_SOURCE = _plugin_source(
    docstring="Plugin that imports a sibling subpackage with a relative import.",
    imports="from .helpers import greet",
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            return [
                Tool(
                    name="greet",
                    description="Greet a name via a relative import.",
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "Name."}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                ),
            ]
            """,
        ),
        "invoke_tool": _method(
            "invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult",
            """
            if name == "greet":
                return ToolResult.success({"text": greet(args["name"])})
            return ToolResult.failure("not_found", f"Unknown tool: {name}")
            """,
        ),
    },
)

CRASHING_CONSTRUCTOR_SOURCE = _plugin_source(
    docstring="Plugin whose constructor always raises.",
    methods={
        "__init__": _method(
            "__init__(self, ctx: PluginContext) -> None",
            """
            super().__init__(ctx)
            raise RuntimeError("constructor boom")
            """,
        ),
    },
)

CRASHING_AUTH_SOURCE = _plugin_source(
    methods={
        "auth_methods": _method(
            "auth_methods(self) -> list[AuthMode]",
            """
            raise RuntimeError("auth boom")
            """,
        ),
    },
)

CRASHING_LIST_SOURCE = _plugin_source(
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            raise RuntimeError("list boom")
            """,
        ),
    },
)

CRASHING_INVOKE_SOURCE = _plugin_source(
    docstring="Valid echo catalog whose invoke_tool always raises.",
    methods={
        "list_tools": _echo_list_tools(),
        "invoke_tool": _method(
            "invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult",
            """
            raise RuntimeError("invoke boom")
            """,
        ),
    },
)

CRASHING_PING_SOURCE = _plugin_source(
    methods={
        "ping": _method(
            "ping(self) -> Status",
            """
            raise RuntimeError("ping boom")
            """,
        ),
    },
)

SLOW_METHOD_SOURCE = _plugin_source(
    docstring="Plugin whose ping blocks until released or a bounded timeout.",
    module_body="""
    import threading
    import time

    RELEASE_EVENT = threading.Event()
    SLOW_SECONDS = 0.5
    POLL_SECONDS = 0.01


    def _block_until_released() -> None:
        deadline = time.monotonic() + SLOW_SECONDS
        while time.monotonic() < deadline and not RELEASE_EVENT.is_set():
            time.sleep(POLL_SECONDS)
    """,
    methods={
        "ping": _method(
            "ping(self) -> Status",
            """
            _block_until_released()
            return Status.CONNECTED
            """,
        ),
    },
)

MALFORMED_TOOL_RESULT_SOURCE = _plugin_source(
    docstring="Valid echo catalog whose invoke_tool returns a plain dict.",
    methods={
        "list_tools": _echo_list_tools(),
        "invoke_tool": _method(
            "invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult",
            """
            if name == "echo":
                return {"text": args["text"]}
            return ToolResult.failure("not_found", f"Unknown tool: {name}")
            """,
        ),
    },
)

AUTH_MISMATCH_SOURCE = _plugin_source(
    docstring="Plugin whose SDK auth mode disagrees with the manifest.",
    methods={
        "auth_methods": _method(
            "auth_methods(self) -> list[AuthMode]",
            """
            return [AuthMode.API_KEY]
            """,
        ),
    },
)

DUPLICATE_TOOL_NAMES_SOURCE = _plugin_source(
    docstring="Plugin exposing two tools with the same name.",
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            return [
                Tool(
                    name="dup",
                    description="First duplicate.",
                    input_schema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="dup",
                    description="Second duplicate.",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]
            """,
        ),
    },
)

INVALID_TOOL_NAME_SOURCE = _plugin_source(
    docstring="Plugin exposing a tool name outside the allowed pattern.",
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            return [
                Tool(
                    name="bad name!",
                    description="Invalid name.",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]
            """,
        ),
    },
)

SCHEMA_NOT_OBJECT_SOURCE = _plugin_source(
    docstring="Plugin whose tool schema does not declare an object type.",
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            return [
                Tool(name="bad", description="Non-object schema.", input_schema={"type": "string"}),
            ]
            """,
        ),
    },
)

INVALID_SCHEMA_SOURCE = _plugin_source(
    docstring="Plugin whose tool schema fails JSON Schema validation.",
    methods={
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            return [
                Tool(
                    name="bad",
                    description="Invalid schema.",
                    input_schema={
                        "type": "object",
                        "properties": {"x": {"type": "not-a-real-type"}},
                    },
                ),
            ]
            """,
        ),
    },
)

CHANGING_TOOLS_SOURCE = _plugin_source(
    docstring="Plugin that changes its tool set after connect().",
    module_body="""
    TOOL_PHASE = [0]
    """,
    methods={
        "connect": _method(
            "connect(self) -> ConnectResult",
            """
            TOOL_PHASE[0] = 1
            return ConnectResult(status=Status.CONNECTED)
            """,
        ),
        "list_tools": _method(
            "list_tools(self) -> list[Tool]",
            """
            name = "before" if TOOL_PHASE[0] == 0 else "after"
            return [
                Tool(
                    name=name,
                    description="Phase-dependent tool.",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]
            """,
        ),
    },
)

SHUTDOWN_MARKER_SOURCE = _plugin_source(
    docstring="Plugin that records a marker in scoped storage on shutdown.",
    methods={
        "shutdown": _method(
            "shutdown(self) -> None",
            """
            self.ctx.storage.set("shutdown", True)
            """,
        ),
    },
)

CONSTRUCTOR_THREAD_ID_SOURCE = _plugin_source(
    docstring="Plugin that records import and constructor thread ids in storage.",
    module_body="""
    import threading

    IMPORT_THREAD_ID = threading.get_ident()
    """,
    methods={
        "__init__": _method(
            "__init__(self, ctx: PluginContext) -> None",
            """
            super().__init__(ctx)
            self.ctx.storage.set("import_thread_id", IMPORT_THREAD_ID)
            self.ctx.storage.set("constructor_thread_id", threading.get_ident())
            """,
        ),
    },
)


# ---------------------------------------------------------------------------
# Stable builder
# ---------------------------------------------------------------------------


def _as_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def make_runtime_zip(
    name: str,
    files: Mapping[str, bytes | str],
    *,
    auth_modes: Sequence[str] = DEFAULT_AUTH_MODES,
    version: str = DEFAULT_VERSION,
    entry: str = DEFAULT_ENTRY,
    display_name: str | None = None,
    min_core_version: str | None = None,
    extra_manifest: Mapping[str, object] | None = None,
) -> bytes:
    """Build a plugin ZIP with a generated ``manifest.json`` plus ``files``.

    ``files`` maps archive paths (for example ``"plugin.py"`` or
    ``"helpers/__init__.py"``) to ``str``/``bytes`` content.  The manifest always
    contains ``name``, ``version``, ``entry`` and ``auth_modes``; optional fields
    are added when supplied, and ``extra_manifest`` merges additional keys.
    Fixtures are deterministic (fixed timestamps) so repeated builds are equal.
    """
    manifest: dict[str, object] = {
        "name": name,
        "version": version,
        "entry": entry,
        "auth_modes": list(auth_modes),
    }
    if display_name is not None:
        manifest["display_name"] = display_name
    if min_core_version is not None:
        manifest["min_core_version"] = min_core_version
    if extra_manifest is not None:
        manifest.update(extra_manifest)

    entries: dict[str, bytes] = {"manifest.json": json.dumps(manifest).encode("utf-8")}
    for path, content in files.items():
        entries[path] = _as_bytes(content)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, payload in entries.items():
            info = zipfile.ZipInfo(path, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Fixture registry
# ---------------------------------------------------------------------------

_FIXTURE_BUILDERS: dict[str, Callable[[], bytes]] = {
    "hello_clone": lambda: make_runtime_zip("hello", {"plugin.py": HELLO_CLONE_SOURCE}),
    "relative_import_subpackage": lambda: make_runtime_zip(
        "rel-import",
        {"plugin.py": RELATIVE_IMPORT_SOURCE, "helpers/__init__.py": HELPERS_SOURCE},
    ),
    "crashing_constructor": lambda: make_runtime_zip(
        "crash-ctor", {"plugin.py": CRASHING_CONSTRUCTOR_SOURCE}
    ),
    "crashing_auth": lambda: make_runtime_zip(
        "crash-auth", {"plugin.py": CRASHING_AUTH_SOURCE}
    ),
    "crashing_list": lambda: make_runtime_zip(
        "crash-list", {"plugin.py": CRASHING_LIST_SOURCE}
    ),
    "crashing_invoke": lambda: make_runtime_zip(
        "crash-invoke", {"plugin.py": CRASHING_INVOKE_SOURCE}
    ),
    "crashing_ping": lambda: make_runtime_zip(
        "crash-ping", {"plugin.py": CRASHING_PING_SOURCE}
    ),
    "slow_method": lambda: make_runtime_zip(
        "slow-plugin", {"plugin.py": SLOW_METHOD_SOURCE}
    ),
    "malformed_tool_result": lambda: make_runtime_zip(
        "bad-result", {"plugin.py": MALFORMED_TOOL_RESULT_SOURCE}
    ),
    "auth_manifest_mismatch": lambda: make_runtime_zip(
        "auth-mismatch", {"plugin.py": AUTH_MISMATCH_SOURCE}, auth_modes=("none",)
    ),
    "duplicate_tool_names": lambda: make_runtime_zip(
        "dup-tools", {"plugin.py": DUPLICATE_TOOL_NAMES_SOURCE}
    ),
    "invalid_tool_name": lambda: make_runtime_zip(
        "bad-toolname", {"plugin.py": INVALID_TOOL_NAME_SOURCE}
    ),
    "schema_not_object": lambda: make_runtime_zip(
        "bad-schema-type", {"plugin.py": SCHEMA_NOT_OBJECT_SOURCE}
    ),
    "invalid_schema": lambda: make_runtime_zip(
        "invalid-schema", {"plugin.py": INVALID_SCHEMA_SOURCE}
    ),
    "changing_tools_after_connect": lambda: make_runtime_zip(
        "changing-tools", {"plugin.py": CHANGING_TOOLS_SOURCE}
    ),
    "shutdown_marker": lambda: make_runtime_zip(
        "shutdown-mark", {"plugin.py": SHUTDOWN_MARKER_SOURCE}
    ),
    "constructor_thread_id": lambda: make_runtime_zip(
        "thread-ids", {"plugin.py": CONSTRUCTOR_THREAD_ID_SOURCE}
    ),
}


def fixture_names() -> list[str]:
    """Return every fixture name in registration order."""
    return list(_FIXTURE_BUILDERS)


def fixture_cases() -> dict[str, bytes]:
    """Return the full fixture mapping ``name -> ZIP bytes`` (fresh, deterministic)."""
    return {name: build() for name, build in _FIXTURE_BUILDERS.items()}


__all__ = [
    "AUTH_MISMATCH_SOURCE",
    "CHANGING_TOOLS_SOURCE",
    "CONSTRUCTOR_THREAD_ID_SOURCE",
    "CRASHING_AUTH_SOURCE",
    "CRASHING_CONSTRUCTOR_SOURCE",
    "CRASHING_INVOKE_SOURCE",
    "CRASHING_LIST_SOURCE",
    "CRASHING_PING_SOURCE",
    "DEFAULT_AUTH_MODES",
    "DEFAULT_ENTRY",
    "DEFAULT_VERSION",
    "DUPLICATE_TOOL_NAMES_SOURCE",
    "FIXED_DATE_TIME",
    "HELLO_CLONE_SOURCE",
    "HELPERS_SOURCE",
    "INVALID_SCHEMA_SOURCE",
    "INVALID_TOOL_NAME_SOURCE",
    "MALFORMED_TOOL_RESULT_SOURCE",
    "RELATIVE_IMPORT_SOURCE",
    "SCHEMA_NOT_OBJECT_SOURCE",
    "SHUTDOWN_MARKER_SOURCE",
    "SLOW_METHOD_SOURCE",
    "fixture_cases",
    "fixture_names",
    "make_runtime_zip",
]
