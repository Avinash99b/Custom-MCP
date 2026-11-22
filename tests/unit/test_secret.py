"""Unit tests for the secret authenticator and bearer parsing."""

from __future__ import annotations

import pytest

from custom_mcp.auth.base import AuthInfo, Principal, get_auth_info
from custom_mcp.auth.secret import SecretAuthenticator, extract_bearer_token
from custom_mcp.auth.sessions import SESSION_COOKIE_NAME, SessionService
from tests.helpers import make_request


@pytest.fixture
def authenticator(settings, sessions: SessionService) -> SecretAuthenticator:
    return SecretAuthenticator(settings.MCP_SECRET, sessions)


async def test_valid_bearer_yields_bearer_principal(
    authenticator: SecretAuthenticator, settings
) -> None:
    request = make_request(headers={"Authorization": f"Bearer {settings.MCP_SECRET}"})
    principal = await authenticator.authenticate(request)
    assert principal == Principal(id="owner")
    assert get_auth_info(request) == AuthInfo(method="bearer")


async def test_invalid_bearer_is_rejected(authenticator: SecretAuthenticator) -> None:
    request = make_request(headers={"Authorization": "Bearer definitely-wrong"})
    assert await authenticator.authenticate(request) is None


async def test_unicode_bearer_does_not_raise(authenticator: SecretAuthenticator) -> None:
    request = make_request(headers={"Authorization": "Bearer sécrèt-✨-tøken"})
    assert await authenticator.authenticate(request) is None


async def test_non_bearer_scheme_ignored(authenticator: SecretAuthenticator, settings) -> None:
    request = make_request(headers={"Authorization": f"Basic {settings.MCP_SECRET}"})
    assert await authenticator.authenticate(request) is None
    assert extract_bearer_token(request) is None


async def test_valid_session_cookie_yields_session_principal(
    authenticator: SecretAuthenticator, sessions: SessionService
) -> None:
    session_id = await sessions.create()
    request = make_request(cookies={SESSION_COOKIE_NAME: session_id})
    principal = await authenticator.authenticate(request)
    assert principal == Principal(id="owner")
    assert get_auth_info(request) == AuthInfo(method="session", session_id=session_id)


async def test_unknown_session_cookie_rejected(authenticator: SecretAuthenticator) -> None:
    request = make_request(cookies={SESSION_COOKIE_NAME: "not-a-real-session"})
    assert await authenticator.authenticate(request) is None


async def test_verify_login_secret_constant_time_compare(
    authenticator: SecretAuthenticator, settings
) -> None:
    assert authenticator.verify_login_secret(settings.MCP_SECRET) is True
    assert authenticator.verify_login_secret("wrong") is False
    assert authenticator.verify_login_secret("") is False


async def test_secret_with_surrounding_spaces_compares_exact(
    sessions: SessionService,
) -> None:
    spaced = "  abcdefghijklmnop  "
    auth = SecretAuthenticator(spaced, sessions)
    assert auth.verify_login_secret(spaced) is True
    assert auth.verify_login_secret(spaced.strip()) is False


async def test_bearer_token_is_compared_without_stripping(
    sessions: SessionService,
) -> None:
    secret = "exact-secret-with-no-spaces"
    auth = SecretAuthenticator(secret, sessions)

    exact = make_request(headers={"Authorization": f"Bearer {secret}"})
    assert await auth.authenticate(exact) is not None

    trailing = make_request(headers={"Authorization": f"Bearer {secret} "})
    assert await auth.authenticate(trailing) is None

    double_separator = make_request(headers={"Authorization": f"Bearer  {secret}"})
    assert await auth.authenticate(double_separator) is None


async def test_bearer_matches_secret_with_embedded_trailing_space(
    sessions: SessionService,
) -> None:
    secret = "spaced-secret "
    auth = SecretAuthenticator(secret, sessions)

    exact = make_request(headers={"Authorization": f"Bearer {secret}"})
    assert await auth.authenticate(exact) is not None

    stripped = make_request(headers={"Authorization": "Bearer spaced-secret"})
    assert await auth.authenticate(stripped) is None


def test_extract_bearer_token_preserves_inner_whitespace() -> None:
    double_separator = make_request(headers={"Authorization": "Bearer  two-spaces"})
    assert extract_bearer_token(double_separator) == " two-spaces"

    scheme_only = make_request(headers={"Authorization": "Bearer"})
    assert extract_bearer_token(scheme_only) is None

    trailing = make_request(headers={"Authorization": "Bearer token "})
    assert extract_bearer_token(trailing) == "token "
