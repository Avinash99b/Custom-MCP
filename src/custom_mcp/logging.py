"""Structured JSON logging to stdout.

A single ``request_id`` context variable is threaded through log records so that
every line emitted while handling a request can be correlated. Request bodies
are never logged and secrets are never passed to these helpers.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_LOGGER_NAME = "custom_mcp"


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, writing to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger (``custom_mcp`` by default)."""
    return logging.getLogger(_LOGGER_NAME if name is None else f"{_LOGGER_NAME}.{name}")


class RequestContextMiddleware:
    """Assign a request id, echo it as ``X-Request-ID``, and log one line.

    Only method, path, status and duration are recorded; request bodies and
    credentials are never logged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 0
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                # A propagating exception means no response was produced by the
                # inner app; the outer error middleware will emit a 500. Keep any
                # status already sent, and never claim success for cancellations
                # (CancelledError is a BaseException and is not caught here).
                if not response_started:
                    status_code = 500
                raise
        finally:
            # Emit the summary while the request id is still in context, and
            # guarantee the context is reset even if logging itself raises.
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                get_logger("request").info(
                    "request",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "method": scope.get("method", ""),
                            "path": scope.get("path", ""),
                            "status": status_code,
                            "duration_ms": duration_ms,
                        }
                    },
                )
            finally:
                request_id_var.reset(token)


__all__ = [
    "JsonFormatter",
    "RequestContextMiddleware",
    "configure_logging",
    "get_logger",
    "request_id_var",
]
