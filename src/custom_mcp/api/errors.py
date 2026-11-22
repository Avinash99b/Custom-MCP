"""API error responses and security headers.

All JSON errors share the shape ``{"error": {"code": ..., "message": ...}}``.
Internal diagnostics are logged but never returned to clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from custom_mcp.logging import get_logger

logger = get_logger("errors")

_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}

_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable_entity",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}

# Plugin-domain error codes are deliberately distinct from generic HTTP status
# phrases. Only handlers that own these domains use this mapping.
_PLUGIN_CODE_STATUS: dict[str, int] = {
    "plugin_exists": 409,
    "conflict": 409,
    "not_loaded": 409,
    "invalid_plugin": 422,
    "invalid_request": 422,
    "payload_too_large": 413,
    "not_found": 404,
}


def security_headers() -> dict[str, str]:
    """Return a fresh copy of the baseline security headers."""
    return dict(_SECURITY_HEADERS)


def error_payload(code: str, message: str) -> dict[str, dict[str, str]]:
    """Return the canonical JSON error body."""
    return {"error": {"code": code, "message": message}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a JSON error response carrying security and no-store headers."""
    merged = security_headers()
    merged["Cache-Control"] = "no-store"
    if headers:
        merged.update(headers)
    return JSONResponse(error_payload(code, message), status_code=status_code, headers=merged)


def plugin_error(
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an error response for a known plugin-domain code."""
    return error_response(_PLUGIN_CODE_STATUS.get(code, 422), code, message, headers)


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert Starlette ``HTTPException``s into the shared JSON error shape."""
    if not isinstance(exc, HTTPException):
        return await unhandled_exception_handler(request, exc)
    code = _CODE_BY_STATUS.get(exc.status_code, f"http_{exc.status_code}")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else _phrase(exc.status_code)
    return error_response(exc.status_code, code, detail, headers=exc.headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a generic 500 without leaking internals."""
    request_id = getattr(request.state, "request_id", None)
    extra_fields: dict[str, object] = {"path": request.url.path}
    headers: dict[str, str] | None = None
    if isinstance(request_id, str) and request_id:
        extra_fields["request_id"] = request_id
        headers = {"X-Request-ID": request_id}
    logger.error(
        "unhandled_exception",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"extra_fields": extra_fields},
    )
    return error_response(
        500,
        "internal_error",
        "An internal server error occurred.",
        headers=headers,
    )


__all__ = [
    "error_payload",
    "error_response",
    "http_exception_handler",
    "plugin_error",
    "security_headers",
    "unhandled_exception_handler",
]
