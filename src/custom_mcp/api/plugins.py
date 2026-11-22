"""Plugin registry REST endpoints for Phase 1.

Authentication is inherited from the ASGI middleware: these handlers only read
``request.app.state.plugin_manager`` and therefore never inspect headers,
cookies or credentials. Uploads are bounded before multipart parsing so a huge
body cannot be spooled to disk without limit.
"""

from __future__ import annotations

import json
from contextlib import aclosing
from typing import Any

from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Message, Receive

from custom_mcp.api.errors import plugin_error
from custom_mcp.logging import get_logger
from custom_mcp.plugins.manager import (
    PluginConnectError,
    PluginExistsError,
    PluginManager,
    is_valid_slug,
)
from custom_mcp.plugins.manifest import ManifestError
from custom_mcp.plugins.packaging import InvalidPluginError, PackageTooLargeError

logger = get_logger("plugins.api")

# Multipart framing, headers and the ``mode`` field are allowed on top of the
# configured package size. The actual file field is checked separately.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_MAX_FORM_FIELDS = 8
_MAX_NON_FILE_PART_BYTES = 64 * 1024
_INSTALL_MODES = frozenset({"reject", "replace"})
_LOG_DEFAULT_LIMIT = 50
_LOG_MAX_LIMIT = 200
#: Hard limit for the connect/disconnect JSON body (credentials are tiny).
_CONNECT_BODY_LIMIT = 64 * 1024

_NO_STORE = {"Cache-Control": "no-store"}


class _BodyTooLargeError(Exception):
    """Raised by the bounded receive wrapper before a body is fully read."""


class _BoundedReceive:
    """ASGI receive wrapper enforcing a hard total-body limit."""

    def __init__(self, receive: Receive, limit: int) -> None:
        self._receive = receive
        self._limit = limit
        self._total = 0

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._total += len(message.get("body", b""))
            if self._total > self._limit:
                raise _BodyTooLargeError
        return message


def _manager(request: Request) -> PluginManager:
    manager = request.app.state.plugin_manager
    if not isinstance(manager, PluginManager):
        raise RuntimeError("plugin manager is not configured")
    return manager


def _json(payload: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_NO_STORE)


async def _close_uploads(form: FormData) -> None:
    for _, value in form.multi_items():
        if isinstance(value, UploadFile):
            await value.close()


async def _parse_multipart(
    request: Request,
    *,
    max_files: int,
    max_fields: int,
    max_part_size: int,
) -> FormData:
    """Parse multipart while guaranteeing temporary files close on any error.

    The bounded receive wrapper can raise outside Starlette's parser error
    handling, so cleanup is done against the parser's own file list instead of
    relying on the caller having a ``FormData`` instance.
    """
    stream = request.stream()
    parser = MultiPartParser(
        request.headers,
        stream,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )
    try:
        async with aclosing(stream):
            return await parser.parse()
    except BaseException:
        for file in getattr(parser, "_files_to_close_on_error", ()):
            file.close()
        raise


def _requested_mode(request: Request, form: FormData) -> str | None:
    """Query mode takes precedence; form mode remains a compatibility fallback."""
    query_mode = request.query_params.get("mode")
    if query_mode is not None:
        return query_mode.strip()
    raw_form = form.get("mode")
    if raw_form is None:
        return "reject"
    if not isinstance(raw_form, str):
        return None
    return raw_form.strip() or "reject"


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return -1
    return value if value >= 0 else -1


async def list_plugins(request: Request) -> Response:
    """Return dashboard summaries for every discovered package."""
    manager = _manager(request)
    return _json({"plugins": await manager.summaries()})


async def plugin_detail(request: Request) -> Response:
    """Return dashboard detail for one slug."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    detail = await _manager(request).detail(slug)
    if detail is None:
        return plugin_error("not_found", "No such plugin.")
    return _json(detail)


async def delete_plugin(request: Request) -> Response:
    """Remove one plugin and only its exact state namespaces."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    removed = await _manager(request).remove(slug)
    if not removed:
        return plugin_error("not_found", "No such plugin.")
    return Response(status_code=204, headers=_NO_STORE)


async def reload_plugin(request: Request) -> Response:
    """Re-extract the stored package, unload and reload the plugin code."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    manager = _manager(request)
    if not await manager.reload(slug):
        return plugin_error("not_found", "No such plugin.")
    detail = await manager.detail(slug)
    if detail is None:
        return plugin_error("not_found", "No such plugin.")
    return _json(detail)


async def _parse_connect_body(request: Request) -> dict[str, Any] | Response:
    """Read and validate the bounded JSON connect body.

    Returns either a ``{"mode": ..., "credentials": {...}}`` mapping or an error
    :class:`~starlette.responses.Response` the caller should return directly.
    A declared ``Content-Length`` over the limit is rejected before reading; an
    actual overrun, invalid JSON or wrong shape becomes a safe error.
    """
    declared = _declared_length(request)
    if declared is not None and declared < 0:
        return plugin_error("invalid_request", "The request body is invalid.")
    if declared is not None and declared > _CONNECT_BODY_LIMIT:
        return plugin_error("payload_too_large", "The request body is too large.")

    bounded = Request(
        request.scope,
        receive=_BoundedReceive(request._receive, _CONNECT_BODY_LIMIT),
    )
    try:
        raw = await bounded.body()
    except _BodyTooLargeError:
        return plugin_error("payload_too_large", "The request body is too large.")

    try:
        parsed = json.loads(raw) if raw.strip() else None
    except (TypeError, ValueError):
        return plugin_error("invalid_request", "The request body is invalid.")
    if not isinstance(parsed, dict):
        return plugin_error("invalid_request", "The request body must be a JSON object.")

    mode = parsed.get("mode")
    if not isinstance(mode, str):
        return plugin_error("invalid_request", "The authentication mode is required.")
    credentials = parsed.get("credentials", {})
    if credentials is None:
        credentials = {}
    if not isinstance(credentials, dict):
        return plugin_error("invalid_request", "The credentials must be a JSON object.")
    return {"mode": mode, "credentials": credentials}


async def connect_plugin(request: Request) -> Response:
    """Apply credentials and connect a loaded plugin for the selected mode."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    body = await _parse_connect_body(request)
    if isinstance(body, Response):
        return body
    manager = _manager(request)
    if await manager.detail(slug) is None:
        return plugin_error("not_found", "No such plugin.")
    try:
        detail = await manager.connect(
            slug, mode=body["mode"], credentials=body["credentials"]
        )
    except PluginConnectError as exc:
        return plugin_error(exc.code, exc.message)
    return _json(detail)


