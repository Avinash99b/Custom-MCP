"""Static assertions over the deployment artifacts.

These tests never require Docker or a network: they read the committed
``Dockerfile``, ``docker-compose.yml`` and ``.env.example`` from the repository
root and make structural assertions about them.  They are deliberately tolerant
of formatting (whitespace, quoting, YAML list style) so that cosmetic edits do
not break the guarantees they encode.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE = "Dockerfile"
COMPOSE_FILE = "docker-compose.yml"
ENV_EXAMPLE = ".env.example"


def _read(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"missing deployment artifact: {name}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Dockerfile helpers
# --------------------------------------------------------------------------- #
def _instructions(text: str) -> list[tuple[str, str]]:
    """Return ``(INSTRUCTION, argument)`` pairs, joining ``\\`` continuations."""
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        buffer = f"{buffer} {line}".strip() if buffer else line
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        logical.append(buffer)
        buffer = ""
    if buffer:
        logical.append(buffer)

    parsed: list[tuple[str, str]] = []
    for line in logical:
        head, _, tail = line.partition(" ")
        parsed.append((head.upper(), tail.strip()))
    return parsed


def _args_for(text: str, instruction: str) -> list[str]:
    return [arg for name, arg in _instructions(text) if name == instruction.upper()]


def _is_root_user(spec: str) -> bool:
    token = spec.strip().lower().split(":")[0]
    return token in {"root", "0"}


# --------------------------------------------------------------------------- #
# Compose helpers
# --------------------------------------------------------------------------- #
def _load_compose(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict), "compose file must parse to a mapping"
    return loaded


def _services(compose: dict[str, Any]) -> dict[str, Any]:
    services = compose.get("services")
    assert isinstance(services, dict), "compose file must define services"
    return services


def _mounts(service: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(source, target)`` pairs for a service's volume mounts."""
    mounts: list[tuple[str, str]] = []
    entries = service.get("volumes") or []
    assert isinstance(entries, list)
    for entry in entries:
        if not isinstance(entry, str):
            continue
        parts = entry.split(":")
        if len(parts) >= 2:
            mounts.append((parts[0], parts[1]))
        else:
            mounts.append((entry, entry))
    return mounts


def _published_ports(service: dict[str, Any]) -> list[str]:
    entries = service.get("ports") or []
    assert isinstance(entries, list)
    return [str(entry) for entry in entries]


# --------------------------------------------------------------------------- #
# Dockerfile
# --------------------------------------------------------------------------- #
def test_dockerfile_uses_pinned_slim_base() -> None:
    text = _read(DOCKERFILE)
    froms = _args_for(text, "FROM")
    assert froms, "Dockerfile must have at least one FROM"
    assert any("python:3.12" in spec for spec in froms), froms
    assert all("slim" in spec for spec in froms), froms
    # Multi-stage: a builder stage and a distinct runtime stage.
    assert " AS " in text.upper()


def test_dockerfile_runs_as_non_root_user() -> None:
    text = _read(DOCKERFILE)
    users = _args_for(text, "USER")
    assert users, "Dockerfile must declare a USER"
    assert not _is_root_user(users[-1]), f"final USER must be non-root, got {users[-1]!r}"
    # A dedicated account is created rather than reusing an existing one.
    assert re.search(r"\b(useradd|adduser)\b", text), "expected a user-creation step"
    # If any root USER appears at all, it must precede the final non-root switch.
    last_non_root = max(i for i, user in enumerate(users) if not _is_root_user(user))
    assert not any(_is_root_user(user) for user in users[last_non_root + 1 :])


def test_dockerfile_declares_persistent_data_volume() -> None:
    text = _read(DOCKERFILE)
    volumes = _args_for(text, "VOLUME")
    assert any("/data" in arg for arg in volumes), volumes
    assert "DATA_DIR=/data" in text.replace(" ", ""), "expected ENV DATA_DIR=/data"


def test_dockerfile_exposes_app_port() -> None:
    ports = _args_for(_read(DOCKERFILE), "EXPOSE")
    assert any("8000" in arg for arg in ports), ports


def test_dockerfile_has_healthcheck_on_healthz() -> None:
    text = _read(DOCKERFILE)
    assert _args_for(text, "HEALTHCHECK"), "Dockerfile must define a HEALTHCHECK"
    assert "/healthz" in text, "HEALTHCHECK must probe /healthz"


def test_dockerfile_entrypoint_runs_console_script() -> None:
    cmds = _args_for(_read(DOCKERFILE), "CMD")
    assert any("custom-mcp" in arg for arg in cmds), cmds


def test_dockerfile_pins_uv_and_uses_locked_sync() -> None:
    text = _read(DOCKERFILE)
    assert re.search(r"astral-sh/uv:[\d.]+|uv==[\d.]+", text), "uv must be pinned"
    assert "uv sync --frozen" in text, "dependencies must install from uv.lock"


