"""Gmail connector plugin (Google OAuth 2.0 authorization code + PKCE).

The host loads this module as the isolated package ``mcp_plugin_gmail`` and
resolves ``GmailPlugin`` from the manifest entry ``plugin:GmailPlugin``. It
advertises only ``oauth`` auth and reads mail / creates drafts / sends mail with
the Gmail REST API.

The core drives the OAuth flow:

1. ``connect()`` reports ``oauth_required`` until a valid access token is stored.
2. ``get_oauth_sign_in_url(state)`` mints a PKCE ``code_verifier``, stores it in
   scoped data under ``pkce:{state}`` with a 600 second TTL, and returns the
   Google authorize URL carrying the S256 ``code_challenge``.
3. ``on_oauth_result(code, state)`` exchanges the code (plus verifier and the
   core-stored client secret) for tokens and stores the derived values in
   **secrets** under non-reserved names.
4. ``refresh()`` rotates the stored refresh token and reports ``connected``.

No token, verifier, code, or client secret is ever copied into a tool result, a
log line, or plugin data. Every upstream HTTP call goes through the single
module-level :func:`_http_request` helper so a test can monkeypatch one symbol.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets as _secrets
import time
from email import policy as email_policy
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlencode

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

#: Reserved scoped keys the core writes before calling ``on_oauth_credentials_set``.
CORE_OAUTH_CLIENT_ID = "core.oauth_client_id"
CORE_OAUTH_CLIENT_SECRET = "core.oauth_client_secret"

#: Non-reserved scoped secret keys holding the derived OAuth material.
ACCESS_TOKEN_KEY = "gmail_access_token"
REFRESH_TOKEN_KEY = "gmail_refresh_token"
EXPIRES_AT_KEY = "gmail_expires_at"

#: Scoped data key prefix for the per-state PKCE verifier and its TTL.
PKCE_PREFIX = "pkce:"
PKCE_TTL_SECONDS = 600

#: Scoped data key recording what ``on_oauth_credentials_set`` observed.
CREDENTIALS_HOOK_KEY = "oauth_credentials_hook"

#: Scopes requested at sign-in. ``compose`` covers drafts and sending.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)

#: Environment variables overriding the Google endpoints (used by tests).
AUTHORIZE_URL_ENV = "CUSTOM_MCP_GMAIL_AUTHORIZE_URL"
TOKEN_URL_ENV = "CUSTOM_MCP_GMAIL_TOKEN_URL"
API_BASE_URL_ENV = "CUSTOM_MCP_GMAIL_API_BASE_URL"

DEFAULT_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_API_BASE_URL = "https://gmail.googleapis.com"

#: Short timeout so a stuck worker thread is bounded.
HTTP_TIMEOUT = 5.0

#: Page size bounds for ``list_messages``.
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_CAP = 50

#: PKCE verifier length (RFC 7636 allows 43-128 characters).
_VERIFIER_BYTES = 64

_NOT_FOUND = "not_found"
_AUTH_EXPIRED = "auth_expired"
_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
_RATE_LIMITED = "rate_limited"
_INVALID_ARGUMENTS = "invalid_arguments"
_UPSTREAM_ERROR = "upstream_error"

_API_ROOT = "/gmail/v1/users/me"

#: Gmail message ids are opaque hex-ish tokens; anything else is refused so an
#: argument can never reshape the upstream URL path.
_MESSAGE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _authorize_url() -> str:
    """Return the Google authorize endpoint (environment overridable)."""
    return os.environ.get(AUTHORIZE_URL_ENV, DEFAULT_AUTHORIZE_URL)


def _token_url() -> str:
    """Return the Google token endpoint (environment overridable)."""
    return os.environ.get(TOKEN_URL_ENV, DEFAULT_TOKEN_URL)


def _api_base_url() -> str:
    """Return the Gmail API base URL without a trailing slash."""
    return os.environ.get(API_BASE_URL_ENV, DEFAULT_API_BASE_URL).rstrip("/")


def _http_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> requests.Response:
    """Perform one upstream HTTP call.

    Every network call in this plugin funnels through this single helper so a
    test can monkeypatch it without touching ``requests`` itself.
    """
    return requests.request(
        method,
        url,
        params=params,
        data=data,
        json=json,
        headers=headers,
        timeout=timeout,
    )


def _code_challenge(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for ``verifier`` (unpadded base64url)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_raw_message(
    *,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> str:
    """Build an RFC 2822 message and return it as unpadded base64url.

    ``From`` is left to Gmail, which always sets it to the authenticated user.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message.set_content(body)
    # ``policy.SMTP`` emits RFC 2822 CRLF line endings; the default policy emits
    # bare LF, which is not a valid wire format for ``raw``.
    raw_bytes = message.as_bytes(policy=email_policy.SMTP)
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


