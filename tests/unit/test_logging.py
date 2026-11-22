"""Unit tests for JSON logging and the request-context middleware."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import pytest

from custom_mcp.logging import JsonFormatter, RequestContextMiddleware, request_id_var
from tests.helpers import JsonLogCapture


def _make_record(
    message: str,
    *,
    level: int = logging.INFO,
    extra_fields: dict[str, Any] | None = None,
    exc_info: Any = None,
) -> logging.LogRecord:
    record = logging.LogRecord("custom_mcp.test", level, __file__, 10, message, None, exc_info)
    if extra_fields is not None:
        record.extra_fields = extra_fields
    return record


def _summary_scope() -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/x", "headers": []}


def _receive() -> Any:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


# --- JsonFormatter ----------------------------------------------------------


def test_formatter_writes_single_line_json_through_stream_handler() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("custom_mcp.unit.logging.stream")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    token = request_id_var.set("stream-id")
    try:
        logger.info("streamed")
    finally:
        request_id_var.reset(token)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["request_id"] == "stream-id"
    assert payload["message"] == "streamed"
    assert payload["level"] == "INFO"


def test_formatter_uses_context_request_id() -> None:
    token = request_id_var.set("ctx-id")
    try:
        payload = json.loads(JsonFormatter().format(_make_record("hello")))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "ctx-id"
    assert payload["message"] == "hello"


def test_formatter_extra_fields_override_context_request_id() -> None:
    token = request_id_var.set("ctx-id")
    try:
        record = _make_record(
            "evt",
            extra_fields={"request_id": "state-id", "path": "/p"},
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "state-id"
    assert payload["path"] == "/p"


def test_formatter_defaults_to_dash_without_context() -> None:
    payload = json.loads(JsonFormatter().format(_make_record("noctx")))
    assert payload["request_id"] == "-"


def test_formatter_includes_exception_text() -> None:
    try:
        raise ValueError("bad thing")
    except ValueError:
        record = _make_record("err", level=logging.ERROR, exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exception"]
    assert "bad thing" in payload["exception"]


# --- RequestContextMiddleware ----------------------------------------------


async def _invoke(middleware: RequestContextMiddleware) -> list[MutableMapping[str, Any]]:
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await middleware(_summary_scope(), _receive(), send)
    return sent


async def test_summary_log_has_status_and_matching_header(
    json_logs: JsonLogCapture,
) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = await _invoke(RequestContextMiddleware(app))
    assert request_id_var.get() == "-"

    summaries = json_logs.with_message("request")
    assert summaries
    summary = summaries[-1]
    assert summary["status"] == 204
    assert summary["path"] == "/x"
    assert summary["request_id"] != "-"

    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"].decode() == summary["request_id"]


async def test_summary_records_500_for_unhandled_exception(
    json_logs: JsonLogCapture,
) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    middleware = RequestContextMiddleware(app)
    with pytest.raises(RuntimeError):
        await _invoke(middleware)
    assert request_id_var.get() == "-"

    summary = json_logs.with_message("request")[-1]
    assert summary["status"] == 500
    assert summary["request_id"] != "-"


async def test_concurrent_requests_have_isolated_request_ids(
    json_logs: JsonLogCapture,
) -> None:
    captured: list[str] = []
    started = asyncio.Event()
    arrivals = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            started.set()
        await started.wait()
        captured.append(request_id_var.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestContextMiddleware(app)
    await asyncio.gather(_invoke(middleware), _invoke(middleware))

    assert len(captured) == 2
    assert len(set(captured)) == 2
    assert "-" not in captured
    summary_ids = {summary["request_id"] for summary in json_logs.with_message("request")}
    assert summary_ids == set(captured)