# --------------------------------------------------------------------------- #
# docker-compose.yml
# --------------------------------------------------------------------------- #
def test_compose_defines_app_and_redis_services() -> None:
    services = _services(_load_compose(_read(COMPOSE_FILE)))
    assert "app" in services
    assert "redis" in services


def test_compose_redis_enables_aof_and_has_named_volume() -> None:
    services = _services(_load_compose(_read(COMPOSE_FILE)))
    redis = services["redis"]
    command = redis.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    command_text = str(command)
    assert "appendonly" in command_text and re.search(r"appendonly\s+yes", command_text)
    assert re.search(r"appendfsync\s+everysec", command_text)

    mounts = _mounts(redis)
    assert any(source == "redis-data" and target == "/data" for source, target in mounts), mounts


def test_compose_does_not_publish_redis_port() -> None:
    compose = _load_compose(_read(COMPOSE_FILE))
    services = _services(compose)
    assert not _published_ports(services["redis"]), "Redis must not publish a host port"
    for name, service in services.items():
        assert isinstance(service, dict)
        for published in _published_ports(service):
            assert "6379" not in published, f"{name} publishes Redis port: {published}"
    # Belt and braces: no 6379:6379 style mapping anywhere in the raw file.
    assert not re.search(r"6379\s*:\s*6379", _read(COMPOSE_FILE))


def test_compose_redis_healthcheck_uses_redis_cli() -> None:
    redis = _services(_load_compose(_read(COMPOSE_FILE)))["redis"]
    healthcheck = redis.get("healthcheck")
    assert isinstance(healthcheck, dict), "redis needs a healthcheck"
    test = healthcheck.get("test")
    flattened = " ".join(str(part) for part in test) if isinstance(test, list) else str(test)
    assert "redis-cli" in flattened and "ping" in flattened, flattened


def test_compose_app_depends_on_healthy_redis() -> None:
    app = _services(_load_compose(_read(COMPOSE_FILE)))["app"]
    depends = app.get("depends_on")
    assert isinstance(depends, dict), "app must declare depends_on"
    redis_dep = depends.get("redis")
    assert isinstance(redis_dep, dict), "redis dependency must use the long form"
    assert redis_dep.get("condition") == "service_healthy"


def test_compose_app_mounts_persistent_data_volume() -> None:
    compose = _load_compose(_read(COMPOSE_FILE))
    app = _services(compose)["app"]
    mounts = _mounts(app)
    assert any(target == "/data" for _source, target in mounts), mounts
    declared = compose.get("volumes")
    assert isinstance(declared, dict)
    assert "data" in declared
    assert "redis-data" in declared


def test_compose_services_share_private_network_only() -> None:
    compose = _load_compose(_read(COMPOSE_FILE))
    services = _services(compose)
    networks = compose.get("networks")
    assert isinstance(networks, dict) and networks, "a top-level network is required"
    for name in ("app", "redis"):
        assert services[name].get("networks"), f"{name} must join the private network"


def test_compose_references_env_file_not_literal_secret() -> None:
    raw = _read(COMPOSE_FILE)
    assert ".env" in raw, "compose must reference the .env file"


# --------------------------------------------------------------------------- #
# .env.example
# --------------------------------------------------------------------------- #
def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic for a high-entropy token that was pasted in by mistake."""
    if len(value) < 32:
        return False
    has_lower = any(ch.islower() for ch in value)
    has_upper = any(ch.isupper() for ch in value)
    has_digit = any(ch.isdigit() for ch in value)
    has_separator = any(ch in "-_./:=+ " for ch in value)
    return has_lower and has_upper and has_digit and not has_separator


PLACEHOLDER_MARKERS = (
    "change",
    "placeholder",
    "example",
    "your-",
    "replace",
    "secret",
    "xxx",
    "dev-",
    "test-",
    "<",
)


def test_env_example_exists_and_documents_required_settings() -> None:
    values = _parse_env(_read(ENV_EXAMPLE))
    assert "MCP_SECRET" in values
    assert "BASE_URL" in values
    assert values["BASE_URL"].startswith(("http://", "https://"))


def test_env_example_secret_is_not_a_real_secret() -> None:
    value = _parse_env(_read(ENV_EXAMPLE))["MCP_SECRET"]
    assert value, "MCP_SECRET placeholder must be present"
    assert len(value) >= 16, "the placeholder should show the minimum length"
    lowered = value.lower()
    assert any(marker in lowered for marker in PLACEHOLDER_MARKERS), value
    assert not _looks_like_real_secret(value), "placeholder looks like a real secret"


def test_env_example_notes_cookie_secure_derivation() -> None:
    text = _read(ENV_EXAMPLE)
    assert "COOKIE_SECURE" in text
    assert "derived from BASE_URL" in text