# --- JSON Schemas (a fresh object per call, never a shared nested dict) ------


def _empty_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def _list_messages_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": "Gmail search query, e.g. 'is:unread newer_than:7d'.",
            },
            "maxResults": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS_CAP,
                "default": DEFAULT_MAX_RESULTS,
                "description": f"Maximum messages to return (capped at {MAX_RESULTS_CAP}).",
            },
        },
        "required": [],
        "additionalProperties": False,
    }


def _get_message_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "pattern": "^[A-Za-z0-9_-]{1,128}$",
                "description": "Gmail message id.",
            },
            "format": {
                "type": "string",
                "enum": ["metadata", "full"],
                "default": "metadata",
                "description": "Gmail message format.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    }


def _compose_schema(*, include_cc: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "to": {"type": "string", "minLength": 1, "description": "Recipient address(es)."},
        "subject": {"type": "string", "description": "Subject line."},
        "body": {"type": "string", "description": "Plain-text body."},
    }
    if include_cc:
        properties["cc"] = {"type": "string", "description": "Optional Cc address(es)."}
        properties["bcc"] = {"type": "string", "description": "Optional Bcc address(es)."}
    return {
        "type": "object",
        "properties": properties,
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
    }


class GmailPlugin(MCPPlugin):
    """A read/compose Gmail connector backed by a stored OAuth access token."""

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self._last_status: Status = Status.OAUTH_REQUIRED

    # --- required methods ---------------------------------------------------

    def auth_methods(self) -> list[AuthMode]:
        return [AuthMode.OAUTH]

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="whoami",
                description=(
                    "Report the auth mode and whether a token is present. Never "
                    "returns any part of a token."
                ),
                input_schema=_empty_schema(),
            ),
            Tool(
                name="list_labels",
                description="List the mailbox labels (id, name, type).",
                input_schema=_empty_schema(),
            ),
            Tool(
                name="list_messages",
                description=(
                    "List message ids matching an optional Gmail search query."
                ),
                input_schema=_list_messages_schema(),
            ),
            Tool(
                name="get_message",
                description=(
                    "Fetch one message's ids, snippet and Subject/From/Date headers."
                ),
                input_schema=_get_message_schema(),
            ),
            Tool(
                name="create_draft",
                description=(
                    "Create a Gmail draft from to/subject/body (plus optional cc/bcc). "
                    "Does not send anything."
                ),
                input_schema=_compose_schema(include_cc=True),
            ),
            Tool(
                name="send_message",
                description=(
                    "DESTRUCTIVE: send an email immediately to the given recipients. "
                    "This delivers real mail and cannot be undone."
                ),
                input_schema=_compose_schema(include_cc=True),
            ),
        ]

    def connect(self) -> ConnectResult:
        if not self._has_valid_token():
            self._last_status = Status.OAUTH_REQUIRED
            return ConnectResult(
                status=Status.OAUTH_REQUIRED, message="Google OAuth sign-in required."
            )
        self._last_status = Status.CONNECTED
        return ConnectResult(
            status=Status.CONNECTED, message="Connected with the stored Google token."
        )

    def ping(self) -> Status:
        if self._has_valid_token():
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
        if name == "list_labels":
            return self._list_labels_result()
        if name == "list_messages":
            return self._list_messages_result(args)
        if name == "get_message":
            return self._get_message_result(args)
        if name == "create_draft":
            return self._create_draft_result(args)
        if name == "send_message":
            return self._send_message_result(args)
        return ToolResult.failure(_NOT_FOUND, f"Unknown tool: {name}")

    # --- optional hooks -----------------------------------------------------

    def on_oauth_credentials_set(
        self, client_id: str, client_secret: str | None
    ) -> None:
        """Record what the core stored before this hook ran.

        Neither the client id nor the secret is copied into the record; only
        presence/matching booleans are kept.
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
        self.ctx.storage.set(f"{PKCE_PREFIX}{state}", verifier, ttl=PKCE_TTL_SECONDS)
        params = {
            "response_type": "code",
            "client_id": self._client_id() or "",
            "redirect_uri": self.ctx.callback_url,
            "state": state,
            "scope": " ".join(SCOPES),
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{_authorize_url()}?{urlencode(params)}"

    def on_oauth_result(self, code: str, state: str) -> ConnectResult:
        """Exchange the authorization code (plus verifier) for tokens."""
        verifier = self._pkce_verifier(state)
        if verifier is None:
            return ConnectResult(
                status=Status.AUTH_EXPIRED,
                message="The Google sign-in expired or was not started.",
            )

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": self.ctx.callback_url,
            "client_id": self._client_id() or "",
        }
        client_secret = self._client_secret()
        if client_secret is not None:
            payload["client_secret"] = client_secret

        try:
            response = _http_request(
                "POST",
                _token_url(),
                data=payload,
                headers={"Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            status_code = int(response.status_code)
        except requests.RequestException:
            return ConnectResult(
                status=Status.ERROR, message="The Google token endpoint is unavailable."
            )

        self._delete_pkce(state)
        if not 200 <= status_code < 300:
            if status_code in (400, 401, 403):
                return ConnectResult(
                    status=Status.AUTH_EXPIRED,
                    message="Google rejected the authorization code.",
                )
            return ConnectResult(
                status=Status.ERROR,
                message=f"Google returned an unexpected status ({status_code}).",
            )

        body = _decode_payload(response)
        if body is None or not _valid_token(body.get("access_token")):
            return ConnectResult(
                status=Status.ERROR,
                message="Google returned an invalid token response.",
            )

        self._apply_token_payload(body, rotated=False)
        self._last_status = Status.CONNECTED
        return ConnectResult(
            status=Status.CONNECTED, message="Connected with the Google OAuth token."
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
        client_secret = self._client_secret()
        if client_secret is not None:
            payload["client_secret"] = client_secret

        try:
            response = _http_request(
                "POST",
                _token_url(),
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

    def on_disconnect(self) -> None:
        """Drop derived token material so a reconnect needs a fresh sign-in."""
        for key in (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, EXPIRES_AT_KEY):
            self.ctx.secrets.delete(key)
        self._last_status = Status.NOT_CONNECTED

    # --- stored credential helpers -----------------------------------------

    def _access_token(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(ACCESS_TOKEN_KEY))

    def _refresh_token(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(REFRESH_TOKEN_KEY))

    def _client_id(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(CORE_OAUTH_CLIENT_ID))

    def _client_secret(self) -> str | None:
        return _secret_string(self.ctx.secrets.get(CORE_OAUTH_CLIENT_SECRET))

    def _has_valid_token(self) -> bool:
        return self._access_token() is not None and not self._is_expired()

    def _is_expired(self) -> bool:
        expires_at = self.ctx.secrets.get(EXPIRES_AT_KEY)
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            return float(expires_at) <= time.time()
        return False

    def _pkce_verifier(self, state: str) -> str | None:
        return _secret_string(self.ctx.storage.get(f"{PKCE_PREFIX}{state}"))

    def _delete_pkce(self, state: str) -> None:
        self.ctx.storage.delete(f"{PKCE_PREFIX}{state}")

    def _apply_token_payload(self, payload: dict[str, Any], *, rotated: bool) -> None:
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

    # --- Gmail API plumbing -------------------------------------------------

    def _api_call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Call the Gmail API; return ``(body, None)`` or ``(None, failure)``."""
        access = self._access_token()
        if access is None:
            return None, ToolResult.failure(
                _AUTH_EXPIRED, "No Google access token is stored."
            )

        try:
            response = _http_request(
                method,
                f"{_api_base_url()}{_API_ROOT}{path}",
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "application/json",
                },
                timeout=HTTP_TIMEOUT,
            )
            status_code = int(response.status_code)
        except requests.RequestException:
            return None, ToolResult.failure(
                _UPSTREAM_UNAVAILABLE, "The Gmail API is unavailable.", retryable=True
            )

        if 200 <= status_code < 300:
            body = _decode_payload(response)
            if body is None:
                return None, ToolResult.failure(
                    _UPSTREAM_UNAVAILABLE,
                    "The Gmail API returned an invalid response.",
                    retryable=True,
                )
            return body, None
        return None, _map_status(status_code)

    # --- tool results -------------------------------------------------------

    def _whoami_result(self) -> ToolResult:
        access = self._access_token()
        return ToolResult.success(
            {
                "mode": AuthMode.OAUTH.value,
                "connected": self._has_valid_token(),
                "token_present": access is not None,
                "refreshable": self._refresh_token() is not None,
            }
        )

    def _list_labels_result(self) -> ToolResult:
        body, failure = self._api_call("GET", "/labels")
        if failure is not None:
            return failure
        assert body is not None
        labels: list[dict[str, Any]] = []
        raw = body.get("labels")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    labels.append(
                        {
                            "id": item.get("id"),
                            "name": item.get("name"),
                            "type": item.get("type"),
                        }
                    )
        return ToolResult.success({"labels": labels})

    def _list_messages_result(self, args: dict[str, Any]) -> ToolResult:
        params: dict[str, Any] = {"maxResults": _bounded_max_results(args)}
        query = _optional_arg_str(args, "q")
        if query is not None:
            params["q"] = query

        body, failure = self._api_call("GET", "/messages", params=params)
        if failure is not None:
            return failure
        assert body is not None

        messages: list[dict[str, Any]] = []
        raw = body.get("messages")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    messages.append(
                        {"id": item.get("id"), "threadId": item.get("threadId")}
                    )
        result: dict[str, Any] = {
            "messages": messages,
            "resultSizeEstimate": body.get("resultSizeEstimate"),
        }
        next_page = body.get("nextPageToken")
        if isinstance(next_page, str):
            result["nextPageToken"] = next_page
        return ToolResult.success(result)

    def _get_message_result(self, args: dict[str, Any]) -> ToolResult:
        message_id = _arg_str(args, "id")
        if message_id is None:
            return ToolResult.failure(
                _INVALID_ARGUMENTS, "A non-empty 'id' argument is required."
            )
        if _MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
            return ToolResult.failure(
                _INVALID_ARGUMENTS,
                "'id' must be a Gmail message id (letters, digits, '-' or '_').",
            )
        fmt = args.get("format", "metadata")
        if fmt not in ("metadata", "full"):
            return ToolResult.failure(
                _INVALID_ARGUMENTS, "'format' must be 'metadata' or 'full'."
            )

        body, failure = self._api_call(
            "GET", f"/messages/{message_id}", params={"format": fmt}
        )
        if failure is not None:
            return failure
        assert body is not None

        headers = _headers_map(body.get("payload"))
        label_ids = body.get("labelIds")
        return ToolResult.success(
            {
                "id": body.get("id"),
                "threadId": body.get("threadId"),
                "labelIds": label_ids if isinstance(label_ids, list) else [],
                "snippet": body.get("snippet"),
                "subject": headers.get("Subject"),
                "from": headers.get("From"),
                "date": headers.get("Date"),
                "headers": headers,
            }
        )

    def _create_draft_result(self, args: dict[str, Any]) -> ToolResult:
        raw, failure = self._compose_raw(args)
        if failure is not None:
            return failure
        assert raw is not None

        body, failure = self._api_call(
            "POST", "/drafts", json_body={"message": {"raw": raw}}
        )
        if failure is not None:
            return failure
        assert body is not None

        message = body.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        return ToolResult.success(
            {"draft_id": body.get("id"), "message_id": message_id}
        )

    def _send_message_result(self, args: dict[str, Any]) -> ToolResult:
        raw, failure = self._compose_raw(args)
        if failure is not None:
            return failure
        assert raw is not None

        body, failure = self._api_call(
            "POST", "/messages/send", json_body={"raw": raw}
        )
        if failure is not None:
            return failure
        assert body is not None

        label_ids = body.get("labelIds")
        return ToolResult.success(
            {
                "message_id": body.get("id"),
                "threadId": body.get("threadId"),
                "labelIds": label_ids if isinstance(label_ids, list) else [],
            }
        )

    def _compose_raw(
        self, args: dict[str, Any]
    ) -> tuple[str | None, ToolResult | None]:
        """Validate compose arguments and build the base64url ``raw`` message."""
        to = _arg_str(args, "to")
        subject = args.get("subject")
        body = args.get("body")
        cc = _optional_arg_str(args, "cc")
        bcc = _optional_arg_str(args, "bcc")

        if to is None:
            return None, ToolResult.failure(
                _INVALID_ARGUMENTS, "A non-empty 'to' argument is required."
            )
        if not isinstance(subject, str) or not isinstance(body, str):
            return None, ToolResult.failure(
                _INVALID_ARGUMENTS, "'subject' and 'body' must be strings."
            )
        for value in (to, subject, cc, bcc):
            if value is not None and _has_header_break(value):
                return None, ToolResult.failure(
                    _INVALID_ARGUMENTS, "Header values must not contain line breaks."
                )

        try:
            raw = _build_raw_message(
                to=to, subject=subject, body=body, cc=cc, bcc=bcc
            )
        except (ValueError, TypeError):
            return None, ToolResult.failure(
                _INVALID_ARGUMENTS, "The message headers could not be built."
            )
        return raw, None


