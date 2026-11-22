"""Unit tests for the ``gmail`` connector plugin.

The example module is imported directly from ``plugin_examples/gmail``. The
OAuth sign-in/exchange tests drive it against the real loopback mock provider in
``tests/oauth_provider.py`` so the plugin's synchronous ``requests`` calls are
genuinely exercised; the Gmail API tools monkeypatch the single
``_http_request`` helper and assert on the recorded calls.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import requests

from mcp_plugin_sdk import AuthMode, PluginContext, Status
from tests.oauth_provider import MockOAuthProvider

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "plugin_examples" / "gmail"
PLUGIN_PATH = EXAMPLE_DIR / "plugin.py"
MANIFEST_PATH = EXAMPLE_DIR / "manifest.json"

CALLBACK_URL = "https://mcp.example.com/mcp/gmail/oauth/callback"
AUTHORIZE_URL_ENV = "CUSTOM_MCP_GMAIL_AUTHORIZE_URL"
TOKEN_URL_ENV = "CUSTOM_MCP_GMAIL_TOKEN_URL"
API_BASE_URL_ENV = "CUSTOM_MCP_GMAIL_API_BASE_URL"

API_BASE = "https://gmail.test"
API_ROOT = f"{API_BASE}/gmail/v1/users/me"

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


class MemoryStorage:
    """Minimal in-memory SDK ``Storage`` that also records TTLs."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.ttls: dict[str, int | None] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.values[key] = value
        self.ttls[key] = ttl

    def delete(self, key: str) -> bool:
        self.ttls.pop(key, None)
        if key in self.values:
            del self.values[key]
            return True
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.values if key.startswith(prefix))


class FakeResponse:
    """A stand-in for ``requests.Response`` used by the HTTP helper."""

    def __init__(
        self, status_code: int, payload: Any = None, *, json_raises: bool = False
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("no json body")
        return self._payload


class RecordingHttp:
    """A monkeypatched ``_http_request`` that records every call."""

    def __init__(
        self, response: FakeResponse | None = None, exc: BaseException | None = None
    ) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


@pytest.fixture(scope="module")
def example() -> Any:
    """Import the example plugin module directly from its source file."""
    spec = importlib.util.spec_from_file_location("gmail_plugin_under_test", PLUGIN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AUTHORIZE_URL_ENV, raising=False)
    monkeypatch.delenv(TOKEN_URL_ENV, raising=False)
    monkeypatch.setenv(API_BASE_URL_ENV, API_BASE)


@pytest.fixture
def mock_oauth_provider() -> Iterator[MockOAuthProvider]:
    """Start a loopback mock provider for the duration of one test."""
    with MockOAuthProvider() as provider:
        yield provider


def _make_plugin(
    module: Any, *, client_id: str | None = None, client_secret: str | None = None
) -> tuple[Any, MemoryStorage, MemoryStorage, list[tuple[str, str]]]:
    storage = MemoryStorage()
    secrets = MemoryStorage()
    if client_id is not None:
        secrets.set("core.oauth_client_id", client_id)
    if client_secret is not None:
        secrets.set("core.oauth_client_secret", client_secret)
    logs: list[tuple[str, str]] = []
    ctx = PluginContext(
        slug="gmail",
        storage=storage,
        secrets=secrets,
        log=lambda level, message: logs.append((level, message)),
        callback_url=CALLBACK_URL,
    )
    return module.GmailPlugin(ctx), storage, secrets, logs


def _connected_plugin(module: Any, token: str = "access-1") -> tuple[Any, MemoryStorage]:
    plugin, _, secrets, _ = _make_plugin(module, client_id="client-1")
    secrets.set("gmail_access_token", token)
    secrets.set("gmail_expires_at", time.time() + 3600)
    return plugin, secrets


def _point_at_provider(monkeypatch: pytest.MonkeyPatch, provider: MockOAuthProvider) -> None:
    monkeypatch.setenv(AUTHORIZE_URL_ENV, f"{provider.base_url}/authorize")
    monkeypatch.setenv(TOKEN_URL_ENV, f"{provider.base_url}/token")


def _sign_in(plugin: Any, state: str) -> tuple[str, requests.Response]:
    url = plugin.get_oauth_sign_in_url(state)
    response = requests.get(url, allow_redirects=False, timeout=5)
    return url, response


def _redirect_params(response: requests.Response) -> dict[str, str]:
    parsed = urlsplit(response.headers["Location"])
    return {key: value[0] for key, value in parse_qs(parsed.query).items()}


def _exchange(
    plugin: Any, state: str = "state-abc"
) -> tuple[Any, dict[str, str]]:
    _, response = _sign_in(plugin, state)
    params = _redirect_params(response)
    return plugin.on_oauth_result(params["code"], params["state"]), params


def _expected_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode_raw(raw: str) -> str:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding).decode("utf-8")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_is_valid_json_with_oauth_mode() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert parsed["name"] == "gmail"
    assert parsed["display_name"] == "gmail"
    assert parsed["entry"] == "plugin:GmailPlugin"
    assert parsed["auth_modes"] == ["oauth"]
    assert parsed["min_core_version"] == "0.1.0"


