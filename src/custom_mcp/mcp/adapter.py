"""FastMCP adapter for one immutable plugin runtime (Phase 3, adapter slice).

:func:`build_plugin_app` returns a FastMCP streamable-HTTP ASGI app whose tools
mirror ``runtime.tools`` exactly. The entry point is intentionally frozen so the
later router slice can mount it without touching plugin code.

FastMCP 2.14.7 contract used here (verified against the installed package)
----------------------------------------------------------------------------
* ``FastMCP.http_app(path="/", stateless_http=True, json_response=True)``
  returns a ``fastmcp.server.http.StarletteWithLifespan`` whose
  ``lifespan`` is ``router.lifespan_context``.
* ``FastMCP.add_tool(tool)`` accepts a ``fastmcp.tools.tool.Tool``. ``Tool`` is
  a pydantic model carrying the raw ``parameters`` JSON schema; a subclass
  implements ``async def run(self, arguments: dict[str, Any]) -> ToolResult``.
* ``ToolManager.call_tool`` awaits ``tool.run(arguments)`` and expects a
  ``fastmcp.tools.tool.ToolResult`` (imported from ``fastmcp.tools.tool``, not
  ``fastmcp.tools``).
* ``ToolResult(content=..., structured_content=...)`` (the only public way to
  emit both) makes ``to_mcp_result()`` return a
  ``(content, structured_content)`` tuple; the low-level server then sends a
  ``CallToolResult`` with ``isError=False``, the JSON text block in ``content``
  and the dictionary in ``structuredContent``.
* There is no ``ToolResult`` field for ``isError``. Failures are raised as
  ``fastmcp.exceptions.ToolError`` -- the documented "escape hatch" that
  ``ToolManager.call_tool`` re-raises unchanged (it is a ``FastMCPError``) and
  the low-level server converts into ``CallToolResult(isError=True,
  content=[TextContent(text=str(exc))])``. Raising therefore preserves the
  exact safe message on the wire.

Uniform argument-error form
---------------------------
Every tool validates ``arguments`` with :class:`jsonschema.Draft202012Validator`
against the raw schema before execution. The first JSON-Schema violation (ordered
by JSON path then keyword for determinism) raises::

    [invalid_arguments] Invalid arguments for tool '<name>':
    <json_path> failed the '<keyword>' check.

Only the JSON path and keyword are reported, never the offending value, so tool
arguments are not echoed back. Every failure path uses the same
``[<code>] <message>[ (retryable)]`` shape.

Credential safety
-----------------
Only host-sanitized messages (``PluginCallError.safe_message``) and SDK
``ToolError`` code/message values reach the client. Plugin exception text, stored
secrets and storage values are never included; unexpected internal failures map
to a generic ``[internal_error]`` message. When a redactor is supplied, each run
first captures the plugin's current secret strings and every plugin-derived
failure text is redacted against that captured set before it is raised, so a
concurrent disconnect (which wipes the secrets) cannot leave an in-flight
failure unredacted. A capture or redaction failure fails closed to a generic
message (a plugin error body may otherwise echo a stored credential).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.tools.tool import Tool as FastMCPTool
from fastmcp.tools.tool import ToolResult
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import PrivateAttr

from custom_mcp.plugins.host import PluginCallError, PluginHost
from custom_mcp.plugins.runtime import PluginRuntime
from mcp_plugin_sdk import Status, Tool
from mcp_plugin_sdk import ToolResult as PluginToolResult

RETRYABLE_SUFFIX = " (retryable)"
_INVALID_ARGUMENTS = "invalid_arguments"
_INTERNAL_ERROR = "internal_error"
# Host failures that are transient and therefore worth retrying.
_RETRYABLE_HOST_KINDS = frozenset({"timeout", "plugin_overloaded"})

#: Captures the slug's current secret strings for one tool run.
SecretsProvider = Callable[[], Awaitable[tuple[str, ...]]]
#: Redacts one rendered failure text against a captured secret set (pure).
Redact = Callable[[str, Sequence[str]], str]


def _failure_text(code: str, message: str, retryable: bool = False) -> str:
    """Render the uniform ``[code] message[ (retryable)]`` failure text."""
    text = f"[{code}] {message}"
    if retryable:
        text += RETRYABLE_SUFFIX
    return text


def _argument_error_message(tool_name: str, error: ValidationError) -> str:
    """Build the uniform argument-error message without echoing argument values."""
    location = error.json_path or "$"
    keyword = error.validator or "schema"
    return (
        f"Invalid arguments for tool '{tool_name}': "
        f"{location} failed the '{keyword}' check."
    )


def _first_argument_error(
    validator: Draft202012Validator, arguments: Any
) -> ValidationError | None:
    """Return the first schema violation, ordered by path then keyword."""
    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda exc: (list(exc.absolute_path), exc.validator or ""),
    )
    return errors[0] if errors else None


class _PluginTool(FastMCPTool):
    """A FastMCP tool backed by one cached SDK tool of a plugin runtime.

    The raw ``parameters`` schema is stored verbatim (no signature
    introspection, no normalization) and advertised unchanged. Execution is
    delegated to the host so it stays off the event loop and bounded by the
    per-plugin semaphore.
    """

    _host: PluginHost = PrivateAttr()
    _runtime: PluginRuntime = PrivateAttr()
    _validator: Draft202012Validator = PrivateAttr()
    _secrets: SecretsProvider | None = PrivateAttr(default=None)
    _redact: Redact | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        tool: Tool,
        host: PluginHost,
        runtime: PluginRuntime,
        secrets: SecretsProvider | None = None,
        redact: Redact | None = None,
    ) -> None:
        super().__init__(
            name=tool.name,
            description=tool.description,
            parameters=tool.input_schema,
        )
        self._host = host
        self._runtime = runtime
        self._validator = Draft202012Validator(tool.input_schema)
        self._secrets = secrets
        self._redact = redact

    async def _capture_secrets(self) -> tuple[str, ...] | None:
        """Capture the slug's current secrets; ``None`` means capture failed.

        Called once at the start of a run so a concurrent disconnect cannot wipe
        the secrets before the failure text is redacted.
        """
        if self._secrets is None:
            return None
        try:
            return await self._secrets()
        except Exception:
            return None

    def _redact_failure(
        self,
        text: str,
        *,
        captured: tuple[str, ...] | None,
        fallback: str,
    ) -> str:
        """Redact plugin-derived failure text, failing closed on any error.

        A plugin-supplied error message may echo a stored credential (for example
        an upstream rejection body that includes the plugin's own API key), so it
        must never reach the wire unredacted. A missing captured set or a
        redaction failure degrades to the supplied generic ``fallback``. With no
        redactor configured the text is returned unchanged (adapter unit tests).
        """
        if self._redact is None:
            return text
        if captured is None:
            return fallback
        try:
            return self._redact(text, captured)
        except Exception:
            return fallback

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        captured = (
            await self._capture_secrets() if self._redact is not None else None
        )
        error = _first_argument_error(self._validator, arguments)
        if error is not None:
            raise ToolError(
                _failure_text(
                    _INVALID_ARGUMENTS, _argument_error_message(self.name, error)
                )
            )

        if self._runtime.status is Status.NOT_CONNECTED:
            raise ToolError(
                _failure_text(
                    "not_connected",
                    f"Plugin '{self._runtime.slug}' is not connected. "
                    "Reconnect it in the dashboard.",
                )
            )

        try:
            result = await self._host.call(
                self._runtime, "invoke_tool", self.name, arguments
            )
        except PluginCallError as exc:
            # ``safe_message`` is already host-sanitized (no plugin text), but
            # redact the whole rendered failure defensively against a rotated
            # credential.
            raise ToolError(
                self._redact_failure(
                    _failure_text(
                        exc.kind,
                        exc.safe_message,
                        exc.kind in _RETRYABLE_HOST_KINDS,
                    ),
                    captured=captured,
                    fallback=_failure_text(
                        _INTERNAL_ERROR, "Plugin tool execution failed."
                    ),
                )
            ) from None
        except Exception:
            raise ToolError(
                _failure_text(_INTERNAL_ERROR, "Plugin tool execution failed.")
            ) from None

        if not isinstance(result, PluginToolResult):
            raise ToolError(
                _failure_text(_INTERNAL_ERROR, "Plugin tool execution failed.")
            )

        if result.ok:
            payload = result.result
            if not isinstance(payload, dict):
                raise ToolError(
                    _failure_text(_INTERNAL_ERROR, "Plugin tool execution failed.")
                )
            return ToolResult(
                content=json.dumps(payload),
                structured_content=payload,
            )

        failure = result.error
        if failure is None:
            raise ToolError(
                _failure_text(_INTERNAL_ERROR, "Plugin tool execution failed.")
            )
        # Redact the whole rendered failure (code **and** message): a plugin may
        # map a real upstream error's code or message through the SDK, either of
        # which can echo a stored credential.
        raise ToolError(
            self._redact_failure(
                _failure_text(failure.code, failure.message, failure.retryable),
                captured=captured,
                fallback=_failure_text(
                    _INTERNAL_ERROR, "Plugin tool execution failed."
                ),
            )
        )


def build_plugin_app(
    runtime: PluginRuntime,
    host: PluginHost,
    *,
    secrets: SecretsProvider | None = None,
    redact: Redact | None = None,
) -> StarletteWithLifespan:
    """Build the streamable-HTTP FastMCP app for one immutable plugin runtime.

    One tool is registered per cached SDK :class:`~mcp_plugin_sdk.Tool`, with
    ``parameters`` set to the tool's ``input_schema`` unchanged. Tools are listed
    regardless of connection status; calls are validated and routed through
    ``host``. When ``secrets`` and ``redact`` are supplied, each run captures the
    slug's current secrets up front and every plugin-derived failure message is
    redacted against that captured set before it is raised, so a concurrent
    disconnect cannot leave an in-flight failure unredacted.
    """
    server: FastMCP[Any] = FastMCP(runtime.slug)
    for tool in runtime.tools:
        server.add_tool(
            _PluginTool(
                tool=tool,
                host=host,
                runtime=runtime,
                secrets=secrets,
                redact=redact,
            )
        )
    return server.http_app(path="/", stateless_http=True, json_response=True)


__all__ = ["build_plugin_app"]
