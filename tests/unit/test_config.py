"""Configuration parsing and validation."""

from __future__ import annotations

import pytest

from custom_mcp.config import ConfigError, Settings, get_settings
from tests.helpers import TEST_BASE_URL, TEST_SECRET, build_env


def test_valid_settings_from_env() -> None:
    settings = Settings.from_env(build_env())
    assert settings.MCP_SECRET == TEST_SECRET
    assert settings.BASE_URL == TEST_BASE_URL
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.DATA_DIR == "./data"
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8000
    assert settings.PING_INTERVAL_SECONDS == 60
    assert settings.PING_TIMEOUT_SECONDS == 10
    assert settings.PING_FAIL_THRESHOLD == 3
    assert settings.TOOL_TIMEOUT_SECONDS == 30
    assert settings.PLUGIN_CALL_TIMEOUT_SECONDS == 15
    assert settings.WORKER_THREADS == 16
    assert settings.PER_PLUGIN_CONCURRENCY == 4
    assert settings.MAX_PLUGIN_ZIP_MB == 20
    assert settings.MAX_BACKUP_MB == 200
    assert settings.SESSION_TTL_HOURS == 168
    assert settings.LOG_LEVEL == "INFO"


def test_missing_secret_fails() -> None:
    with pytest.raises(ConfigError, match="MCP_SECRET is required"):
        Settings.from_env(build_env(MCP_SECRET=None))


def test_short_secret_fails() -> None:
    with pytest.raises(ConfigError, match="at least 16"):
        Settings.from_env(build_env(MCP_SECRET="short"))


def test_whitespace_only_secret_fails() -> None:
    with pytest.raises(ConfigError, match="MCP_SECRET is required"):
        Settings.from_env(build_env(MCP_SECRET=" " * 32))


def test_exactly_min_length_secret_accepted() -> None:
    settings = Settings.from_env(build_env(MCP_SECRET="a" * 16))
    assert settings.MCP_SECRET == "a" * 16


def test_secret_preserves_surrounding_whitespace() -> None:
    secret = "  abcdefghijklmnop  "
    settings = Settings.from_env(build_env(MCP_SECRET=secret))
    assert secret == settings.MCP_SECRET


def test_secret_is_redacted_in_repr() -> None:
    secret = "  abcdefghijklmnop  "
    settings = Settings.from_env(build_env(MCP_SECRET=secret))
    assert secret not in repr(settings)
    assert "MCP_SECRET" not in repr(settings)


def test_missing_base_url_fails() -> None:
    with pytest.raises(ConfigError, match="BASE_URL is required"):
        Settings.from_env(build_env(BASE_URL=None))


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("ftp://mcp.example.com", "http or https"),
        ("https://user:pass@mcp.example.com", "userinfo"),
        ("https://mcp.example.com?x=1", "query"),
        ("https://mcp.example.com#frag", "fragment"),
        ("https://mcp.example.com/", "trailing slash"),
        ("https://mcp.example.com/dashboard", "without a path"),
        ("https://", "hostname"),
        ("https://mcp.example.com:99999", "invalid port"),
        ("https://[::1", "valid URL"),
        ("https://mcp.example.com ", "whitespace"),
        ("https://mcp.example\n.com", "whitespace"),
    ],
)
def test_invalid_base_url_fails(value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        Settings.from_env(build_env(BASE_URL=value))


def test_base_url_with_port_is_origin() -> None:
    settings = Settings.from_env(build_env(BASE_URL="http://localhost:8080"))
    assert settings.BASE_URL == "http://localhost:8080"
    assert settings.origin == "http://localhost:8080"


def test_cookie_secure_defaults_true_for_https() -> None:
    settings = Settings.from_env(build_env(BASE_URL="https://mcp.example.com"))
    assert settings.COOKIE_SECURE is True


def test_cookie_secure_defaults_false_for_http() -> None:
    settings = Settings.from_env(build_env(BASE_URL="http://mcp.example.com"))
    assert settings.COOKIE_SECURE is False


def test_cookie_secure_override() -> None:
    settings = Settings.from_env(build_env(COOKIE_SECURE="false"))
    assert settings.COOKIE_SECURE is False


def test_session_ttl_seconds_derived() -> None:
    settings = Settings.from_env(build_env(SESSION_TTL_HOURS="2"))
    assert settings.session_ttl_seconds == 7200


@pytest.mark.parametrize(
    "name",
    ["PORT", "PING_INTERVAL_SECONDS", "WORKER_THREADS", "SESSION_TTL_HOURS"],
)
def test_invalid_number_fails(name: str) -> None:
    with pytest.raises(ConfigError, match="integer"):
        Settings.from_env(build_env(**{name: "not-a-number"}))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_number_fails(value: str) -> None:
    with pytest.raises(ConfigError, match=">="):
        Settings.from_env(build_env(WORKER_THREADS=value))


def test_port_out_of_range_fails() -> None:
    with pytest.raises(ConfigError, match="<="):
        Settings.from_env(build_env(PORT="70000"))


def test_invalid_boolean_fails() -> None:
    with pytest.raises(ConfigError, match="boolean"):
        Settings.from_env(build_env(COOKIE_SECURE="maybe"))


def test_invalid_log_level_fails() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(build_env(LOG_LEVEL="LOUD"))


def test_log_level_normalized() -> None:
    settings = Settings.from_env(build_env(LOG_LEVEL="debug"))
    assert settings.LOG_LEVEL == "DEBUG"


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", TEST_BASE_URL)
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
