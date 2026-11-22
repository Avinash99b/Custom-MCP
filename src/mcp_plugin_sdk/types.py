"""Enums and value objects exported by the plugin SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AuthMode(str, Enum):  # noqa: UP042 - spec mandates str, Enum
    """Authentication modes a plugin may advertise."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"


class Status(str, Enum):  # noqa: UP042 - spec mandates str, Enum
    """Connection status of a plugin instance."""

    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"
    OAUTH_REQUIRED = "oauth_required"
    AUTH_EXPIRED = "auth_expired"
    ERROR = "error"


@dataclass(frozen=True)
class Tool:
    """A plugin tool definition.

    ``input_schema`` is a JSON Schema object. The host validates it exactly as
    supplied; it must explicitly declare ``type: object``.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass
class ToolError:
    """A structured tool failure."""

    code: str
    message: str
    retryable: bool = False


@dataclass
class ToolResult:
    """The outcome of a tool invocation."""

    ok: bool
    result: dict[str, Any] | None = None
    error: ToolError | None = None

    @classmethod
    def success(cls, result: dict[str, Any]) -> ToolResult:
        """Build a successful result."""
        return cls(ok=True, result=result)

    @classmethod
    def failure(cls, code: str, message: str, retryable: bool = False) -> ToolResult:
        """Build a failed result."""
        return cls(
            ok=False,
            error=ToolError(code=code, message=message, retryable=retryable),
        )


@dataclass
class ConnectResult:
    """The outcome of a connect/refresh attempt.

    Mutable so the host may attach a ``redirect_url`` after the plugin returns.
    """

    status: Status
    message: str | None = None
    redirect_url: str | None = None


__all__ = [
    "AuthMode",
    "ConnectResult",
    "Status",
    "Tool",
    "ToolError",
    "ToolResult",
]
