"""Backup export and restore REST endpoints (Phase 6).

Both handlers read ``request.app.state`` only for their own services and never
touch headers, cookies or credentials; authentication is inherited from the ASGI
middleware. The restore upload is bounded before multipart parsing (mirroring the
plugin import route) so a huge body cannot be spooled to disk without limit. The
export response is streamed from the service-owned spool.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from custom_mcp.api.errors import plugin_error
from custom_mcp.api.plugins import (
    _BodyTooLargeError,
    _BoundedReceive,
    _close_uploads,
    _declared_length,
    _parse_multipart,
)
from custom_mcp.backup import (
    STREAM_CHUNK_BYTES,
    BackupService,
    InvalidBackupError,
    RestoreService,
    UnknownPluginError,
)

# Multipart framing/headers on top of the configured backup limit.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_MAX_FORM_FIELDS = 8
_MAX_NON_FILE_PART_BYTES = 64 * 1024
_RESTORE_MODES = frozenset({"skip", "replace"})

_NO_STORE = {"Cache-Control": "no-store"}


def _backup(request: Request) -> BackupService:
    service = request.app.state.backup
    if not isinstance(service, BackupService):
        raise RuntimeError("backup service is not configured")
    return service


def _restore(request: Request) -> RestoreService:
    service = request.app.state.restore
    if not isinstance(service, RestoreService):
        raise RuntimeError("restore service is not configured")
    return service


def _json(payload: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_NO_STORE)


def _max_backup_bytes(request: Request) -> int:
    settings = request.app.state.settings
    return int(settings.MAX_BACKUP_MB) * 1024 * 1024


def _plugin_filter(request: Request) -> list[str] | None:
    """Parse ``?plugins=a,b``; ``None`` means every installed plugin."""
    raw = request.query_params.get("plugins")
    if raw is None or raw.strip() == "":
        return None
    return [token.strip() for token in raw.split(",")]


def _slug_filter(request: Request) -> list[str] | None:
    """Parse ``?slugs=a,b``; ``None`` means every plugin in the archive."""
    raw = request.query_params.get("slugs")
    if raw is None or raw.strip() == "":
        return None
    return [token.strip() for token in raw.split(",")]


def _restore_mode(request: Request) -> str | None:
    raw = request.query_params.get("mode")
    if raw is None or raw.strip() == "":
        return "skip"
    value = raw.strip()
    return value if value in _RESTORE_MODES else None


async def _stream(spool: tempfile.SpooledTemporaryFile[bytes]) -> AsyncIterator[bytes]:
    try:
        while True:
            chunk = await asyncio.to_thread(spool.read, STREAM_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(spool.close)


async def export_backup(request: Request) -> Response:
    """Stream a backup archive for every installed plugin or an optional subset."""
    try:
        spool = await _backup(request).build(_plugin_filter(request))
    except UnknownPluginError:
        return plugin_error("not_found", "No such plugin.")
    filename = f"custom-mcp-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return StreamingResponse(
        _stream(spool), media_type="application/zip", headers=headers
    )


async def restore_backup(request: Request) -> Response:
    """Validate and apply a multipart backup archive."""
    service = _restore(request)
    max_backup = _max_backup_bytes(request)
    max_body = max_backup + _MULTIPART_OVERHEAD_BYTES

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        return plugin_error("invalid_request", "A multipart backup upload is required.")

    declared = _declared_length(request)
    if declared is not None and declared < 0:
        return plugin_error("invalid_request", "The request body is invalid.")
    if declared is not None and declared > max_body:
        return plugin_error("payload_too_large", "The uploaded backup is too large.")

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
        return plugin_error("payload_too_large", "The uploaded backup is too large.")
    except MultiPartException:
        return plugin_error("invalid_request", "The request body is invalid.")

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        await _close_uploads(form)
        return plugin_error("invalid_request", "A backup file is required.")

    try:
        data = await upload.read(max_backup + 1)
    finally:
        await upload.close()
    if len(data) > max_backup:
        return plugin_error("payload_too_large", "The uploaded backup is too large.")

    mode = _restore_mode(request)
    if mode is None:
        return plugin_error("invalid_request", "The requested restore mode is invalid.")

    try:
        report = await service.restore(data, mode=mode, slugs=_slug_filter(request))
    except UnknownPluginError:
        return plugin_error("not_found", "No such plugin.")
    except InvalidBackupError as exc:
        return plugin_error("invalid_plugin", str(exc) or "The backup is invalid.")
    return _json(report.to_dict())


backup_routes = [
    Route("/api/backup", export_backup, methods=["GET"]),
    Route("/api/restore", restore_backup, methods=["POST"]),
]

__all__ = ["backup_routes", "export_backup", "restore_backup"]
