"""A self-contained loopback OAuth 2.0 + PKCE provider for tests.

The plugin under test performs real synchronous ``requests`` calls, so this
module runs a genuine HTTP server on ``127.0.0.1`` with an ephemeral port. Tests
use it as a context manager (or via the ``mock_oauth_provider`` fixture) and can
inspect :attr:`MockOAuthProvider.requests` afterwards.

Endpoints:

* ``GET /authorize`` -- validates ``response_type``/``client_id``/``redirect_uri``/
  ``state``/``code_challenge`` (S256), issues a one-time ``code`` and redirects to
  ``<redirect_uri>?code=...&state=...``. ``force_authorize_error`` instead
  redirects with ``error=access_denied``.
* ``POST /token`` -- ``authorization_code`` verifies the S256 verifier against the
  stored challenge and checks ``redirect_uri``; ``refresh_token`` verifies and
  rotates. Returns ``{"access_token", "refresh_token", "token_type",
  "expires_in"}``. A wrong verifier is rejected with ``400``.
* ``GET /me`` -- requires ``Authorization: Bearer <access_token>`` and returns a
  JSON subject; ``401`` when the token is absent or unknown.

The server is thread-safe and shut down cleanly by :meth:`stop`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

DEFAULT_CLIENT_ID = "oauth-demo-client"
DEFAULT_SUBJECT = "user-42"
DEFAULT_SUBJECT_NAME = "OAuth Demo User"
ACCESS_TOKEN_TTL = 3600


def s256(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for ``verifier`` (unpadded base64url)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class RecordedRequest:
    """One request observed by the provider."""

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class MockOAuthProvider:
    """A loopback OAuth provider exercising the full authorize/code/token flow."""

    def __init__(
        self,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str | None = None,
        subject: str = DEFAULT_SUBJECT,
        subject_name: str = DEFAULT_SUBJECT_NAME,
        expires_in: int = ACCESS_TOKEN_TTL,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.subject = subject
        self.subject_name = subject_name
        self.expires_in = expires_in
        self.force_authorize_error = False

        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._codes: dict[str, dict[str, Any]] = {}
        self._access: dict[str, str] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._code_counter = 0
        self._token_counter = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> MockOAuthProvider:
        if self._server is not None:
            return self
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(self))
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="mock-oauth-provider", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> MockOAuthProvider:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- inspection helpers -------------------------------------------------

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            raise RuntimeError("provider is not started")
        address = server.server_address
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{int(address[1])}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def requests_for(self, path: str) -> list[RecordedRequest]:
        return [record for record in self.requests if record.path == path]

    @property
    def active_access_tokens(self) -> set[str]:
        with self._lock:
            return set(self._access)

    @property
    def active_refresh_tokens(self) -> set[str]:
        with self._lock:
            return set(self._refresh)

    # --- request handling ---------------------------------------------------

    def _record(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        form: dict[str, str],
        headers: dict[str, str],
    ) -> None:
        record = RecordedRequest(
            method=method, path=path, query=query, form=form, headers=headers
        )
        with self._lock:
            self.requests.append(record)

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        self._record("GET", parsed.path, query, {}, _lower_headers(handler))
        if parsed.path == "/authorize":
            self._authorize(handler, query)
        elif parsed.path == "/me":
            self._me(handler)
        else:
            self._send_json(handler, 404, {"error": "not_found"})

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        form = {
            key: value[0]
            for key, value in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()
        }
        self._record("POST", parsed.path, {}, form, _lower_headers(handler))
        if parsed.path == "/token":
            self._token(handler, form)
        else:
            self._send_json(handler, 404, {"error": "not_found"})

    def _authorize(
        self, handler: BaseHTTPRequestHandler, query: dict[str, str]
    ) -> None:
        redirect_uri = query.get("redirect_uri", "")
        valid = (
            query.get("response_type") == "code"
            and query.get("client_id") == self.client_id
            and bool(redirect_uri)
            and bool(query.get("state"))
            and bool(query.get("code_challenge"))
            and query.get("code_challenge_method") == "S256"
        )
        if not valid:
            self._send_json(handler, 400, {"error": "invalid_request"})
            return
        if self.force_authorize_error:
            self._redirect(
                handler,
                redirect_uri,
                {"error": "access_denied", "state": query.get("state", "")},
            )
            return

        with self._lock:
            self._code_counter += 1
            code = f"code-{self._code_counter}"
            self._codes[code] = {
                "challenge": query["code_challenge"],
                "redirect_uri": redirect_uri,
                "client_id": query["client_id"],
                "used": False,
            }
        self._redirect(
            handler,
            redirect_uri,
            {"code": code, "state": query.get("state", "")},
        )

    def _token(self, handler: BaseHTTPRequestHandler, form: dict[str, str]) -> None:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            self._token_authorization_code(handler, form)
        elif grant == "refresh_token":
            self._token_refresh(handler, form)
        else:
            self._send_json(handler, 400, {"error": "unsupported_grant_type"})

    def _token_authorization_code(
        self, handler: BaseHTTPRequestHandler, form: dict[str, str]
    ) -> None:
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        redirect_uri = form.get("redirect_uri", "")
        client_id = form.get("client_id", "")
        with self._lock:
            record = self._codes.get(code)
            accepted = (
                record is not None
                and not record["used"]
                and record["redirect_uri"] == redirect_uri
                and record["client_id"] == client_id
                and record["challenge"] == s256(verifier)
            )
            if accepted and record is not None:
                record["used"] = True
                tokens = self._issue_tokens()
            else:
                tokens = None
        if tokens is None:
            self._send_json(handler, 400, {"error": "invalid_grant"})
            return
        self._send_json(handler, 200, tokens)

    def _token_refresh(
        self, handler: BaseHTTPRequestHandler, form: dict[str, str]
    ) -> None:
        token = form.get("refresh_token", "")
        with self._lock:
            known = token in self._refresh
            if known:
                del self._refresh[token]
                tokens = self._issue_tokens()
            else:
                tokens = None
        if tokens is None:
            self._send_json(handler, 400, {"error": "invalid_grant"})
            return
        self._send_json(handler, 200, tokens)

    def _me(self, handler: BaseHTTPRequestHandler) -> None:
        auth = handler.headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        with self._lock:
            subject = self._access.get(token) if token else None
        if subject is None:
            self._send_json(handler, 401, {"error": "invalid_token"})
            return
        self._send_json(handler, 200, {"sub": subject, "name": self.subject_name})

    # --- internals ----------------------------------------------------------

    def _issue_tokens(self) -> dict[str, Any]:
        # Caller must hold ``self._lock``.
        self._token_counter += 1
        access = f"access-{self._token_counter}"
        self._token_counter += 1
        refresh = f"refresh-{self._token_counter}"
        self._access[access] = self.subject
        self._refresh[refresh] = {"subject": self.subject}
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
        }

    def _redirect(
        self, handler: BaseHTTPRequestHandler, location_base: str, params: dict[str, str]
    ) -> None:
        parts = urlsplit(location_base)
        merged = {key: value[0] for key, value in parse_qs(parts.query).items()}
        merged.update(params)
        location = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment)
        )
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _send_json(
        self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


def _lower_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {key.lower(): value for key, value in handler.headers.items()}


def _handler_class(provider: MockOAuthProvider) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            provider._handle_get(self)

        def do_POST(self) -> None:
            provider._handle_post(self)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return _Handler


@pytest.fixture
def mock_oauth_provider() -> Iterator[MockOAuthProvider]:
    """Yield a started :class:`MockOAuthProvider` and shut it down afterwards."""
    with MockOAuthProvider() as provider:
        yield provider


__all__ = [
    "ACCESS_TOKEN_TTL",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_SUBJECT",
    "DEFAULT_SUBJECT_NAME",
    "MockOAuthProvider",
    "RecordedRequest",
    "mock_oauth_provider",
    "s256",
]
