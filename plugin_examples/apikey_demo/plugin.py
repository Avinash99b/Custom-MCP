"""API-key example plugin: credential-gated tools over a small upstream.

The host loads this module as the isolated package ``mcp_plugin_apikey-demo``
and resolves ``ApiKeyDemoPlugin`` from the manifest entry
``plugin:ApiKeyDemoPlugin``. It advertises only ``api_key`` auth.

The plugin reads the core-stored credential from ``self.ctx.secrets`` and never
copies any part of it into a tool result, a log line, or storage. All upstream
HTTP goes through the single module-level :func:`_http_get` helper so a test can
monkeypatch one symbol.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    MCPPlugin,
    PluginContext,
    Status,
    Tool,
    ToolResult,
)

#: Reserved scoped key the core writes before calling ``on_api_key_set``.
CORE_API_KEY = "core.api_key"

#: Scoped storage key used to record what ``on_api_key_set`` observed.
API_KEY_HOOK_KEY = "api_key_hook"

#: Environment variable overriding the upstream base URL.
BASE_URL_ENV = "CUSTOM_MCP_APIKEY_DEMO_BASE_URL"

#: Base URL used when the environment variable is unset.
DEFAULT_BASE_URL = "https://api.example.invalid"

#: Short timeout so a stuck worker thread is bounded.
HTTP_TIMEOUT = 5.0

_NOT_FOUND = "not_found"
_AUTH_EXPIRED = "auth_expired"
_UPSTREAM_UNAVAILABLE = "upstream_unavailable"


def _base_url() -> str:
    """Return the upstream base URL from the environment (no trailing slash)."""
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def _http_get(url: str, headers: dict[str, str], timeout: float) -> requests.Response:
    """Perform one upstream GET.

    Every network call in this plugin funnels through this single helper so a
    test can monkeypatch it without touching ``requests`` itself.
    """
    return requests.get(url, headers=headers, timeout=timeout)


def _whoami_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for ``whoami`` (no shared nested objects)."""
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _fetch_item_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for ``fetch_item``."""
    return {
        "type": "object",
        "properties": {
            "item_id": {
                "type": "string",
                "description": "Identifier of the upstream item to fetch.",
            },
        },
        "required": ["item_id"],
        "additionalProperties": False,
    }


class ApiKeyDemoPlugin(MCPPlugin):
    """A minimal ``api_key`` plugin whose tool set depends on a stored key."""

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self._last_status: Status = Status.NOT_CONNECTED

    # --- required methods ---------------------------------------------------

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.API_KEY]

    def list_tools(self) -> list[Tool]:
        tools = [
            Tool(
                name="whoami",
                description=(
                    "Report the auth mode and credential presence. Never returns "
                    "any part of the credential."
                ),
                input_schema=_whoami_schema(),
            )
        ]
        # ``fetch_item`` only appears once the host has stored a key, so the
        # live tool set changes when the plugin connects.
        if self._api_key() is not None:
            tools.append(
                Tool(
                    name="fetch_item",
                    description="Fetch an item from the upstream using the stored API key.",
                    input_schema=_fetch_item_schema(),
                )
            )
        return tools

    def connect(self) -> ConnectResult:
        if self._api_key() is None:
            self._last_status = Status.NOT_CONNECTED
            return ConnectResult(status=Status.NOT_CONNECTED, message="No API key.")

        status = self._probe()
        self._last_status = status
        if status is Status.CONNECTED:
            return ConnectResult(status=status, message="Connected with the stored API key.")
        if status is Status.AUTH_EXPIRED:
            return ConnectResult(status=status, message="The upstream rejected the credential.")
        return ConnectResult(status=status, message="The upstream is unavailable.")

    def ping(self) -> Status:
        if self._api_key() is None:
            self._last_status = Status.NOT_CONNECTED
            return Status.NOT_CONNECTED

        probe = self._probe()
        if probe is Status.AUTH_EXPIRED:
            self._last_status = Status.AUTH_EXPIRED
            return Status.AUTH_EXPIRED
        if probe is Status.CONNECTED and self._last_status is Status.CONNECTED:
            return Status.CONNECTED
        self._last_status = Status.NOT_CONNECTED
        return Status.NOT_CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "whoami":
            return self._whoami_result()
        if name == "fetch_item":
            return self._fetch_item(args)
        return ToolResult.failure(_NOT_FOUND, f"Unknown tool: {name}")

    # --- optional hooks -----------------------------------------------------

    def on_api_key_set(self, api_key: str) -> None:
        """Record what the core stored before this hook ran.

        The contract guarantees ``core.api_key`` already holds ``api_key``; we
        record whether that held true instead of raising, so a mismatch stays
        observable without failing the connect.
        """
        stored = self.ctx.secrets.get(CORE_API_KEY)
        self.ctx.storage.set(
            API_KEY_HOOK_KEY,
            {"stored": stored is not None, "matched": stored == api_key},
        )

    # --- internal helpers ---------------------------------------------------

    def _api_key(self) -> str | None:
        """Return the stored credential, or ``None`` when absent/empty."""
        key = self.ctx.secrets.get(CORE_API_KEY)
        if isinstance(key, str) and key:
            return key
        return None

    def _headers(self) -> dict[str, str]:
        """Return upstream headers carrying the bearer credential."""
        return {
            "Authorization": f"Bearer {self._api_key() or ''}",
            "Accept": "application/json",
        }

    def _probe(self) -> Status:
        """Call the upstream ``whoami`` and map the outcome to a status."""
        try:
            response = _http_get(
                f"{_base_url()}/whoami", self._headers(), HTTP_TIMEOUT
            )
            code = int(response.status_code)
        except requests.RequestException:
            return Status.ERROR
        if 200 <= code < 300:
            return Status.CONNECTED
        if code in (401, 403):
            return Status.AUTH_EXPIRED
        return Status.ERROR

    def _whoami_result(self) -> ToolResult:
        key = self._api_key()
        return ToolResult.success(
            {
                "mode": AuthMode.API_KEY.value,
                "connected": key is not None and self._last_status is Status.CONNECTED,
                "credential_present": key is not None,
            }
        )

    def _fetch_item(self, args: dict[str, Any]) -> ToolResult:
        item_id = args.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            return ToolResult.failure(
                "invalid_arguments", "The 'item_id' argument must be a non-empty string."
            )
        if self._api_key() is None:
            return ToolResult.failure(_AUTH_EXPIRED, "No API key is stored.")

        try:
            response = _http_get(
                f"{_base_url()}/items/{item_id}", self._headers(), HTTP_TIMEOUT
            )
            code = int(response.status_code)
        except requests.RequestException:
            return ToolResult.failure(
                _UPSTREAM_UNAVAILABLE, "The upstream is unavailable.", retryable=True
            )

        if 200 <= code < 300:
            payload = _decode_payload(response)
            if payload is None:
                return ToolResult.failure(
                    _UPSTREAM_UNAVAILABLE,
                    "The upstream returned an invalid response.",
                    retryable=True,
                )
            return ToolResult.success(
                {"id": payload.get("id", item_id), "name": payload.get("name")}
            )
        if code in (401, 403):
            return ToolResult.failure(
                _AUTH_EXPIRED, "The upstream rejected the credential."
            )
        if code >= 500:
            return ToolResult.failure(
                _UPSTREAM_UNAVAILABLE, "The upstream is unavailable.", retryable=True
            )
        return ToolResult.failure(
            "upstream_error", f"The upstream returned an unexpected status ({code})."
        )


def _decode_payload(response: requests.Response) -> dict[str, Any] | None:
    """Return a JSON object body, or ``None`` when it is missing/malformed."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
