"""Public slug-bound OAuth callback route.

The route is registered *before* the ``/mcp`` catchall mount so a plugin's
provider callback is handled here, not dispatched as MCP traffic. It is
deliberately public (see ``auth/middleware.py::is_public_path``): a provider
redirect cannot carry the owner bearer token or a dashboard session.

For a known slug every outcome ends in a ``303``
:class:`~starlette.responses.RedirectResponse` to the fixed relative dashboard
target ``/#/plugins/{slug}?oauth=success`` or ``/#/plugins/{slug}?oauth=failed``
— never to a plugin-supplied URL and never with a secret in the URL. An
unknown/invalid slug is a 404, matching every other slug-bound plugin route. A
missing/forged/reused/expired/mismatched state, and a provider-declared error,
are handled without changing plugin status.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from custom_mcp.api.errors import plugin_error
from custom_mcp.plugins.manager import (
    PluginConnectError,
    PluginManager,
    is_valid_slug,
)

#: A provider-declared failure is recorded with this fixed, secret-free message.
_OAUTH_PROVIDER_ERROR_LOG = "The OAuth provider returned an error."


def _manager(request: Request) -> PluginManager:
    manager = request.app.state.plugin_manager
    if not isinstance(manager, PluginManager):
        raise RuntimeError("plugin manager is not configured")
    return manager


def _redirect(slug: str, outcome: str) -> RedirectResponse:
    """Return the fixed relative dashboard redirect (success|failed only)."""
    return RedirectResponse(f"/#/plugins/{slug}?oauth={outcome}", status_code=303)


async def oauth_callback(request: Request) -> Response:
    """Handle a provider OAuth callback for one slug."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")

    manager = _manager(request)
    # Consistent with every other slug-bound plugin route: an unknown slug is a
    # 404 (there is no dashboard entry to redirect to). A known slug always ends
    # in a 303 to the fixed relative success|failed target.
    if await manager.detail(slug) is None:
        return plugin_error("not_found", "No such plugin.")

    state = request.query_params.get("state")
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if not state:
        return _redirect(slug, "failed")

    if error is not None:
        # A provider-declared error requires a valid state; it must not call
        # ``on_oauth_result`` or change plugin status. Consume the state so it
        # cannot be replayed, then redirect failed.
        if not await manager.consume_oauth_state(state, slug):
            return _redirect(slug, "failed")
        await manager.log_health(slug, _OAUTH_PROVIDER_ERROR_LOG, level="error")
        return _redirect(slug, "failed")

    # A valid state is consumed (single-use) and validated inside
    # ``complete_oauth`` before any status mutation.
    try:
        detail: dict[str, Any] = await manager.complete_oauth(
            slug, code=code or "", state=state
        )
    except PluginConnectError:
        return _redirect(slug, "failed")

    outcome = "success" if detail.get("status") == "connected" else "failed"
    return _redirect(slug, outcome)


oauth_routes = [
    Route("/mcp/{slug}/oauth/callback", oauth_callback, methods=["GET"]),
]

__all__ = ["oauth_callback", "oauth_routes"]
