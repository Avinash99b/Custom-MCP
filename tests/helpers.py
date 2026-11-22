"""Shared test helpers."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from starlette.requests import Request

from tests.runtime_cases.builders import make_runtime_zip

TEST_SECRET = "correct-horse-battery-staple"
TEST_BASE_URL = "https://mcp.example.com"

HELLO_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "plugin_examples" / "hello"

PLUGIN_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.NONE]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown_tool", "unknown tool")
'''


def build_plugin_zip(
    *,
    slug: str = "demo",
    version: str = "1.0.0",
    entry: str = "plugin:Plugin",
    auth_modes: tuple[str, ...] = ("none",),
    display_name: str | None = None,
    description: str = "",
    nested: bool = False,
    manifest_extra: Mapping[str, Any] | None = None,
    extra_files: Mapping[str, bytes | str] | None = None,
    plugin_source: str = PLUGIN_SOURCE,
) -> bytes:
    """Build a small valid plugin ZIP without importing validator test cases."""
    manifest: dict[str, Any] = {
        "name": slug,
        "version": version,
        "entry": entry,
        "auth_modes": list(auth_modes),
        "description": description,
    }
    if display_name is not None:
        manifest["display_name"] = display_name
    if manifest_extra:
        manifest.update(manifest_extra)

    prefix = f"{slug}/" if nested else ""
    files: dict[str, bytes | str] = {
        "manifest.json": json.dumps(manifest),
        "plugin.py": plugin_source,
    }
    if extra_files:
        files.update(extra_files)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(f"{prefix}{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def build_hello_zip() -> bytes:
    """Build the ``hello`` example package ZIP for lifecycle tests."""
    plugin_source = (HELLO_EXAMPLE_DIR / "plugin.py").read_text(encoding="utf-8")
    manifest_bytes = (HELLO_EXAMPLE_DIR / "manifest.json").read_bytes()
    return make_runtime_zip(
        "hello",
        {"plugin.py": plugin_source, "manifest.json": manifest_bytes},
        entry="plugin:HelloPlugin",
    )


class JsonLogCapture:
    """Reads JSON log lines captured from a ``StreamHandler``."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def payloads(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in self._stream.getvalue().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def with_message(self, message: str) -> list[dict[str, Any]]:
        return [record for record in self.payloads() if record.get("message") == message]


def build_env(**overrides: str | None) -> dict[str, str]:
    """Build an environment mapping for :meth:`Settings.from_env`.

    A value of ``None`` removes the key, which lets tests exercise missing
    required variables.
    """
    env: dict[str, str] = {"MCP_SECRET": TEST_SECRET, "BASE_URL": TEST_BASE_URL}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def make_request(
    *,
    method: str = "GET",
    path: str = "/api/session",
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
) -> Request:
    """Build a Starlette request with the given headers and cookies."""
    raw: list[tuple[bytes, bytes]] = [
        (name.lower().encode("latin-1"), value.encode("utf-8"))
        for name, value in (headers or {}).items()
    ]
    if cookies:
        jar = "; ".join(f"{name}={value}" for name, value in cookies.items())
        raw.append((b"cookie", jar.encode("utf-8")))
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "server": ("mcp.example.com", 443),
        "client": ("127.0.0.1", 12345),
        "state": {},
    }
    return Request(scope)


__all__ = [
    "HELLO_EXAMPLE_DIR",
    "PLUGIN_SOURCE",
    "TEST_BASE_URL",
    "TEST_SECRET",
    "JsonLogCapture",
    "build_env",
    "build_hello_zip",
    "build_plugin_zip",
    "make_request",
]
