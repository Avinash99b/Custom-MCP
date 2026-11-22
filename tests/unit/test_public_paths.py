"""Exact public-route matching."""

from __future__ import annotations

import pytest

from custom_mcp.auth.middleware import is_public_path


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/login",
        "/static",
        "/static/app.js",
        "/static/styles.css",
        "/mcp/demo/oauth/callback",
        "/mcp/a/oauth/callback",
        f"/mcp/{'a' * 40}/oauth/callback",
    ],
)
def test_public_paths(path: str) -> None:
    assert is_public_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/session",
        "/api/anything",
        "/logout",
        "/mcp",
        "/mcp/demo",
        "/mcp/demo/",
        "/mcp/demo/tools",
        "/mcp/DEMO/oauth/callback",
        "/mcp/demo/oauth/callback/extra",
        "/mcp/demo/oauth/other",
        f"/mcp/{'a' * 41}/oauth/callback",
        "/mcp/-bad/oauth/callback",
        "/staticx",
        "/static2/app.js",
        "/loginx",
        "/healthz/extra",
        "/healthz/",
    ],
)
def test_protected_paths(path: str) -> None:
    assert is_public_path(path) is False