def _map_status(status_code: int) -> ToolResult:
    """Map an upstream HTTP status to the structured failure contract."""
    if status_code in (401, 403):
        return ToolResult.failure(
            _AUTH_EXPIRED, "Google rejected the access token. Sign in again."
        )
    if status_code == 404:
        return ToolResult.failure(_NOT_FOUND, "The requested Gmail resource was not found.")
    if status_code == 429:
        return ToolResult.failure(
            _RATE_LIMITED, "The Gmail API rate limit was hit.", retryable=True
        )
    if status_code >= 500:
        return ToolResult.failure(
            _UPSTREAM_UNAVAILABLE, "The Gmail API is unavailable.", retryable=True
        )
    return ToolResult.failure(
        _UPSTREAM_ERROR, f"The Gmail API returned an unexpected status ({status_code})."
    )


def _bounded_max_results(args: dict[str, Any]) -> int:
    """Return a sane, bounded page size for ``list_messages``."""
    value = args.get("maxResults", DEFAULT_MAX_RESULTS)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_MAX_RESULTS
    return max(1, min(value, MAX_RESULTS_CAP))


def _arg_str(args: dict[str, Any], key: str) -> str | None:
    """Return a non-blank string argument, or ``None``."""
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_arg_str(args: dict[str, Any], key: str) -> str | None:
    """Return an optional non-blank string argument, or ``None``."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


def _has_header_break(value: str) -> bool:
    """Whether ``value`` would inject a new header line."""
    return "\r" in value or "\n" in value


def _headers_map(payload: Any) -> dict[str, str]:
    """Return the header name/value pairs from a Gmail message payload."""
    headers: dict[str, str] = {}
    if not isinstance(payload, dict):
        return headers
    raw = payload.get("headers")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name")
                value = item.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    headers[name] = value
    return headers


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
