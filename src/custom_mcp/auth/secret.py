"""Owner secret authentication: bearer tokens plus session fallback."""

from __future__ import annotations

import hmac

from starlette.requests import Request

from custom_mcp.auth.base import Authenticator, AuthInfo, Principal, set_auth_info
from custom_mcp.auth.sessions import SESSION_COOKIE_NAME, SessionService

OWNER_SUBJECT = "owner"


def extract_bearer_token(request: Request) -> str | None:
    """Return the bearer token from the Authorization header, if present.

    Only the single separator space after the scheme is removed; the candidate
    is returned byte-for-byte so it can be compared exactly to the configured
    secret.
    """
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token or None


class SecretAuthenticator(Authenticator):
    """Authenticate the single owner via a shared secret.

    A valid ``Authorization: Bearer <secret>`` yields a ``bearer`` principal.
    Otherwise a valid Redis-backed dashboard session cookie yields a ``session``
    principal. Comparisons are constant-time over UTF-8 bytes.
    """

    def __init__(
        self,
        secret: str,
        sessions: SessionService,
        *,
        subject: str = OWNER_SUBJECT,
    ) -> None:
        self._secret_bytes = secret.encode("utf-8")
        self._sessions = sessions
        self._subject = subject

    def verify_login_secret(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), self._secret_bytes)

    def _verify_bearer(self, token: str) -> bool:
        return hmac.compare_digest(token.encode("utf-8"), self._secret_bytes)

    async def authenticate(self, request: Request) -> Principal | None:
        token = extract_bearer_token(request)
        if token is not None and self._verify_bearer(token):
            set_auth_info(request, AuthInfo(method="bearer"))
            return Principal(id=self._subject)

        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id:
            session = await self._sessions.get(session_id)
            if session is not None:
                set_auth_info(request, AuthInfo(method="session", session_id=session.id))
                return Principal(id=self._subject)
        return None


__all__ = ["OWNER_SUBJECT", "SecretAuthenticator", "extract_bearer_token"]
