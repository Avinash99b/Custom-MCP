"""Frozen application configuration parsed once from the environment.

The settings object is intentionally immutable and validated eagerly so that a
misconfigured deployment fails fast at startup rather than at first use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_DATA_DIR = "./data"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "INFO"
MIN_SECRET_LENGTH = 16

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})
_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _parse_int(
    name: str,
    raw: str | None,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _parse_bool(name: str, raw: str, default: bool) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _parse_log_level(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return DEFAULT_LOG_LEVEL
    value = raw.strip().upper()
    if value not in _LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {raw!r}"
        )
    return value


def _parse_base_url(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        raise ConfigError("BASE_URL is required")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        raise ConfigError("BASE_URL must not contain whitespace or control characters")
    value = raw
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError("BASE_URL is not a valid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise ConfigError("BASE_URL must use the http or https scheme")
    try:
        hostname = parts.hostname
    except ValueError as exc:
        raise ConfigError("BASE_URL is not a valid URL") from exc
    if not parts.netloc or hostname is None:
        raise ConfigError("BASE_URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("BASE_URL must not contain userinfo")
    if parts.query:
        raise ConfigError("BASE_URL must not contain a query string")
    if parts.fragment:
        raise ConfigError("BASE_URL must not contain a fragment")
    if parts.path:
        raise ConfigError("BASE_URL must be a bare origin without a path or trailing slash")
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigError("BASE_URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("BASE_URL port must be between 1 and 65535")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable runtime configuration."""

    MCP_SECRET: str = field(repr=False)
    BASE_URL: str
    REDIS_URL: str
    DATA_DIR: str
    HOST: str
    PORT: int
    PING_INTERVAL_SECONDS: int
    PING_TIMEOUT_SECONDS: int
    PING_FAIL_THRESHOLD: int
    TOOL_TIMEOUT_SECONDS: int
    PLUGIN_CALL_TIMEOUT_SECONDS: int
    WORKER_THREADS: int
    PER_PLUGIN_CONCURRENCY: int
    MAX_PLUGIN_ZIP_MB: int
    MAX_BACKUP_MB: int
    SESSION_TTL_HOURS: int
    COOKIE_SECURE: bool
    LOG_LEVEL: str

    @property
    def origin(self) -> str:
        """The single allowed browser origin (scheme://host[:port])."""
        return self.BASE_URL

    @property
    def session_ttl_seconds(self) -> int:
        """Session lifetime in seconds, derived from ``SESSION_TTL_HOURS``."""
        return self.SESSION_TTL_HOURS * 3600

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build validated settings from a mapping (defaults to ``os.environ``)."""
        if env is None:
            import os

            env = os.environ

        secret = env.get("MCP_SECRET")
        if secret is None or secret.strip() == "":
            raise ConfigError("MCP_SECRET is required")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"MCP_SECRET must be at least {MIN_SECRET_LENGTH} characters"
            )

        base_url = _parse_base_url(env.get("BASE_URL"))

        raw_cookie_secure = env.get("COOKIE_SECURE")
        if raw_cookie_secure is None or raw_cookie_secure.strip() == "":
            cookie_secure = urlsplit(base_url).scheme == "https"
        else:
            cookie_secure = _parse_bool("COOKIE_SECURE", raw_cookie_secure, False)

        return cls(
            MCP_SECRET=secret,
            BASE_URL=base_url,
            REDIS_URL=env.get("REDIS_URL", DEFAULT_REDIS_URL).strip() or DEFAULT_REDIS_URL,
            DATA_DIR=env.get("DATA_DIR", DEFAULT_DATA_DIR).strip() or DEFAULT_DATA_DIR,
            HOST=env.get("HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            PORT=_parse_int("PORT", env.get("PORT"), DEFAULT_PORT, minimum=1, maximum=65535),
            PING_INTERVAL_SECONDS=_parse_int(
                "PING_INTERVAL_SECONDS", env.get("PING_INTERVAL_SECONDS"), 60, minimum=1
            ),
            PING_TIMEOUT_SECONDS=_parse_int(
                "PING_TIMEOUT_SECONDS", env.get("PING_TIMEOUT_SECONDS"), 10, minimum=1
            ),
            PING_FAIL_THRESHOLD=_parse_int(
                "PING_FAIL_THRESHOLD", env.get("PING_FAIL_THRESHOLD"), 3, minimum=1
            ),
            TOOL_TIMEOUT_SECONDS=_parse_int(
                "TOOL_TIMEOUT_SECONDS", env.get("TOOL_TIMEOUT_SECONDS"), 30, minimum=1
            ),
            PLUGIN_CALL_TIMEOUT_SECONDS=_parse_int(
                "PLUGIN_CALL_TIMEOUT_SECONDS",
                env.get("PLUGIN_CALL_TIMEOUT_SECONDS"),
                15,
                minimum=1,
            ),
            WORKER_THREADS=_parse_int(
                "WORKER_THREADS", env.get("WORKER_THREADS"), 16, minimum=1
            ),
            PER_PLUGIN_CONCURRENCY=_parse_int(
                "PER_PLUGIN_CONCURRENCY", env.get("PER_PLUGIN_CONCURRENCY"), 4, minimum=1
            ),
            MAX_PLUGIN_ZIP_MB=_parse_int(
                "MAX_PLUGIN_ZIP_MB", env.get("MAX_PLUGIN_ZIP_MB"), 20, minimum=1
            ),
            MAX_BACKUP_MB=_parse_int(
                "MAX_BACKUP_MB", env.get("MAX_BACKUP_MB"), 200, minimum=1
            ),
            SESSION_TTL_HOURS=_parse_int(
                "SESSION_TTL_HOURS", env.get("SESSION_TTL_HOURS"), 168, minimum=1
            ),
            COOKIE_SECURE=cookie_secure,
            LOG_LEVEL=_parse_log_level(env.get("LOG_LEVEL")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed from the environment exactly once."""
    return Settings.from_env()
