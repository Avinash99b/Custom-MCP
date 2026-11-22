"""Origin normalization used for CSRF comparison."""

from __future__ import annotations

import pytest

from custom_mcp.auth.middleware import normalize_origin


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://mcp.example.com", "https://mcp.example.com:443"),
        ("https://mcp.example.com:443", "https://mcp.example.com:443"),
        ("http://mcp.example.com", "http://mcp.example.com:80"),
        ("http://mcp.example.com:80", "http://mcp.example.com:80"),
        ("https://MCP.Example.COM", "https://mcp.example.com:443"),
        ("https://mcp.example.com:8443", "https://mcp.example.com:8443"),
    ],
)
def test_normalizable_origins(value: str, expected: str) -> None:
    assert normalize_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "null",
        "not a url",
        "ftp://mcp.example.com",
        "https://user:pass@mcp.example.com",
        "https://exa mple.com",
        "https://mcp.example.com\n",
        "https://[::1",
        "https://mcp.example.com:99999",
    ],
)
def test_invalid_origins(value: str) -> None:
    assert normalize_origin(value) is None
