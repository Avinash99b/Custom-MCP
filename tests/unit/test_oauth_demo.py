"""Unit tests for the ``oauth_demo`` example plugin.

The example module is imported directly from ``plugin_examples/oauth_demo``. Most
tests drive it against the real loopback mock provider in
``tests/oauth_provider.py`` so the plugin's synchronous ``requests`` calls are
genuinely exercised; token-lifecycle edge cases additionally monkeypatch the
single ``_http_request`` helper.
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
from tests.oauth_provider import DEFAULT_SUBJECT_NAME, MockOAuthProvider

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "plugin_examples" / "oauth_demo"
PLUGIN_PATH = EXAMPLE_DIR / "plugin.py"
MANIFEST_PATH = EXAMPLE_DIR / "manifest.json"

CALLBACK_URL = "https://mcp.example.com/mcp/oauth-demo/oauth/callback"
PROVIDER_URL_ENV = "CUSTOM_MCP_OAUTH_DEMO_PROVIDER_URL"


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


@pytest.fixture(scope="module")
def example() -> Any:
    """Import the example plugin module directly from its source file."""
    spec = importlib.util.spec_from_file_location(
        "oauth_demo_plugin_under_test", PLUGIN_PATH
    )
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
def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROVIDER_URL_ENV, raising=False)


@pytest.fixture
def mock_oauth_provider() -> Iterator[MockOAuthProvider]:
    """Start a loopback mock provider for the duration of one test."""
    with MockOAuthProvider() as provider:
        yield provider


def _make_plugin(
    module: Any, *, client_id: str | None = None
) -> tuple[Any, MemoryStorage, MemoryStorage, list[tuple[str, str]]]:
    storage = MemoryStorage()
    secrets = MemoryStorage()
    if client_id is not None:
        secrets.set("core.oauth_client_id", client_id)
    logs: list[tuple[str, str]] = []
    ctx = PluginContext(
        slug="oauth-demo",
        storage=storage,
        secrets=secrets,
        log=lambda level, message: logs.append((level, message)),
        callback_url=CALLBACK_URL,
    )
    return module.OAuthDemoPlugin(ctx), storage, secrets, logs


def _point_at_provider(monkeypatch: pytest.MonkeyPatch, provider: MockOAuthProvider) -> None:
    monkeypatch.setenv(PROVIDER_URL_ENV, provider.base_url)


def _sign_in(plugin: Any, state: str) -> tuple[str, requests.Response]:
    """Return the sign-in URL and the (non-followed) provider redirect."""
    url = plugin.get_oauth_sign_in_url(state)
    response = requests.get(url, allow_redirects=False, timeout=5)
    return url, response


def _redirect_params(response: requests.Response) -> dict[str, str]:
    parsed = urlsplit(response.headers["Location"])
    return {key: value[0] for key, value in parse_qs(parsed.query).items()}


def _exchange(
    plugin: Any, provider: MockOAuthProvider, state: str = "state-abc"
) -> tuple[Any, dict[str, str]]:
    """Run the authorize redirect and the code exchange; return result + params."""
    _, response = _sign_in(plugin, state)
    params = _redirect_params(response)
    return plugin.on_oauth_result(params["code"], params["state"]), params


def _expected_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    response: FakeResponse | None = None,
    exc: BaseException | None = None,
) -> None:
    def fake(
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> FakeResponse:
        del method, url, data, headers, timeout
        if exc is not None:
            raise exc
        assert response is not None
        return response

    monkeypatch.setattr(module, "_http_request", fake)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_is_valid_json_with_oauth_mode() -> None:
    parsed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert parsed["name"] == "oauth-demo"
    assert parsed["display_name"] == "oauth_demo"
    assert parsed["entry"] == "plugin:OAuthDemoPlugin"
    assert parsed["auth_modes"] == ["oauth"]


def test_manifest_passes_production_validator() -> None:
    from custom_mcp.plugins.manifest import parse_manifest

    manifest = parse_manifest(MANIFEST_PATH.read_bytes())
    assert manifest.name == "oauth-demo"
    assert manifest.display_name == "oauth_demo"
    assert manifest.entry_class == "OAuthDemoPlugin"
    assert [mode.value for mode in manifest.auth_modes] == ["oauth"]


# ---------------------------------------------------------------------------
# Auth methods and the changed tool set
# ---------------------------------------------------------------------------


def test_auth_methods_is_oauth_only(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert plugin.auth_methods() == [AuthMode.OAUTH]


def test_list_tools_without_token_has_only_whoami(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    assert [tool.name for tool in plugin.list_tools()] == ["whoami"]


def test_list_tools_with_token_adds_me(example: Any) -> None:
    plugin, _, secrets, _ = _make_plugin(example)
    secrets.set("oauth_access_token", "access-1")
    assert [tool.name for tool in plugin.list_tools()] == ["whoami", "me"]


def test_tool_set_changes_once_token_is_stored(example: Any) -> None:
    plugin, _, secrets, _ = _make_plugin(example)
    assert [tool.name for tool in plugin.list_tools()] == ["whoami"]
    secrets.set("oauth_access_token", "access-1")
    assert [tool.name for tool in plugin.list_tools()] == ["whoami", "me"]


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_without_token_is_oauth_required(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.connect()
    assert result.status is Status.OAUTH_REQUIRED


def test_connect_with_valid_token_is_connected(example: Any) -> None:
    plugin, _, secrets, _ = _make_plugin(example)
    secrets.set("oauth_access_token", "access-1")
    secrets.set("oauth_expires_at", time.time() + 3600)
    assert plugin.connect().status is Status.CONNECTED


def test_connect_with_expired_token_is_oauth_required(example: Any) -> None:
    plugin, _, secrets, _ = _make_plugin(example)
    secrets.set("oauth_access_token", "access-1")
    secrets.set("oauth_expires_at", time.time() - 1)
    assert plugin.connect().status is Status.OAUTH_REQUIRED


# ---------------------------------------------------------------------------
# on_oauth_credentials_set observes the core-stored client id
# ---------------------------------------------------------------------------


def test_on_oauth_credentials_set_observes_core_client_id(example: Any) -> None:
    plugin, storage, _, _ = _make_plugin(example, client_id="demo-client")
    plugin.on_oauth_credentials_set("demo-client", None)
    assert storage.get("oauth_credentials_hook") == {
        "stored": True,
        "matched": True,
        "has_secret": False,
    }


def test_on_oauth_credentials_set_records_mismatch_without_raising(example: Any) -> None:
    plugin, storage, _, _ = _make_plugin(example, client_id="stored-client")
    plugin.on_oauth_credentials_set("different-client", "secret")
    assert storage.get("oauth_credentials_hook") == {
        "stored": True,
        "matched": False,
        "has_secret": True,
    }


# ---------------------------------------------------------------------------
# get_oauth_sign_in_url / PKCE
# ---------------------------------------------------------------------------


def test_sign_in_url_shape_and_s256_challenge(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)

    url, _ = _sign_in(plugin, "state-1")
    parsed = urlsplit(url)
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}

    assert parsed.scheme in ("http", "https")
    assert parsed.path == "/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == mock_oauth_provider.client_id
    assert query["state"] == "state-1"
    assert query["redirect_uri"] == CALLBACK_URL
    assert query["code_challenge_method"] == "S256"
    verifier = storage.get("pkce:state-1")
    assert query["code_challenge"] == _expected_challenge(verifier)


def test_sign_in_url_quotes_redirect_uri(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    url, _ = _sign_in(plugin, "state-1")
    assert f"redirect_uri={quote(CALLBACK_URL, safe='')}" in urlsplit(url).query


def test_sign_in_url_stores_verifier_with_ttl_600(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    plugin.get_oauth_sign_in_url("state-1")
    assert storage.get("pkce:state-1") is not None
    assert storage.ttls["pkce:state-1"] == 600


def test_pkce_verifier_is_within_rfc_length(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    plugin.get_oauth_sign_in_url("state-1")
    verifier = storage.get("pkce:state-1")
    assert isinstance(verifier, str)
    assert 43 <= len(verifier) <= 128


def test_provider_base_url_comes_from_environment(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROVIDER_URL_ENV, "https://upstream.test/")
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    url = plugin.get_oauth_sign_in_url("state-1")
    assert url.startswith("https://upstream.test/authorize?")


# ---------------------------------------------------------------------------
# on_oauth_result: the full authorization-code exchange
# ---------------------------------------------------------------------------


def test_full_authorization_code_exchange_connects(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, secrets, _ = _make_plugin(
        example, client_id=mock_oauth_provider.client_id
    )
    result, params = _exchange(plugin, mock_oauth_provider)
    assert params["state"] == "state-abc"
    assert result.status is Status.CONNECTED
    assert storage.get("pkce:state-abc") is None

    access = secrets.get("oauth_access_token")
    refresh = secrets.get("oauth_refresh_token")
    assert access in mock_oauth_provider.active_access_tokens
    assert refresh in mock_oauth_provider.active_refresh_tokens


def test_exchange_stores_nonreserved_secret_keys(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _exchange(plugin, mock_oauth_provider)

    keys = set(secrets.list_keys())
    # Only the core-seeded client id is reserved; everything the plugin derives
    # is a non-reserved secret key.
    derived = keys - {"core.oauth_client_id"}
    assert derived == {"oauth_access_token", "oauth_refresh_token", "oauth_expires_at"}
    assert all(not key.startswith("core.") for key in derived)
    assert secrets.get("oauth_expires_at") > time.time()
    assert secrets.get("oauth_access_token") != secrets.get("oauth_refresh_token")


def test_exchange_consumes_a_one_time_code(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _, response = _sign_in(plugin, "state-1")
    params = _redirect_params(response)
    verifier = storage.get("pkce:state-1")

    first = plugin.on_oauth_result(params["code"], params["state"])
    assert first.status is Status.CONNECTED

    # Replay the same code with the same verifier: the provider rejects it.
    storage.set("pkce:state-1", verifier, ttl=600)
    second = plugin.on_oauth_result(params["code"], params["state"])
    assert second.status is Status.AUTH_EXPIRED
    assert len(mock_oauth_provider.requests_for("/token")) == 2


def test_wrong_verifier_is_rejected_as_auth_expired(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, storage, secrets, _ = _make_plugin(
        example, client_id=mock_oauth_provider.client_id
    )
    _, response = _sign_in(plugin, "state-1")
    params = _redirect_params(response)

    storage.set("pkce:state-1", "x" * 43, ttl=600)
    result = plugin.on_oauth_result(params["code"], params["state"])
    assert result.status is Status.AUTH_EXPIRED
    assert secrets.get("oauth_access_token") is None


def test_missing_verifier_is_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    result = plugin.on_oauth_result("code-1", "never-started")
    assert result.status is Status.AUTH_EXPIRED


def test_exchange_network_failure_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    plugin.get_oauth_sign_in_url("state-1")
    _install_http(monkeypatch, example, exc=requests.Timeout("slow"))
    result = plugin.on_oauth_result("code-1", "state-1")
    assert result.status is Status.ERROR
    assert secrets.get("oauth_access_token") is None


def test_exchange_invalid_token_body_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")
    plugin.get_oauth_sign_in_url("state-1")
    _install_http(monkeypatch, example, response=FakeResponse(200, {"tok": "x"}))
    assert plugin.on_oauth_result("code-1", "state-1").status is Status.ERROR


# ---------------------------------------------------------------------------
# Mock provider behaviour
# ---------------------------------------------------------------------------


def test_provider_forced_error_redirects_access_denied(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    mock_oauth_provider.force_authorize_error = True
    plugin, _, _, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _, response = _sign_in(plugin, "state-1")
    assert response.status_code == 302
    params = _redirect_params(response)
    assert params["error"] == "access_denied"
    assert params["state"] == "state-1"


def test_provider_rejects_invalid_authorize_request(
    mock_oauth_provider: MockOAuthProvider,
) -> None:
    response = requests.get(
        mock_oauth_provider.url("/authorize"),
        params={
            "response_type": "code",
            "client_id": mock_oauth_provider.client_id,
            "redirect_uri": CALLBACK_URL,
            "state": "s",
        },
        allow_redirects=False,
        timeout=5,
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# refresh(): rotation
# ---------------------------------------------------------------------------


def test_refresh_rotation_returns_connected_and_rotates(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _exchange(plugin, mock_oauth_provider)

    access_before = secrets.get("oauth_access_token")
    refresh_before = secrets.get("oauth_refresh_token")
    assert plugin.refresh() is Status.CONNECTED

    access_after = secrets.get("oauth_access_token")
    refresh_after = secrets.get("oauth_refresh_token")
    assert access_after != access_before
    assert refresh_after != refresh_before
    assert access_after in mock_oauth_provider.active_access_tokens
    assert refresh_after in mock_oauth_provider.active_refresh_tokens
    assert refresh_before not in mock_oauth_provider.active_refresh_tokens
    assert len(mock_oauth_provider.requests_for("/token")) == 2


def test_refresh_without_refresh_token_is_auth_expired(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _, _ = _make_plugin(example, client_id="c")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTP call expected without a refresh token")

    monkeypatch.setattr(example, "_http_request", fail)
    assert plugin.refresh() is Status.AUTH_EXPIRED


def test_refresh_with_rejected_token_is_auth_expired(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    secrets.set("oauth_refresh_token", "not-a-real-refresh-token")
    assert plugin.refresh() is Status.AUTH_EXPIRED


def test_refresh_network_failure_is_error(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    secrets.set("oauth_refresh_token", "refresh-1")
    _install_http(monkeypatch, example, exc=requests.Timeout("slow"))
    assert plugin.refresh() is Status.ERROR


# ---------------------------------------------------------------------------
# invoke_tool: whoami
# ---------------------------------------------------------------------------


def test_whoami_reports_presence_without_token_value(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _exchange(plugin, mock_oauth_provider)
    access = secrets.get("oauth_access_token")
    refresh = secrets.get("oauth_refresh_token")

    result = plugin.invoke_tool("whoami", {})
    assert result.ok is True
    assert result.result is not None
    assert result.result["mode"] == "oauth"
    assert result.result["token_present"] is True
    assert result.result["refreshable"] is True
    serialized = json.dumps(result.result)
    assert access not in serialized
    assert refresh not in serialized


def test_whoami_without_token(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("whoami", {})
    assert result.result == {
        "mode": "oauth",
        "connected": False,
        "token_present": False,
        "refreshable": False,
    }


# ---------------------------------------------------------------------------
# invoke_tool: me
# ---------------------------------------------------------------------------


def test_me_returns_subject_without_token_value(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, _ = _make_plugin(example, client_id=mock_oauth_provider.client_id)
    _exchange(plugin, mock_oauth_provider)
    access = secrets.get("oauth_access_token")

    result = plugin.invoke_tool("me", {})
    assert result.ok is True
    assert result.result is not None
    assert result.result["name"] == DEFAULT_SUBJECT_NAME
    assert access not in json.dumps(result.result)


def test_me_without_token_is_auth_expired(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("me", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "auth_expired"


def test_me_with_401_is_auth_expired(example: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    secrets.set("oauth_access_token", "unknown-token")
    _install_http(monkeypatch, example, response=FakeResponse(401))
    result = plugin.invoke_tool("me", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "auth_expired"
    assert result.error.retryable is False


def test_me_timeout_is_retryable_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    secrets.set("oauth_access_token", "access-1")
    _install_http(monkeypatch, example, exc=requests.Timeout("slow"))
    result = plugin.invoke_tool("me", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"
    assert result.error.retryable is True


def test_me_5xx_is_retryable_upstream_unavailable(
    example: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, secrets, _ = _make_plugin(example, client_id="c")
    secrets.set("oauth_access_token", "access-1")
    _install_http(monkeypatch, example, response=FakeResponse(503))
    result = plugin.invoke_tool("me", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "upstream_unavailable"
    assert result.error.retryable is True


def test_unknown_tool_returns_not_found(example: Any) -> None:
    plugin, _, _, _ = _make_plugin(example)
    result = plugin.invoke_tool("missing", {})
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "not_found"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_no_token_material_in_any_result(
    example: Any, mock_oauth_provider: MockOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_provider(monkeypatch, mock_oauth_provider)
    plugin, _, secrets, logs = _make_plugin(
        example, client_id=mock_oauth_provider.client_id
    )
    _exchange(plugin, mock_oauth_provider)
    access = secrets.get("oauth_access_token")
    refresh = secrets.get("oauth_refresh_token")

    whoami = plugin.invoke_tool("whoami", {})
    me = plugin.invoke_tool("me", {})
    redacted = json.dumps(
        [
            whoami.result,
            me.result,
            whoami.error.message if whoami.error else None,
            me.error.message if me.error else None,
        ]
    )
    for token in (access, refresh):
        assert isinstance(token, str) and token
        assert token not in redacted
    assert access not in " ".join(message for _, message in logs)
