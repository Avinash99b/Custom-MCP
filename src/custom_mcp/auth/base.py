"""Authentication contracts shared by middleware, handlers and implementations.

``Principal`` deliberately carries only an identity. Which mechanism produced
the principal (bearer token vs. dashboard session) lives in generic auth
metadata attached to ``request.state`` so the middleware never has to know how
authentication is implemented.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from starlette.requests import Request

AuthMethod = Literal["bearer", "session"]
AUTH_STATE_ATTR = "auth"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated identity."""

    id: str


@dataclass(frozen=True, slots=True)
class AuthInfo:
    """Generic, mechanism-agnostic authentication metadata."""

    method: AuthMethod
    session_id: str | None = None


def set_auth_info(request: Request, info: AuthInfo) -> None:
    """Attach authentication metadata to the request state."""
    setattr(request.state, AUTH_STATE_ATTR, info)


def get_auth_info(request: Request) -> AuthInfo | None:
    """Return authentication metadata previously attached by an authenticator."""
    info = getattr(request.state, AUTH_STATE_ATTR, None)
    return info if isinstance(info, AuthInfo) else None


class Authenticator(ABC):
    """Resolve an incoming request to a :class:`Principal`.

    Implementations set ``request.state`` auth metadata (see
    :func:`set_auth_info`) so middleware can apply bearer-only and CSRF rules
    without knowing the concrete mechanism.
    """

    @abstractmethod
    async def authenticate(self, request: Request) -> Principal | None:
        """Return the authenticated principal, or ``None`` when unauthenticated."""

    def verify_login_secret(self, candidate: str) -> bool:
        """Verify a direct login secret.

        Authenticators that do not support secret-based browser login return
        ``False`` so the shared login endpoint cannot be used with them.
        """
        return False


__all__ = [
    "AUTH_STATE_ATTR",
    "AuthInfo",
    "AuthMethod",
    "Authenticator",
    "Principal",
    "get_auth_info",
    "set_auth_info",
]