def test_manifest_passes_production_validator() -> None:
    from custom_mcp.plugins.manifest import parse_manifest

    manifest = parse_manifest(MANIFEST_PATH.read_bytes())
    assert manifest.name == "gmail"
    assert manifest.entry_class == "GmailPlugin"
    assert [mode.value for mode in manifest.auth_modes] == ["oauth"]


# ---------------------------------------------------------------------------
# Auth methods and tool surface
# ---------------------------------------------------------------------------


def test_auth_methods_is_oauth_only(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert plugin.auth_methods() == [AuthMode.OAUTH]


def test_tool_set_includes_compose_tools(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    names = [tool.name for tool in plugin.list_tools()]
    assert names == [
        "whoami",
        "list_labels",
        "list_messages",
        "get_message",
        "create_draft",
        "send_message",
    ]


def test_every_tool_schema_is_a_fresh_object_schema(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    first = {tool.name: tool.input_schema for tool in plugin.list_tools()}
    second = {tool.name: tool.input_schema for tool in plugin.list_tools()}
    for name, schema in first.items():
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema is not second[name]


def test_send_message_description_marks_destructive(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    tools = {tool.name: tool for tool in plugin.list_tools()}
    assert "DESTRUCTIVE" in tools["send_message"].description.upper()


# ---------------------------------------------------------------------------
# connect() / ping()
# ---------------------------------------------------------------------------


def test_connect_without_token_is_oauth_required(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert plugin.connect().status is Status.OAUTH_REQUIRED


def test_connect_with_valid_token_is_connected(example: Any) -> None:
    plugin, _ = _connected_plugin(example)
    assert plugin.connect().status is Status.CONNECTED


def test_ping_states(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert plugin.ping() is Status.NOT_CONNECTED
    plugin, secrets = _connected_plugin(example)
    assert plugin.ping() is Status.CONNECTED
    secrets.set("gmail_expires_at", time.time() - 1)
    # An expired access token without a refresh token cannot be recovered.
    assert plugin.ping() is Status.NOT_CONNECTED
    secrets.set("gmail_refresh_token", "refresh-1")
    assert plugin.ping() is Status.AUTH_EXPIRED


# ---------------------------------------------------------------------------
# get_oauth_sign_in_url / PKCE
# ---------------------------------------------------------------------------


def test_sign_in_url_shape_and_s256_challenge(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)

    url = plugin.get_oauth_sign_in_url("state-1")
    parsed = urlsplit(url)
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}

    assert parsed.path == "/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == mock_oauth_provider.client_id
    assert query["redirect_uri"] == CALLBACK_URL
    assert query["state"] == "state-1"
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["include_granted_scopes"] == "true"
    assert set(query["scope"].split(" ")) == {READONLY_SCOPE, COMPOSE_SCOPE}
    verifier = storage.get("pkce:state-1")
    assert query["code_challenge"] == _expected_challenge(verifier)


def test_sign_in_url_quotes_redirect_uri(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    url = plugin.get_oauth_sign_in_url("state-1")
    assert f"redirect_uri={quote(CALLBACK_URL, safe='')}" in urlsplit(url).query


def test_sign_in_url_stores_verifier_with_ttl_600(example: Any) -> None:
    plugin, storage, _, _ = _make_plugin(example, client_id="c")
    plugin.get_oauth_sign_in_url("state-1")
    assert storage.get("pkce:state-1") is not None
    assert storage.ttls["pkce:state-1"] == 600


def test_default_authorize_url_is_google(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    url = plugin.get_oauth_sign_in_url("state-1")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")


# ---------------------------------------------------------------------------
# on_oauth_result: the full authorization-code exchange
# ---------------------------------------------------------------------------


def test_full_authorization_code_exchange_connects(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, secrets, _ = _make_plugin(
        example, client_id=mock_oauth_provider.client_id, client_secret="shh"
    )
    result, params = _exchange(plugin)
    assert params["state"] == "state-abc"
    assert result.status is Status.CONNECTED
    assert storage.get("pkce:state-abc") is None

    access = secrets.get("gmail_access_token")
    refresh = secrets.get("gmail_refresh_token")
    assert access in mock_oauth_provider.active_access_tokens
    assert refresh in mock_oauth_provider.active_refresh_tokens

    token_request = mock_oauth_provider.requests_for("/token")[0]
    assert token_request.form["grant_type"] == "authorization_code"
    assert token_request.form["client_secret"] == "shh"
    assert token_request.form["redirect_uri"] == CALLBACK_URL


def test_exchange_stores_only_nonreserved_secret_keys(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _exchange(plugin)

    derived = set(secrets.list_keys()) - {"core.oauth_client_id"}
    assert derived == {"gmail_access_token", "gmail_refresh_token", "gmail_expires_at"}
    assert all(not key.startswith("core.") for key in derived)
    assert secrets.get("gmail_expires_at") > time.time()


def test_missing_verifier_is_auth_expired(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    assert plugin.on_oauth_result("code-1", "never-started").status is Status.AUTH_EXPIRED


def test_exchange_network_failure_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    plugin.get_oauth_sign_in_url("state-1")
    monkeypatch.setattr(
        example, "_http_request", RecordingHttp(exc=requests.Timeout("slow"))
    )
    assert plugin.on_oauth_result("code-1", "state-1").status is Status.ERROR
    assert secrets.get("gmail_access_token") is None


# ---------------------------------------------------------------------------
# refresh(): rotation
# ---------------------------------------------------------------------------


def test_refresh_rotation_writes_refresh_before_access(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(
        example, client_id=mock_oauth_provider.client_id, client_secret="shh"
    )
    _exchange(plugin)

    access_before = secrets.get("gmail_access_token")
    refresh_before = secrets.get("gmail_refresh_token")

    writes: list[str] = []
    original_set = secrets.set

    def recording_set(key: str, value: Any, ttl: int | None = None) -> None:
        writes.append(key)
        original_set(key, value, ttl)

    secrets.set = recording_set  # type: ignore[method-assign]
    assert plugin.refresh() is Status.CONNECTED

    assert writes[:2] == ["gmail_refresh_token", "gmail_access_token"]
    access_after = secrets.get("gmail_access_token")
    refresh_after = secrets.get("gmail_refresh_token")
    assert access_after != access_before
    assert refresh_after != refresh_before
    assert access_after in mock_oauth_provider.active_access_tokens
    assert refresh_before not in mock_oauth_provider.active_refresh_tokens

    refresh_request = mock_oauth_provider.requests_for("/token")[1]
    assert refresh_request.form["grant_type"] == "refresh_token"
    assert refresh_request.form["client_secret"] == "shh"


def test_refresh_without_refresh_token_is_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTP call expected without a refresh token")

    monkeypatch.setattr(example, "_http_request", fail)
    assert plugin.refresh() is Status.AUTH_EXPIRED


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


def test_whoami_reports_presence_without_token_value(example: Any) -> None:
    plugin, secrets = _connected_plugin(example)
    access = secrets.get("gmail_access_token")
    result = plugin.invoke_tool("whoami", {})
    assert result.result == {
        "mode": "oauth",
        "connected": True,
        "token_present": True,
        "refreshable": False,
    }
    assert access not in json.dumps(result.result)


# ---------------------------------------------------------------------------
# list_labels / list_messages / get_message
# ---------------------------------------------------------------------------


def test_list_labels_hits_labels_endpoint(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(
        FakeResponse(
            200,
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system"},
                    {"id": "Label_1", "name": "Work", "type": "user"},
                ]
            },
        )
    )
    monkeypatch.setattr(example, "_http_request", http)

    result = plugin.invoke_tool("list_labels", {})
    assert result.ok is True
    assert result.result is not None
    assert result.result["labels"][1] == {
        "id": "Label_1",
        "name": "Work",
        "type": "user",
    }
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{API_ROOT}/labels"
    assert call["headers"]["Authorization"] == "Bearer access-1"
    assert call["headers"]["Accept"] == "application/json"


def test_list_messages_passes_query_and_bounded_max_results(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(
        FakeResponse(
            200,
            {
                "messages": [{"id": "m1", "threadId": "t1"}],
                "resultSizeEstimate": 1,
            },
        )
    )
    monkeypatch.setattr(example, "_http_request", http)

    result = plugin.invoke_tool(
        "list_messages", {"q": "is:unread", "maxResults": 10_000}
    )
    assert result.ok is True
    assert result.result is not None
    assert result.result["messages"] == [{"id": "m1", "threadId": "t1"}]
    call = http.calls[0]
    assert call["url"] == f"{API_ROOT}/messages"
    assert call["params"] == {"maxResults": example.MAX_RESULTS_CAP, "q": "is:unread"}


def test_list_messages_defaults_max_results(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {"messages": []}))
    monkeypatch.setattr(example, "_http_request", http)
    plugin.invoke_tool("list_messages", {})
    assert http.calls[0]["params"] == {"maxResults": example.DEFAULT_MAX_RESULTS}


def test_get_message_parses_headers(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(
        FakeResponse(
            200,
            {
                "id": "m1",
                "threadId": "t1",
                "labelIds": ["INBOX"],
                "snippet": "hello",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Hi"},
                        {"name": "From", "value": "a@example.com"},
                        {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                    ]
                },
            },
        )
    )
    monkeypatch.setattr(example, "_http_request", http)

    result = plugin.invoke_tool("get_message", {"id": "m1"})
    assert result.ok is True
    assert result.result is not None
    assert result.result["subject"] == "Hi"
    assert result.result["from"] == "a@example.com"
    assert result.result["date"].startswith("Mon, 1 Jan 2024")
    assert result.result["snippet"] == "hello"
    call = http.calls[0]
    assert call["url"] == f"{API_ROOT}/messages/m1"
    assert call["params"] == {"format": "metadata"}


def test_get_message_rejects_bad_format(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {}))
    monkeypatch.setattr(example, "_http_request", http)
    result = plugin.invoke_tool("get_message", {"id": "m1", "format": "raw"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert http.calls == []


@pytest.mark.parametrize(
    "message_id",
    [
        "../labels",
        "m1/../../labels",
        "m1?format=full",
        "m1#frag",
        "m1%2F..",
        "m 1",
        "a" * 129,
        "",
        "   ",
    ],
)
def test_get_message_rejects_ids_that_would_reshape_the_url(
    example: Any, monkeypatch: pytest.MonkeyPatch, message_id: str
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {}))
    monkeypatch.setattr(example, "_http_request", http)
    result = plugin.invoke_tool("get_message", {"id": message_id})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert http.calls == []


def test_get_message_accepts_a_real_gmail_id_shape(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {"id": "18c1f_2a-b", "payload": {}}))
    monkeypatch.setattr(example, "_http_request", http)
    result = plugin.invoke_tool("get_message", {"id": "18c1f_2a-b"})
    assert result.ok is True
    assert http.calls[0]["url"] == f"{API_ROOT}/messages/18c1f_2a-b"


def test_get_message_full_format_is_forwarded(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {"id": "m1", "payload": {}}))
    monkeypatch.setattr(example, "_http_request", http)
    plugin.invoke_tool("get_message", {"id": "m1", "format": "full"})
    assert http.calls[0]["params"] == {"format": "full"}


# ---------------------------------------------------------------------------
# create_draft / send_message
# ---------------------------------------------------------------------------


def test_create_draft_posts_decodable_raw_message(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(
        FakeResponse(200, {"id": "draft-1", "message": {"id": "msg-1"}})
    )
    monkeypatch.setattr(example, "_http_request", http)

    result = plugin.invoke_tool(
        "create_draft",
        {"to": "bob@example.com", "subject": "Hello", "body": "Body text", "cc": "c@x.com"},
    )
    assert result.ok is True
    assert result.result == {"draft_id": "draft-1", "message_id": "msg-1"}

    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{API_ROOT}/drafts"
    raw = call["json"]["message"]["raw"]
    assert "=" not in raw
    decoded = _decode_raw(raw)
    assert "To: bob@example.com" in decoded
    assert "Cc: c@x.com" in decoded
    assert "Subject: Hello" in decoded
    assert "Body text" in decoded


def test_send_message_posts_raw_message(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(
        FakeResponse(200, {"id": "msg-9", "threadId": "t9", "labelIds": ["SENT"]})
    )
    monkeypatch.setattr(example, "_http_request", http)

    result = plugin.invoke_tool(
        "send_message", {"to": "bob@example.com", "subject": "Hi", "body": "Yo"}
    )
    assert result.ok is True
    assert result.result == {
        "message_id": "msg-9",
        "threadId": "t9",
        "labelIds": ["SENT"],
    }
    call = http.calls[0]
    assert call["url"] == f"{API_ROOT}/messages/send"
    decoded = _decode_raw(call["json"]["raw"])
    assert "To: bob@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "Yo" in decoded


def test_compose_requires_recipient(example: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {}))
    monkeypatch.setattr(example, "_http_request", http)
    result = plugin.invoke_tool("create_draft", {"subject": "s", "body": "b"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert http.calls == []


def test_compose_rejects_header_injection(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    http = RecordingHttp(FakeResponse(200, {}))
    monkeypatch.setattr(example, "_http_request", http)
    result = plugin.invoke_tool(
        "send_message",
        {"to": "bob@example.com", "subject": "Hi\r\nBcc: evil@x.com", "body": "b"},
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert http.calls == []


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (401, "auth_expired", False),
        (403, "auth_expired", False),
        (404, "not_found", False),
        (429, "rate_limited", True),
        (503, "upstream_unavailable", True),
    ],
)
def test_status_mapping(
    example: Any,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    code: str,
    retryable: bool,
) -> None:
    plugin, _ = _connected_plugin(example)
    monkeypatch.setattr(
        example, "_http_request", RecordingHttp(FakeResponse(status_code))
    )
    result = plugin.invoke_tool("list_labels", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == code
    assert result.error.retryable is retryable


def test_timeout_maps_to_retryable_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    monkeypatch.setattr(
        example, "_http_request", RecordingHttp(exc=requests.Timeout("slow"))
    )
    result = plugin.invoke_tool("list_labels", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"
    assert result.error.retryable is True


def test_invalid_json_maps_to_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _connected_plugin(example)
    monkeypatch.setattr(
        example, "_http_request", RecordingHttp(FakeResponse(200, json_raises=True))
    )
    result = plugin.invoke_tool("list_labels", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"


def test_tool_without_token_is_auth_expired(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("list_labels", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "auth_expired"


def test_unknown_tool_returns_not_found(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("missing", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "not_found"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_no_token_material_in_results_or_logs(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, logs = _make_plugin(
        example, client_id=mock_oauth_provider.client_id, client_secret="client-secret"
    )
    _exchange(plugin)
    access = secrets.get("gmail_access_token")
    refresh = secrets.get("gmail_refresh_token")
    plugin.on_oauth_credentials_set(mock_oauth_provider.client_id, "client-secret")

    monkeypatch.setattr(
        example,
        "_http_request",
        RecordingHttp(FakeResponse(200, {"labels": [], "messages": []})),
    )
    results = [
        plugin.invoke_tool("whoami", {}),
        plugin.invoke_tool("list_labels", {}),
        plugin.invoke_tool("list_messages", {}),
    ]
    serialized = json.dumps(
        [
            {"result": r.result, "error": r.error.message if r.error else None}
            for r in results
        ]
    )
    for secret in (access, refresh, "client-secret"):
        assert isinstance(secret, str) and secret
        assert secret not in serialized
        assert secret not in " ".join(message for _, message in logs)

    # Only presence booleans were recorded about the credentials hook.
    hook = plugin.ctx.storage.get("oauth_credentials_hook")
    assert hook == {"stored": True, "matched": True, "has_secret": True}
    assert str(plugin.ctx.storage.values).count("client-secret") == 0
