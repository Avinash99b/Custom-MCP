"""Bounded, host-mediated execution for synchronous plugin code.

Every plugin method, constructor and import runs in a shared thread pool. The
host validates return shapes, tracks timed-out workers precisely, and keeps
per-plugin concurrency bounded even when a caller times out or is cancelled.

A call's timeout bounds the whole call: waiting for a free per-plugin capacity
slot and running the worker. A saturated plugin therefore fails fast with a
timeout instead of stalling the caller (and, for a health refresh, the
lifecycle lock) for as long as other workers occupy the slots.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator

from custom_mcp.logging import get_logger
from custom_mcp.plugins.runtime import PluginRuntime
from custom_mcp.plugins.seal import WritePermit, use_write_permit
from mcp_plugin_sdk import (
    AuthMode,
    ConnectResult,
    Status,
    Tool,
    ToolError,
    ToolResult,
)

logger = get_logger("plugins.host")

LogCallback = Callable[[str, str, str], Awaitable[None]]
CallKind = Literal["timeout", "exception", "plugin_overloaded", "malformed_return"]

TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_HOOK_METHODS = frozenset(
    {
        "on_api_key_set",
        "on_oauth_credentials_set",
        "on_restore",
        "on_disconnect",
        "shutdown",
    }
)
_CONNECT_METHODS = frozenset({"connect", "on_oauth_result"})


class PluginCallError(Exception):
    """A normalized plugin-call failure.

    ``detail_safe`` never contains plugin exception text. ``detail_log`` may
    contain representation data and must be redacted by the diagnostics layer
    before it reaches persisted logs or the API.
    """

    def __init__(
        self,
        kind: CallKind,
        detail_safe: str,
        *,
        detail_log: str | None = None,
    ) -> None:
        self.kind = kind
        self.detail_safe = detail_safe
        self.detail_log = detail_log
        super().__init__(detail_safe)

    @property
    def safe_message(self) -> str:
        """A client-safe description; never contains plugin exception text."""
        return self.detail_safe


class _HostContractError(Exception):
    """Explicit host-owned contract failure with a fixed safe message.

    Only this exact internal type is allowed to carry a non-generic client-safe
    plugin failure message. Arbitrary plugin exceptions are never inspected for
    ``safe_message``/``detail_safe`` attributes.
    """

    def __init__(self, detail_safe: str, *, detail_log: str | None = None) -> None:
        self.detail_safe = detail_safe
        self.detail_log = detail_log
        super().__init__(detail_safe)


class _MalformedReturn(Exception):
    def __init__(self, detail_safe: str, detail_log: str) -> None:
        self.detail_safe = detail_safe
        self.detail_log = detail_log
        super().__init__(detail_safe)


class _WorkerFailure(Exception):
    """Internal carrier for an off-loop plugin failure."""

    def __init__(self, detail_safe: str, detail_log: str) -> None:
        self.detail_safe = detail_safe
        self.detail_log = detail_log
        super().__init__(detail_safe)


class _NotLoaded(Exception):
    pass


@dataclass(slots=True)
class _CallState:
    released: bool = False
    leaked: bool = False
    done: bool = False
    permit: WritePermit | None = None


def _safe_repr(value: Any) -> str:
    """Best-effort repr that never leaks a raw ``__repr__`` failure."""
    try:
        return repr(value)
    except BaseException as exc:
        try:
            name = type(exc).__name__
        except BaseException:
            name = "Exception"
        return f"<unrepresentable {name}>"


def _safe_exception_parts(exc: BaseException) -> tuple[str, str]:
    """Return ``(detail_safe, detail_log)`` for a worker failure.

    Only the explicit host-owned :class:`_HostContractError` may supply a
    non-generic safe message. Arbitrary plugin exceptions are never probed for
    ``safe_message``/``detail_safe`` attributes; that could execute plugin
    properties or leak credential-bearing text.
    """
    if isinstance(exc, _HostContractError):
        safe = exc.detail_safe or "Plugin contract check failed."
        log = exc.detail_log or f"{type(exc).__name__}: {safe}"
        return safe, log
    try:
        name = type(exc).__name__
    except BaseException:
        name = "Exception"
    return "Plugin raised an exception.", f"{name}: {_safe_repr(exc)}"


def _worker_failure(exc: BaseException) -> _WorkerFailure:
    safe, log = _safe_exception_parts(exc)
    return _WorkerFailure(safe, log)


def _malformed(message: str, value: Any) -> _MalformedReturn:
    return _MalformedReturn(message, f"value={_safe_repr(value)}")


def _json_safe(value: Any, message: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _malformed(message, value) from exc


def _auth_modes(value: Any) -> list[AuthMode]:
    if not isinstance(value, list) or not value:
        raise _malformed("Plugin returned an invalid auth-mode list.", value)
    seen: set[AuthMode] = set()
    for mode in value:
        if not isinstance(mode, AuthMode) or mode in seen:
            raise _malformed("Plugin returned an invalid auth-mode list.", value)
        seen.add(mode)
    return list(value)


def _tools(value: Any) -> list[Tool]:
    if not isinstance(value, list):
        raise _malformed("Plugin returned an invalid tool list.", value)
    names: set[str] = set()
    for tool in value:
        if not isinstance(tool, Tool):
            raise _malformed("Plugin returned an invalid tool list.", value)
        if not isinstance(tool.name, str) or TOOL_NAME_RE.fullmatch(tool.name) is None:
            raise _malformed("Plugin returned an invalid tool name.", tool.name)
        if tool.name in names:
            raise _malformed("Plugin returned duplicate tool names.", tool.name)
        names.add(tool.name)
        if not isinstance(tool.description, str):
            raise _malformed("Plugin returned an invalid tool description.", tool.description)
        schema = tool.input_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise _malformed("Plugin tool schema must declare type 'object'.", schema)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise _malformed("Plugin returned an invalid tool schema.", schema) from exc
        _json_safe(schema, "Plugin returned a non-serializable tool schema.")
    return list(value)


def _connect_result(value: Any) -> ConnectResult:
    if not isinstance(value, ConnectResult) or not isinstance(value.status, Status):
        raise _malformed("Plugin returned an invalid connection result.", value)
    if value.message is not None and not isinstance(value.message, str):
        raise _malformed("Plugin returned an invalid connection result.", value)
    if value.redirect_url is not None and not isinstance(value.redirect_url, str):
        raise _malformed("Plugin returned an invalid connection result.", value)
    return value


def _status(value: Any) -> Status:
    if not isinstance(value, Status):
        raise _malformed("Plugin returned an invalid status.", value)
    return value


def _tool_result(value: Any) -> ToolResult:
    if not isinstance(value, ToolResult) or not isinstance(value.ok, bool):
        raise _malformed("Plugin returned an invalid tool result.", value)
    if value.error is not None:
        error = value.error
        if not isinstance(error, ToolError):
            raise _malformed("Plugin returned an invalid tool error.", error)
        if not isinstance(error.code, str) or not isinstance(error.message, str):
            raise _malformed("Plugin returned an invalid tool error.", error)
        if not isinstance(error.retryable, bool):
            raise _malformed("Plugin returned an invalid tool error.", error)
    if value.ok:
        if value.error is not None or not isinstance(value.result, dict):
            raise _malformed("Plugin returned an inconsistent tool result.", value)
        _json_safe(value.result, "Plugin returned a non-serializable tool result.")
    else:
        if value.error is None or value.result is not None:
            raise _malformed("Plugin returned an inconsistent tool result.", value)
    return value


def _validate_result(method: str, value: Any) -> Any:
    if method == "auth_methods":
        return _auth_modes(value)
    if method == "list_tools":
        return _tools(value)
    if method in _CONNECT_METHODS:
        return _connect_result(value)
    if method in {"ping", "refresh"}:
        return _status(value)
    if method == "invoke_tool":
        return _tool_result(value)
    if method == "get_oauth_sign_in_url":
        if not isinstance(value, str):
            raise _malformed("Plugin returned an invalid sign-in URL.", value)
        return value
    if method in _HOOK_METHODS:
        if value is not None:
            raise _malformed("Plugin hook returned a value; expected None.", value)
        return None
    return value


class PluginHost:
    """Shared executor, per-runtime semaphore and result validation."""

    def __init__(
        self,
        *,
        worker_threads: int = 16,
        per_plugin_concurrency: int = 4,
        call_timeout: float = 15.0,
        tool_timeout: float = 30.0,
        shutdown_timeout: float = 5.0,
        log: LogCallback | None = None,
        executor: concurrent.futures.ThreadPoolExecutor | None = None,
    ) -> None:
        if worker_threads < 1 or per_plugin_concurrency < 1:
            raise ValueError("worker_threads and per_plugin_concurrency must be positive")
        if call_timeout <= 0 or tool_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("plugin timeouts must be positive")
        self._per_plugin_concurrency = per_plugin_concurrency
        self._call_timeout = call_timeout
        self._tool_timeout = tool_timeout
        self._shutdown_timeout = shutdown_timeout
        self._log = log
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_threads,
            thread_name_prefix="plugin-host",
        )
        self._owns_executor = executor is None
        self._closed = False
        # Keyed by slug so leaked workers from an old runtime generation keep
        # counting against replacement/reload calls until they actually finish.
        self._leaked_by_slug: dict[str, int] = {}
        self._idle_events: dict[str, asyncio.Event] = {}

    @property
    def shutdown_timeout(self) -> float:
        return self._shutdown_timeout

    def leaked_workers(self, slug: str) -> int:
        """Number of unfinished timed-out/cancelled workers for a slug."""
        return self._leaked_by_slug.get(slug, 0)

    async def wait_for_idle(
        self,
        slug: str,
        timeout: float | None = None,  # noqa: ASYNC109 - bounded diagnostic wait
    ) -> bool:
        """Wait until no leaked worker remains for ``slug``."""
        if self.leaked_workers(slug) == 0:
            return True
        event = self._idle_event(slug)
        if timeout is None:
            await event.wait()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def _idle_event(self, slug: str) -> asyncio.Event:
        event = self._idle_events.get(slug)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._idle_events[slug] = event
        return event

    def _sync_runtime_leaks(self, runtime: PluginRuntime) -> int:
        count = self.leaked_workers(runtime.slug)
        runtime._leaked_workers = count
        return count

    @staticmethod
    def validate_tools(value: Any) -> list[Tool]:
        """Validate and return a tool list, raising a normalized error.

        This is an internal/bounded helper: it may execute JSON Schema checks
        for hostile metadata and must be called inside a host worker, not on the
        event loop. ``host.call(runtime, "list_tools")`` already routes through
        the worker and returns a validated list.
        """
        try:
            return _tools(value)
        except _MalformedReturn as exc:
            raise PluginCallError(
                "malformed_return", exc.detail_safe, detail_log=exc.detail_log
            ) from None

    async def call(
        self,
        runtime: PluginRuntime,
        method: str,
        *args: Any,
        timeout: float | None = None,  # noqa: ASYNC109 - API contract keyword
    ) -> Any:
        """Call a synchronous plugin method off the event loop."""
        limit = timeout
        if limit is None:
            limit = self._tool_timeout if method == "invoke_tool" else self._call_timeout
        # A deliberate ``shutdown`` may persist data on a sealed generation. Its
        # permit is revoked if the call is abandoned, so a timed-out shutdown
        # worker cannot keep writing after its call timed out.
        permit = WritePermit() if method == "shutdown" else None
        return await self._run(
            runtime,
            self._method_worker,
            runtime,
            method,
            args,
            permit,
            timeout=limit,
            method_label=method,
            permit=permit,
        )

    async def call_callable(
        self,
        runtime: PluginRuntime,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,  # noqa: ASYNC109 - API contract keyword
    ) -> Any:
        """Call an arbitrary plugin-owned synchronous callable off-loop."""
        limit = self._call_timeout if timeout is None else timeout
        return await self._run(
            runtime,
            self._callable_worker,
            fn,
            args,
            timeout=limit,
            method_label=getattr(fn, "__name__", "<callable>"),
        )

    async def aclose(self) -> None:
        """Shut the owned executor down without waiting for hung workers."""
        if self._closed:
            return
        self._closed = True
        if self._owns_executor:
            await asyncio.to_thread(
                self._executor.shutdown, wait=False, cancel_futures=True
            )

    @staticmethod
    def _method_worker(
        runtime: PluginRuntime,
        method: str,
        args: tuple[Any, ...],
        permit: WritePermit | None = None,
    ) -> Any:
        instance = runtime.instance
        if instance is None:
            raise _NotLoaded
        try:
            # Lookup happens in the worker: a plugin property must never run on loop.
            func = getattr(instance, method)
            if permit is not None:
                # A deliberate teardown hook may persist buffered state even on a
                # sealed generation. The permit is thread-scoped (a leaked worker
                # on another thread stays blocked) and revoked on abandonment.
                with use_write_permit(permit):
                    result = func(*args)
            else:
                result = func(*args)
            return _validate_result(method, result)
        except (_MalformedReturn, _WorkerFailure):
            raise
        except BaseException as exc:
            raise _worker_failure(exc) from None

    @staticmethod
    def _callable_worker(fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        try:
            return fn(*args)
        except _WorkerFailure:
            raise
        except BaseException as exc:
            raise _worker_failure(exc) from None

    async def _run(
        self,
        runtime: PluginRuntime,
        worker: Callable[..., Any],
        *args: Any,
        timeout: float,  # noqa: ASYNC109 - intentional per-call execution bound
        method_label: str,
        permit: WritePermit | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if self._sync_runtime_leaks(runtime) >= self._per_plugin_concurrency:
            await self._report_overloaded(runtime, method_label)
            raise PluginCallError(
                "plugin_overloaded",
                "Plugin is busy; try again after outstanding calls finish.",
            )

        # Admission is part of the call budget: a plugin saturated by other
        # calls must surface a timeout at the deadline rather than hold the
        # caller (e.g. a health refresh that owns the lifecycle lock) until the
        # occupying workers finish. ``Semaphore.acquire`` restores the slot on
        # cancellation, so a timed-out waiter leaves no slot consumed.
        try:
            await asyncio.wait_for(runtime.sem.acquire(), timeout=timeout)
        except TimeoutError:
            await self._report_error(
                runtime,
                "timeout",
                "Plugin call timed out.",
                method=method_label,
            )
            raise PluginCallError(
                "timeout",
                "Plugin call timed out.",
                detail_log=f"method={method_label} timeout={timeout}",
            ) from None
        if self._sync_runtime_leaks(runtime) >= self._per_plugin_concurrency:
            runtime.sem.release()
            await self._report_overloaded(runtime, method_label)
            raise PluginCallError(
                "plugin_overloaded",
                "Plugin is busy; try again after outstanding calls finish.",
            )

        try:
            concurrent_future = self._executor.submit(worker, *args)
        except Exception:
            runtime.sem.release()
            raise PluginCallError(
                "exception", "Plugin host is unavailable."
            ) from None

        wrapped = asyncio.wrap_future(concurrent_future)
        state = _CallState(permit=permit)

        def _finished(future: Any) -> None:
            # Consume an abandoned worker's exception without formatting it:
            # otherwise asyncio reports "Future exception was never retrieved"
            # and may repr credential-bearing exceptions.
            if not future.cancelled():
                with contextlib.suppress(BaseException):
                    future.exception()
            state.done = True
            self._finish(runtime, state)

        wrapped.add_done_callback(_finished)

        try:
            remaining = deadline - loop.time()
            result = await asyncio.wait_for(
                asyncio.shield(wrapped), timeout=max(0.0, remaining)
            )
        except TimeoutError:
            self._abandon(runtime, state, concurrent_future)
            await self._report_error(
                runtime,
                "timeout",
                "Plugin call timed out.",
                method=method_label,
            )
            raise PluginCallError(
                "timeout",
                "Plugin call timed out.",
                detail_log=f"method={method_label} timeout={timeout}",
            ) from None
        except asyncio.CancelledError:
            self._abandon(runtime, state, concurrent_future)
            raise
        except _WorkerFailure as exc:
            self._finish(runtime, state)
            await self._report_error(
                runtime, "exception", exc.detail_safe, method=method_label
            )
            raise PluginCallError(
                "exception", exc.detail_safe, detail_log=exc.detail_log
            ) from None
        except _MalformedReturn as exc:
            self._finish(runtime, state)
            await self._report_error(
                runtime, "malformed_return", exc.detail_safe, method=method_label
            )
            raise PluginCallError(
                "malformed_return", exc.detail_safe, detail_log=exc.detail_log
            ) from None
        except _NotLoaded:
            self._finish(runtime, state)
            raise PluginCallError("exception", "Plugin is not loaded.") from None
        except Exception:
            # Trusted-host fallback only; plugin exceptions are wrapped in the
            # worker and must not be repr'd on the event loop.
            self._finish(runtime, state)
            await self._report_error(
                runtime, "exception", "Plugin raised an exception.", method=method_label
            )
            raise PluginCallError(
                "exception", "Plugin raised an exception."
            ) from None
        self._finish(runtime, state)
        return result

    def _finish(self, runtime: PluginRuntime, state: _CallState) -> None:
        if not state.released:
            state.released = True
            runtime.sem.release()
        if state.leaked:
            state.leaked = False
            current = max(0, self.leaked_workers(runtime.slug) - 1)
            self._leaked_by_slug[runtime.slug] = current
            runtime._leaked_workers = current
            if current == 0:
                self._idle_event(runtime.slug).set()

    def _abandon(
        self,
        runtime: PluginRuntime,
        state: _CallState,
        concurrent_future: concurrent.futures.Future[Any],
    ) -> None:
        if state.permit is not None:
            # The caller timed out or was cancelled: the worker is abandoned, so
            # it must lose any sealed-runtime write permit it carried.
            state.permit.revoke()
        if state.done:
            return
        if concurrent_future.cancel():
            # The job never started, so there is no worker to leak. The wrapped
            # future callback will release the slot; doing it here as well is
            # idempotent.
            self._finish(runtime, state)
            return
        if not state.leaked:
            state.leaked = True
            current = self.leaked_workers(runtime.slug) + 1
            self._leaked_by_slug[runtime.slug] = current
            runtime._leaked_workers = current
            self._idle_event(runtime.slug).clear()
        if not state.released:
            state.released = True
            runtime.sem.release()

    async def _report_overloaded(
        self, runtime: PluginRuntime, method_label: str
    ) -> None:
        logger.error(
            "plugin_overloaded",
            extra={
                "extra_fields": {
                    "slug": runtime.slug,
                    "method": method_label,
                    "leaked_workers": self.leaked_workers(runtime.slug),
                }
            },
        )
        await self._notify_log(runtime.slug, "Plugin is busy.")

    async def _report_error(
        self,
        runtime: PluginRuntime,
        kind: str,
        safe_message: str,
        *,
        method: str,
    ) -> None:
        logger.error(
            "plugin_call_failed",
            extra={
                "extra_fields": {
                    "slug": runtime.slug,
                    "method": method,
                    "kind": kind,
                    "message": safe_message,
                }
            },
        )
        await self._notify_log(runtime.slug, safe_message)

    async def _notify_log(self, slug: str, message: str) -> None:
        if self._log is None:
            return
        try:
            await self._log(slug, "error", message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "plugin_log_callback_failed",
                extra={"extra_fields": {"slug": slug}},
            )


__all__ = [
    "CallKind",
    "LogCallback",
    "PluginCallError",
    "PluginHost",
]