async def disconnect_plugin(request: Request) -> Response:
    """Run ``on_disconnect``, wipe every secret and reset mode/status."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    manager = _manager(request)
    if await manager.detail(slug) is None:
        return plugin_error("not_found", "No such plugin.")
    try:
        detail = await manager.disconnect(slug)
    except PluginConnectError as exc:
        return plugin_error(exc.code, exc.message)
    return _json(detail)


def _log_limit(request: Request) -> int | None:
    """Parse and clamp the ``limit`` query parameter; ``None`` means invalid."""
    raw = request.query_params.get("limit")
    if raw is None:
        return _LOG_DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return min(value, _LOG_MAX_LIMIT)


async def plugin_logs(request: Request) -> Response:
    """Return up to ``limit`` newest redacted log entries for one slug."""
    slug = request.path_params.get("slug", "")
    if not is_valid_slug(slug):
        return plugin_error("not_found", "No such plugin.")
    limit = _log_limit(request)
    if limit is None:
        return plugin_error("invalid_request", "The log limit is invalid.")
    entries = await _manager(request).logs(slug, limit)
    if entries is None:
        return plugin_error("not_found", "No such plugin.")
    return _json({"logs": entries})


async def import_plugin(request: Request) -> Response:
    """Validate and store a multipart plugin ZIP without loading it."""
    manager = _manager(request)
    max_zip = manager.max_plugin_zip_bytes
    max_body = max_zip + _MULTIPART_OVERHEAD_BYTES

    declared = _declared_length(request)
    if declared is not None and declared < 0:
        return plugin_error("invalid_request", "The request body is invalid.")
    if declared is not None and declared > max_body:
        return plugin_error("payload_too_large", "The uploaded plugin package is too large.")

    bounded_request = Request(
        request.scope,
        receive=_BoundedReceive(request._receive, max_body),
    )
    try:
        form = await _parse_multipart(
            bounded_request,
            max_files=1,
            max_fields=_MAX_FORM_FIELDS,
            max_part_size=_MAX_NON_FILE_PART_BYTES,
        )
    except _BodyTooLargeError:
        return plugin_error("payload_too_large", "The uploaded plugin package is too large.")
    except MultiPartException:
        return plugin_error("invalid_request", "The request body is invalid.")

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        await _close_uploads(form)
        return plugin_error("invalid_request", "A plugin package file is required.")

    try:
        data = await upload.read(max_zip + 1)
    finally:
        await upload.close()
    if len(data) > max_zip:
        return plugin_error("payload_too_large", "The uploaded plugin package is too large.")

    mode = _requested_mode(request, form)
    if mode not in _INSTALL_MODES:
        return plugin_error("invalid_request", "The requested install mode is invalid.")

    try:
        record = await manager.install(data, mode=mode)
    except PackageTooLargeError:
        return plugin_error("payload_too_large", "The uploaded plugin package is too large.")
    except PluginExistsError:
        return plugin_error("plugin_exists", "A plugin with this slug already exists.")
    except (ManifestError, InvalidPluginError) as exc:
        reason = str(exc).strip() or "The uploaded plugin package is invalid."
        return plugin_error("invalid_plugin", reason)

    detail = await manager.detail(record.slug)
    if detail is None:
        detail = {"slug": record.slug, "load_state": record.load_state}
    return _json(detail, status_code=201)


plugin_routes = [
    # The import route must precede /api/plugins/{slug}, otherwise "import"
    # would be interpreted as a slug for POST requests.
    Route("/api/plugins/import", import_plugin, methods=["POST"]),
    Route("/api/plugins", list_plugins, methods=["GET"]),
    Route("/api/plugins/{slug}/reload", reload_plugin, methods=["POST"]),
    Route("/api/plugins/{slug}/connect", connect_plugin, methods=["POST"]),
    Route("/api/plugins/{slug}/disconnect", disconnect_plugin, methods=["POST"]),
    Route("/api/plugins/{slug}/logs", plugin_logs, methods=["GET"]),
    Route("/api/plugins/{slug}", plugin_detail, methods=["GET"]),
    Route("/api/plugins/{slug}", delete_plugin, methods=["DELETE"]),
]

__all__ = [
    "connect_plugin",
    "delete_plugin",
    "disconnect_plugin",
    "import_plugin",
    "list_plugins",
    "plugin_detail",
    "plugin_logs",
    "plugin_routes",
    "reload_plugin",
]
