"""OAuth 2.0 authorization-code + PKCE example plugin.

The host loads this module as the isolated package ``mcp_plugin_oauth-demo`` and
resolves ``OAuthDemoPlugin`` from the manifest entry ``plugin:OAuthDemoPlugin``.
It advertises only ``oauth`` auth.

The core drives the flow:

1. ``connect()`` reports ``oauth_required`` until a valid access token is stored.
2. ``get_oauth_sign_in_url(state)`` mints a PKCE ``code_verifier``, stores it in
   scoped data under ``pkce:{state}`` with a 600 second TTL, and returns the
   provider authorize URL carrying the S256 ``code_challenge``.
3. ``on_oauth_result(code, state)`` exchanges the code (plus verifier) for tokens
   and stores the derived values in **secrets** under non-reserved names.
4. ``refresh()`` rotates a stored refresh token and reports ``connected``.

No token, verifier, code, or client secret is ever copied into a tool result, a
log line, or plugin data. Every upstream HTTP call goes through the single
module-level :func:`_http_request` helper so a test can monkeypatch one symbol.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
import time
from typing import Any
from urllib.parse import quote

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

#: Reserved scoped key the core writes before calling ``on_oauth_credentials_set``.
CORE_OAUTH_CLIENT_ID = "core.oauth_client_id"

#: Non-reserved scoped secret keys holding the derived OAuth material.
ACCESS_TOKEN_KEY = "oauth_access_token"
REFRESH_TOKEN_KEY = "oauth_refresh_token"
EXPIRES_AT_KEY = "oauth_expires_at"

#: Scoped data key prefix for the per-state PKCE verifier and its TTL.
PKCE_PREFIX = "pkce:"
PKCE_TTL_SECONDS = 600

#: Scoped data key recording what ``on_oauth_credentials_set`` observed.
CREDENTIALS_HOOK_KEY = "oauth_credentials_hook"

#: Environment variable overriding the provider base URL.
PROVIDER_URL_ENV = "CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL"

#: Base URL used when the environment variable is unset.
DEFAULT_PROVIDER_URL = "https://oauth.example.invalid"

#: Short timeout so a stuck worker thread is bounded.
HTTP_TIMEOUT = 5.0

#: PKCE verifier length (RFC 7636 allows 43-128 characters).
_VERIFIER_BYTES = 64

_NOT_FOUND = "not_found"
_AUTH_EXPIRED = "auth_expired"
_UPSTREAM_UNAVAILABLE = "upstream_unavailable"


def _provider_base_url() -> str:
    """Return the provider base URL from the environment (no trailing slash)."""
    return os.environ.get(PROVIDER_URL_ENV, DEFAULT_PROVIDER_URL).rstrip("/")


def _http_request(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> requests.Response:
    """Perform one upstream HTTP call.

    Every network call in this plugin funnels through this single helper so a
    test can monkeypatch it without touching ``requests`` itself.
    """
    return requests.request(method, url, data=data, headers=headers, timeout=timeout)


def _code_challenge(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for ``verifier`` (unpadded base64url)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _whoami_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for ``whoami`` (no shared nested objects)."""
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _me_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema for ``me``."""
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


class OAuthDemoPlugin(MCPPlugin):
    """A minimal ``oauth`` plugin whose tools depend on a stored access token."""

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self._last_status: Status = Status.OAUTH_REQUIRED

    # --- required methods ---------------------------------------------------

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.OAUTH]

    def list_tools(self) -> list[Tool]:
        tools = [
            Tool(
                name="whoami",
                description=(
                    "Report the auth mode and token presence. Never returns any "
                    "part of a token."
                ),
                input_schema=_whoami_schema(),
            )
        ]
        # ``me`` only appears once a token is stored, so signing in changes the
        # plugin's live tool set.
        if self._access_token() is not None:
            tools.append(
                Tool(
                    name="me",
                    description=(
                        "Fetch the signed-in subject from the provider using the "
                        "stored access token."
                    ),
                    input_schema=_me_schema(),
                )
            )
        return tools

    def connect(self) -> ConnectResult:
        if self._access_token() is None or self._is_expired():
            self._last_status = Status.OAUTH_REQUIRED
            return ConnectResult(
                status=Status.OAUTH_REQUIRED, message="OAuth sign-in required."
            )
        self._last_status = Status.CONNECTED
        return ConnectResult(
            status=Status.CONNECTED, message="Connected with the stored OAuth token."
        )

    def ping(self) -> Status:
        if self._access_token() is not None and not self._is_expired():
            self._last_status = Status.CONNECTED
            return Status.CONNECTED
        if self._refresh_token() is not None:
            self._last_status = Status.AUTH_EXPIRED
            return Status.AUTH_EXPIRED
        self._last_status = Status.NOT_CONNECTED
        return Status.NOT_CONNECTED

    def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == "whoami":
            return self._whoami_result()
        if name == "me":
            return self._me_result()
        return ToolResult.failure(_NOT_FOUND, f"Unknown tool: {name}")

    # --- optional hooks -----------------------------------------------------

    def on_oauth_credentials_set(
        self, client_id: str, client_secret: str | None
    ) -> None:
        """Record what the core stored before this hook ran.

        The contract guarantees ``core.oauth_client_id`` already holds
        ``client_id``; we record whether that held true instead of raising, so a
        mismatch stays observable without failing the connect. The client secret
        is never read, stored, or logged.
        """
        stored = self.ctx.secrets.get(CORE_OAUTH_CLIENT_ID)
        self.ctx.storage.set(
            CREDENTIALS_HOOK_KEY,
            {
                "stored": stored is not None,
                "matched": stored == client_id,
                "has_secret": client_secret is not None,
            },
        )

    def get_oauth_sign_in_url(self, state: str) -> str:
        """Mint a PKCE verifier for ``state`` and return the authorize URL."""
        verifier = _secrets.token_urlsafe(_VERIFIER_BYTES)
        self.ctx.storage.set(
            f"{PKCE_PREFIX}{state}", verifier, ttl=PKCE_TTL_SECONDS
        )
        params = {
            "response_type": "code",
            "client_id": self._client_id() or "",
            "redirect_uri": self.ctx.callback_url,
            "state": state,
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        query = "&".join(
            f"{key}={quote(value, safe='')}" for key, value in params.items()
        )
        return f"{_provider_base_url()}/authorize?{query}"

    def on_oauth_result(self, code: str, state: str) -> ConnectResult:
        """Exchange the authorization code (plus verifier) for tokens."""
        verifier = self._pkce_verifier(state)
        if verifier is None:
            return ConnectResult(
                status=Status.AUTH_EXPIRED,
                message="The OAuth sign-in expired or was not started.",
            )

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": self.ctx.callback_url,
            "client_id": self._client_id() or "",
        }
        try:
            response = _http_request(
                "POST",
                f"{_provider_base_url()}/token",
                data=payload,
                headers={"Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            status_code = int(response.status_code)
        except requests.RequestException:
            return ConnectResult(
                status=Status.ERROR, message="The identity provider is unavailable."
            )

        self._delete_pkce(state)
        if not 200 <= status_code < 300:
            if status_code in (400, 401, 403):
                return ConnectResult(
                    status=Status.AUTH_EXPIRED,
                    message="The identity provider rejected the authorization code.",
                )
            return ConnectResult(
                status=Status.ERROR,
                message=(
                    "The identity provider returned an unexpected status "
                    f"({status_code})."
                ),
            )

        body = _decode_payload(response)
        if body is None or not _valid_token(body.get("access_token")):
            return ConnectResult(
                status=Status.ERROR,
                message="The identity provider returned an invalid token response.",
            )

        self._apply_token_payload(body, rotated=False)
        self._last_status = Status.CONNECTED
        return ConnectResult(
            status=Status.CONNECTED, message="Connected with the OAuth token."
        )

    def refresh(self) -> Status:
        """Rotate the stored refresh token, or report an expired auth."""
        refresh_token = self._refresh_token()
        if refresh_token is None:
            self._last_status = Status.AUTH_EXPIRED
            return Status.AUTH_EXPIRED

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id() or "",
        }
        try:
            response = _http_request(
                "POST",
                f"{_provider_base_url()}/token",
                data=payload,
                headers={"Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            status_code = int(response.status_code)
        except requests.RequestException:
            return Status.ERROR

        if not 200 <= status_code < 300:
            if status_code in (400, 401, 403):
                self._last_status = Status.AUTH_EXPIRED
                return Status.AUTH_EXPIRED
            return Status.ERROR

        body = _decode_payload(response)
        if body is None or not _valid_token(body.get("access_token")):
            return Status.ERROR

        self._apply_token_payload(body, rotated=True)
        self._last_status = Status.CONNECTED
        return Status.CONNECTED

    # --- internal helpers ---------------------------------------------------

    def _access_token(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(ACCESS_TOKEN_KEY))

    def _refresh_token(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(REFRESH_TOKEN_KEY))

    def _client_id(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(CORE_OAUTH_CLIENT_ID))

    def _is_expired(self) -> bool:
        expires_at = self.ctx.secrets.get(EXPIRES_AT_KEY)
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            return float(expires_at) <= time.time()
        return False

    def _pkce_verifier(self, state: str) -> str | None:
        return _secret_string(self.ctx.storage.get(f"{PKCE_PREFIX}{state}"))

    def _delete_pkce(self, state: str) -> None:
        self.ctx.storage.delete(f"{PKCE_PREFIX}{state}")

    def _apply_token_payload(
        self, payload: dict[str, Any], *, rotated: bool
    ) -> None:
        """Persist derived token material without ever logging it.

        During a rotation the new refresh token is written first so the
        long-lived credential is never left stale relative to the access token.
        """
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if rotated:
            if _valid_token(refresh):
                self.ctx.secrets.set(REFRESH_TOKEN_KEY, refresh)
            if _valid_token(access):
                self.ctx.secrets.set(ACCESS_TOKEN_KEY, access)
        else:
            if _valid_token(access):
                self.ctx.secrets.set(ACCESS_TOKEN_KEY, access)
            if _valid_token(refresh):
                self.ctx.secrets.set(REFRESH_TOKEN_KEY, refresh)

        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            self.ctx.secrets.set(EXPIRES_AT_KEY, time.time() + float(expires_in))

    def _whoami_result(self) -> ToolResult:
        access = self._access_token()
        return ToolResult.success(
            {
                "mode": AuthMode.OAUTH.value,
                "connected": access is not None
                and self._last_status is Status.CONNECTED,
                "token_present": access is not None,
                "refreshable": self._refresh_token() is not None,
            }
        )

    def _me_result(self) -> ToolResult:
        access = self._access_token()
        if access is None:
            return ToolResult.failure(_AUTH_EXPIRED, "No OAuth access token is stored.")

        try:
            response = _http_request(
                "GET",
                f"{_provider_base_url()}/me",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "application/json",
                },
                timeout=HTTP_TIMEOUT,
            )
            status_code = int(response.status_code)
        except requests.RequestException:
            return ToolResult.failure(
                _UPSTREAM_UNAVAILABLE,
                "The identity provider is unavailable.",
                retryable=True,
            )

        if 200 <= status_code < 300:
            body = _decode_payload(response)
            if body is None:
                return ToolResult.failure(
                    _UPSTREAM_UNAVAILABLE,
                    "The identity provider returned an invalid response.",
                    retryable=True,
                )
            return ToolResult.success({"sub": body.get("sub"), "name": body.get("name")})
        if status_code in (401, 403):
            return ToolResult.failure(
                _AUTH_EXPIRED, "The identity provider rejected the access token."
            )
        if status_code >= 500:
            return ToolResult.failure(
                _UPSTREAM_UNAVAILABLE,
                "The identity provider is unavailable.",
                retryable=True,
            )
        return ToolResult.failure(
            "upstream_error",
            f"The identity provider returned an unexpected status ({status_code}).",
        )


def _secret_string(value: Any) -> str | None:
    """Return a non-empty string value, or ``None`` for anything else."""
    if isinstance(value, str) and value:
        return value
    return None


def _valid_token(value: Any) -> bool:
    """Whether ``value`` looks like a usable non-empty token string."""
    return isinstance(value, str) and bool(value)


def _decode_payload(response: requests.Response) -> dict[str, Any] | None:
    """Return a JSON object body, or ``None`` when it is missing/malformed."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
