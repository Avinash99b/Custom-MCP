"""Integration tests for the served dashboard shell and its static assets."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest_asyncio
from starlette.applications import Starlette

from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_plugin_zip
from tests.unit.test_dashboard_source import REQUIRED_IDS

JSON_HEADERS = {"Accept": "application/json"}
HTML_HEADERS = {"Accept": "text/html"}

_HANDLER_ATTR = re.compile(r"\son[a-z]+\s*=")

#: Minimal plugin that advertises only ``api_key`` so the manifest and the
#: loaded plugin agree on their auth modes.
API_KEY_PLUGIN_SOURCE = '''\
from mcp_plugin_sdk import AuthMode, ConnectResult, MCPPlugin, Status, ToolResult


class Plugin(MCPPlugin):
    def auth_methods(self):
        return [AuthMode.API_KEY]

    def list_tools(self):
        return []

    def connect(self):
        return ConnectResult(status=Status.NOT_CONNECTED)

    def ping(self):
        return Status.NOT_CONNECTED

    def invoke_tool(self, name, args):
        return ToolResult.failure("unknown_tool", "unknown tool")
'''


@asynccontextmanager
async def _running(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=TEST_BASE_URL
        ) as http:
            yield http


@pytest_asyncio.fixture
async def running_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with _running(app) as http:
        yield http


async def _login(
    client: httpx.AsyncClient, secret: str = TEST_SECRET
) -> httpx.Response:
    return await client.post("/login", json={"secret": secret}, headers=JSON_HEADERS)


# --- shell -------------------------------------------------------------------


async def test_browser_get_redirects_unauthenticated(client: httpx.AsyncClient) -> None:
    response = await client.get("/", headers=HTML_HEADERS)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_dashboard_served_after_login(client: httpx.AsyncClient) -> None:
    assert (await _login(client)).status_code == 200
    response = await client.get("/", headers=HTML_HEADERS)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<script src="/static/app.js"' in response.text
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in response.text, element_id


async def test_dashboard_html_has_no_inline_assets_or_handlers(
    client: httpx.AsyncClient,
) -> None:
    assert (await _login(client)).status_code == 200
    response = await client.get("/", headers=HTML_HEADERS)
    assert response.status_code == 200
    html = response.text
    assert "<script>" not in html
    assert "<style" not in html
    assert _HANDLER_ATTR.search(html) is None


async def test_dashboard_settings_exposes_backup_and_restore(
    client: httpx.AsyncClient,
) -> None:
    assert (await _login(client)).status_code == 200
    response = await client.get("/", headers=HTML_HEADERS)
    assert response.status_code == 200
    html = response.text
    assert 'id="settings-section"' in html
    assert 'id="backup-plugins"' in html
    assert 'id="backup-download"' in html
    assert 'id="restore-file"' in html
    assert 'id="restore-mode"' in html
    assert 'id="restore-button"' in html
    assert 'id="restore-results"' in html
    assert "Backup and restore are not available yet" not in html
    # The plaintext-credentials warning must be visible to the user.
    assert "plaintext" in html.lower()


async def test_served_app_js_is_csp_clean(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/app.js")
    assert response.status_code == 200
    assert (
        response.headers["content-security-policy"]
        == "default-src 'self'; frame-ancestors 'none'"
    )
    js = response.text
    assert "/api/backup" in js
    assert "/api/restore" in js
    for token in ("innerHTML", "eval(", "localStorage", "sessionStorage"):
        assert token not in js, token


# --- static assets -----------------------------------------------------------


async def test_static_assets_are_public_with_csp(client: httpx.AsyncClient) -> None:
    for path in ("/static/app.js", "/static/styles.css"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert (
            response.headers["content-security-policy"]
            == "default-src 'self'; frame-ancestors 'none'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"

    js = await client.get("/static/app.js")
    assert "javascript" in js.headers["content-type"].lower()
    css = await client.get("/static/styles.css")
    assert "css" in css.headers["content-type"].lower()


# --- plugin summaries drive the dashboard ------------------------------------


async def test_plugin_summary_fields_after_import(
    running_client: httpx.AsyncClient,
) -> None:
    assert (await _login(running_client)).status_code == 200

    zip_bytes = build_plugin_zip(
        slug="demo",
        version="1.0.0",
        display_name="Demo Plugin",
        description="A demo plugin",
        auth_modes=("api_key",),
        plugin_source=API_KEY_PLUGIN_SOURCE,
    )
    imported = await running_client.post(
        "/api/plugins/import?mode=reject",
        files={"file": ("plugin.zip", zip_bytes, "application/zip")},
        headers={"Origin": TEST_BASE_URL},
    )
    assert imported.status_code == 201

    listing = await running_client.get("/api/plugins")
    assert listing.status_code == 200
    rows = listing.json()["plugins"]
    assert len(rows) == 1
    row = rows[0]
    for key in ("slug", "status", "load_state", "tool_names", "auth_methods"):
        assert key in row, key
    assert row["slug"] == "demo"
    assert row["load_state"] == "loaded"
    assert row["status"] == "not_connected"
    assert row["auth_methods"] == ["api_key"]
    assert row["tool_names"] == []


__all__ = ["running_client"]
