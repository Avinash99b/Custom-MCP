"""Phase 8 OAuth credential-containment regressions (gate round 1).

The Phase 8 gate critic reproduced four redaction defects. Each test here drives
the assembled app with a diagnostic OAuth plugin and proves the value can no
longer reach any client-visible surface:

* a secret the plugin embeds in the sign-in URL (connect JSON);
* the authorization code echoed into a log and a tool description;
* the PKCE verifier (kept in plugin data ``pkce:{state}``) echoed into a log;
* derived tokens cached in memory across a refresh rotation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import httpx
import pytest_asyncio
from starlette.applications import Starlette

from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_plugin_zip

AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
CLIENT_ID = "diag-client-id"
CLIENT_SECRET = "diag/secret+abc123"
CODE = "diag-authcode-abcdef123456"

_VERIFIER = "diag-verifier-eeeeeeeeeeeeeeeeeeeeeeeeeeee"
_ACCESS1 = "diag-access-aaaaaaaa"
_REFRESH1 = "diag-refresh-bbbbbbbb"
_ACCESS2 = "diag-access-cccccccc"
_REFRESH2 = "diag-refresh-dddddddd"

_F1_SOURCE = '''\
from urllib.parse import quote, quote_plus

from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class DiagPlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.OAUTH]

    def list_tools(self):
        return []

    def connect(self):
        secret = str(self.ctx.secrets.get("core.oauth_client_secret"))
        client_id = str(self.ctx.secrets.get("core.oauth_client_id"))
        return ConnectResult(
            status=Status.OAUTH_REQUIRED,
            redirect_url="https://provider.invalid/authorize?client_id="
            + client_id
            + "&raw=" + secret
            + "&pct=" + quote(secret, safe="")
            + "&form=" + quote_plus(secret)
            + "&lower=" + quote(secret, safe="").lower(),
        )

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

# A value only a decoder can recover: the leading ``d`` is written as ``%64`` so
# a plain substring redactor misses it, but a URL decoder recovers the secret.
_F1_TRICKY_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class DiagPlugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.OAUTH]

    def list_tools(self):
        return []

    def connect(self):
        secret = str(self.ctx.secrets.get("core.oauth_client_secret"))
        return ConnectResult(
            status=Status.OAUTH_REQUIRED,
            redirect_url="https://provider.invalid/authorize?client_secret=%64"
            + secret[1:],
        )

    def ping(self):
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''

_F234_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, Tool, ToolResult

ACCESS1 = "diag-access-aaaaaaaa"
REFRESH1 = "diag-refresh-bbbbbbbb"
ACCESS2 = "diag-access-cccccccc"
REFRESH2 = "diag-refresh-dddddddd"
VERIFIER = "diag-verifier-eeeeeeeeeeeeeeeeeeeeeeeeeeee"


class DiagPlugin(MCPPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.cached = []

    def auth_methods(self):
        return [AuthMode.OAUTH]

    def list_tools(self):
        return [Tool(
            name="whoami",
            description="diagnostic " + " ".join(self.cached),
            input_schema={"type": "object", "properties": {}},
        )]

    def connect(self):
        return ConnectResult(status=Status.OAUTH_REQUIRED)

    def get_oauth_sign_in_url(self, state):
        self.ctx.storage.set("pkce:" + state, VERIFIER, ttl=600)
        self.ctx.log("info", "sign-in verifier=" + VERIFIER)
        return "https://provider.invalid/authorize?state=" + state

    def on_oauth_result(self, code, state):
        self.ctx.log("info", "callback code=" + code)
        self.ctx.secrets.set("oauth_access_token", ACCESS1)
        self.ctx.secrets.set("oauth_refresh_token", REFRESH1)
        self.ctx.secrets.set("oauth_expires_at", 0)
        self.cached = [code, ACCESS1, REFRESH1]
        return ConnectResult(status=Status.CONNECTED)

    def ping(self):
        if self.ctx.secrets.get("oauth_expires_at") == 0:
            return Status.AUTH_EXPIRED
        return Status.CONNECTED

    def refresh(self):
        self.ctx.secrets.set("oauth_access_token", ACCESS2)
        self.ctx.secrets.set("oauth_refresh_token", REFRESH2)
        self.ctx.secrets.set("oauth_expires_at", 1)
        self.cached.extend([ACCESS2, REFRESH2])
        return Status.CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.success({})
'''


def _package(slug: str, source: str) -> bytes:
    return build_plugin_zip(
        slug=slug,
        entry="plugin:DiagPlugin",
        auth_modes=("oauth",),
        plugin_source=source,
    )


@pytest_asyncio.fixture
async def client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=TEST_BASE_URL,
            timeout=httpx.Timeout(30.0),
        ) as http:
            yield http


async def _install(client: httpx.AsyncClient, slug: str, source: str) -> None:
    response = await client.post(
        "/api/plugins/import",
        files={"file": ("plugin.zip", _package(slug, source), "application/zip")},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text


async def _connect(client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/plugins/{slug}/connect",
        json={"mode": "oauth", "credentials": {"client_id": CLIENT_ID}},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _detail(client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    response = await client.get(f"/api/plugins/{slug}", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def _state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def test_connect_redacts_varied_secret_encodings(
    client: httpx.AsyncClient,
) -> None:
    slug = "diag-a"
    await _install(client, slug, _F1_SOURCE)
    response = await client.post(
        f"/api/plugins/{slug}/connect",
        json={
            "mode": "oauth",
            "credentials": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The URL is still returned (the flow continues) but no encoding of the
    # secret survives; the public client id is preserved.
    assert body["status"] == "oauth_required"
    redirect_url = body["redirect_url"]
    assert redirect_url.startswith("https://provider.invalid/authorize")
    assert CLIENT_ID in redirect_url
    for encoding in (
        CLIENT_SECRET,
        quote(CLIENT_SECRET, safe=""),
        quote_plus(CLIENT_SECRET),
        quote(CLIENT_SECRET, safe="").lower(),
    ):
        assert encoding not in response.text
    assert redirect_url.count("***") >= 4


async def test_connect_refuses_an_uncatchable_secret_encoding(
    client: httpx.AsyncClient,
) -> None:
    slug = "diag-f"
    await _install(client, slug, _F1_TRICKY_SOURCE)
    response = await client.post(
        f"/api/plugins/{slug}/connect",
        json={
            "mode": "oauth",
            "credentials": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # A URL a decoder can recover the secret from is refused, not returned.
    assert body["status"] == "error"
    assert "redirect_url" not in body
    assert CLIENT_SECRET not in response.text


async def test_pkce_verifier_is_redacted_from_logs(
    client: httpx.AsyncClient, app: Starlette
) -> None:
    slug = "diag-b"
    await _install(client, slug, _F234_SOURCE)
    started = await _connect(client, slug)
    # Control: a credential-free sign-in URL is still returned and usable.
    assert started["status"] == "oauth_required"
    assert started["redirect_url"].startswith("https://provider.invalid/authorize")

    logs = await app.state.plugin_manager.logs(slug)
    assert _VERIFIER not in json.dumps(logs)
    # The verifier is stored in data for the policy, but must not surface.
    rendered = json.dumps({"logs": logs, "detail": await _detail(client, slug)})
    assert _VERIFIER not in rendered


async def test_authorization_code_is_redacted(
    client: httpx.AsyncClient, app: Starlette
) -> None:
    slug = "diag-c"
    await _install(client, slug, _F234_SOURCE)
    started = await _connect(client, slug)
    state = _state_of(started["redirect_url"])

    callback = await client.get(
        f"/mcp/{slug}/oauth/callback",
        params={"code": CODE, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/#/plugins/{slug}?oauth=success"

    detail = await _detail(client, slug)
    logs = await app.state.plugin_manager.logs(slug)
    rendered = json.dumps({"detail": detail, "logs": logs})
    assert CODE not in rendered
    assert _ACCESS1 not in rendered
    assert _REFRESH1 not in rendered
    assert all(CODE not in str(tool.get("description")) for tool in detail["tools"])


async def test_refresh_rotation_keeps_cached_tokens_redacted(
    client: httpx.AsyncClient, app: Starlette
) -> None:
    slug = "diag-d"
    await _install(client, slug, _F234_SOURCE)
    started = await _connect(client, slug)
    state = _state_of(started["redirect_url"])
    done = await client.get(
        f"/mcp/{slug}/oauth/callback",
        params={"code": CODE, "state": state},
        follow_redirects=False,
    )
    assert done.headers["location"] == f"/#/plugins/{slug}?oauth=success"

    # The derived tokens are retained the moment they are stored, so a later
    # snapshot failure cannot lose their redaction history.
    redaction = await app.state.plugin_logs.redaction_strings(slug)
    assert _ACCESS1 in redaction
    assert _REFRESH1 in redaction

    # A real scheduled cycle: the plugin reports auth_expired, so the scheduler
    # rotates the tokens under its lifecycle lock.
    await app.state.health.run_cycle()
    assert (await _detail(client, slug))["status"] == "connected"

    # Disconnecting forces a fresh description that echoes the cached tokens.
    disconnected = await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
    assert disconnected.status_code == 200, disconnected.text
    rendered = json.dumps(
        {
            "disconnect": disconnected.json(),
            "detail": await _detail(client, slug),
            "logs": await app.state.plugin_manager.logs(slug),
        }
    )
    # Both token generations are gone from storage but must stay redacted
    # because the generation cached them in memory.
    for secret in (_ACCESS1, _REFRESH1, _ACCESS2, _REFRESH2):
        assert secret not in rendered


async def test_failed_snapshot_disconnect_does_not_re_expose_tokens(
    client: httpx.AsyncClient, app: Starlette, monkeypatch: Any
) -> None:
    slug = "diag-e"
    await _install(client, slug, _F234_SOURCE)
    started = await _connect(client, slug)
    state = _state_of(started["redirect_url"])
    done = await client.get(
        f"/mcp/{slug}/oauth/callback",
        params={"code": CODE, "state": state},
        follow_redirects=False,
    )
    assert done.headers["location"] == f"/#/plugins/{slug}?oauth=success"

    # Refresh first so freshly issued tokens exist, then make the first
    # disconnect's pre-wipe snapshot read fail.
    await app.state.health.run_cycle()

    logs = app.state.plugin_logs
    original = logs.secret_strings

    async def _fail(_slug: str) -> tuple[str, ...]:
        raise RuntimeError("snapshot read failed")

    monkeypatch.setattr(logs, "secret_strings", _fail)
    first = await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
    assert first.status_code == 200, first.text
    assert _ACCESS1 not in json.dumps(first.json())
    assert _REFRESH1 not in json.dumps(first.json())

    # Restore the snapshot read; the second disconnect must still not re-expose
    # the tokens (pre- or post-refresh) the plugin cached in memory.
    monkeypatch.setattr(logs, "secret_strings", original)
    second = await client.post(f"/api/plugins/{slug}/disconnect", headers=AUTH)
    assert second.status_code == 200, second.text
    rendered = json.dumps(
        {
            "second": second.json(),
            "detail": await _detail(client, slug),
            "logs": await logs.recent(slug),
        }
    )
    for secret in (_ACCESS1, _REFRESH1, _ACCESS2, _REFRESH2):
        assert secret not in rendered
