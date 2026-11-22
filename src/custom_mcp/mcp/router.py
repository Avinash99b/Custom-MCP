"""Dynamic per-slug MCP router for the gateway (Phase 3, router slice).

The router is a raw ASGI app mounted once at ``/mcp`` in :mod:`custom_mcp.main`
(``Mount("/mcp", app=router)``). It resolves the first path segment as a plugin
slug and forwards the request to that slug's FastMCP streamable-HTTP app with the
scope rewritten so the inner app sees its own root.

Pinned-library constraints (verified against the installed packages)
---------------------------------------------------------------------
* Starlette 1.10 ``Mount.matches`` no longer rewrites the child ``path``; the
  child scope carries the *full* request path (for example
  ``/mcp/demo/mcp``) and ``root_path == "/mcp"``. The router therefore derives
  ``{slug}`` from ``path`` relative to its own ``root_path`` and sets the child
  ``path``/``root_path`` itself. Both ``/mcp/{slug}/`` and ``/mcp/{slug}`` map
  to the child path ``/`` (the only route FastMCP 2.14.7 exposes with
  ``http_app(path="/")``).
* A FastMCP app's lifespan owns AnyIO cancel scopes and context-variable tokens
  and **must be entered and exited in the same task**. Each mounted generation
  therefore runs under a dedicated lifespan-owner task: enter the app lifespan
  via an :class:`~contextlib.AsyncExitStack`, signal readiness, await retirement,
  then close the stack in that same task. The router never enters a mounted
  lifespan itself.
* Retirement drains in-flight *request* traffic before the owner task is asked to
  close the lifespan, so a live swap never closes an app out from under a call
  that is still executing. The adapter closes over its own immutable runtime, so
  an in-flight call keeps talking to the old plugin instance.

What the drain does and does not wait for
-----------------------------------------
Only requests that can carry a JSON-RPC message are drained. A streamable-HTTP
``GET`` opens the server-to-client SSE stream; it never carries a client request
that could invoke a tool, and it stays open for as long as the client wants, so
counting it would make every retirement wait out the whole drain budget for an
idle client. A ``POST`` is always drained -- including the ``202 Accepted`` form
whose response is later delivered on a separate stream -- because the tool call
itself is executed inside the POST handler. The drain budget is paid at most once
per generation: ``PluginManager`` drains just before it seals a retiring runtime,
and the ``mount`` that follows retires the very same generation, so a second wait
would double the cost without protecting anything new.

Concurrency and lock ordering
-----------------------------
The lookup table is replaced only under :attr:`MCPRouter._lock`, so a request
observes either the previous generation or the new one, never a half-built app.
The in-flight counter is read/incremented without awaiting, so the single
asyncio event loop makes lookup-plus-count atomic against retirement.

``mount`` and ``unmount`` are awaited off the request path. ``PluginManager``
holds its stable per-slug lock while invoking the runtime-change hook and calls
``mount`` inside that lock; the mount operation is fast (build the app, spawn the
owner task, await readiness) and the MCP request path never takes the manager's
slug lock, so this cannot deadlock. The only potentially longer step, retirement
drain, waits on requests that need the plugin host semaphore (not the slug lock).

Credential safety: only the slug and a short safe reason are ever logged; plugin
exception text, secrets and tool arguments never reach these log calls.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from fastmcp.server.http import StarletteWithLifespan
from starlette.types import Receive, Scope, Send

from custom_mcp.api.errors import error_response
from custom_mcp.logging import get_logger
from custom_mcp.mcp.adapter import (
    Redact,
    SecretsProvider,
    build_plugin_app,
)
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.manager import is_valid_slug
from custom_mcp.plugins.runtime import PluginRuntime

logger = get_logger("mcp.router")

_NOT_FOUND_MESSAGE = "No such MCP endpoint."
#: Only a bounded prefix of an untrusted slug fragment is ever logged.
_SLUG_LOG_LIMIT = 64

#: Captures a slug's current secret strings for one tool run.
SlugSecrets = Callable[[str], Awaitable[tuple[str, ...]]]
#: Redacts a rendered failure against an already-captured secret set (pure).
SlugRedact = Redact


def _safe_slug(value: object) -> str:
    """Return a bounded, non-secret slug fragment for diagnostics."""
    text = value if isinstance(value, str) else repr(value)
    return text[:_SLUG_LOG_LIMIT]


@dataclass(slots=True)
class _Mounted:
    """Per-generation bookkeeping for one mounted slug.

    ``runtime.mounted`` points at this record rather than the bare Starlette app
    because retirement also needs the owning task and the readiness/retire/
    in-flight coordination events.
    """

    slug: str
    generation: int
    runtime: PluginRuntime
    app: StarletteWithLifespan
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    retire: asyncio.Event = field(default_factory=asyncio.Event)
    drain: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    error: BaseException | None = None
    #: Requests that can carry a JSON-RPC message (POST only; see module docs).
    inflight: int = 0
    #: Whether this generation's single drain budget has already been spent.
    #: A replace/reload drains the retiring generation from ``PluginManager``
    #: (before the seal, so the write ordering is protected) and then retires the
    #: very same generation from :meth:`MCPRouter.mount`; without this flag the
    #: second drain would pay the whole budget again for no added protection.
    #: Every new POST clears it, so a request that starts *after* a drain is still
    #: waited for before the lifespan is closed.
    drain_spent: bool = False


class MCPRouter:
    """An ASGI app that serves each loaded plugin's MCP app under ``/mcp/{slug}``."""

    def __init__(
        self,
        host: PluginHost,
        *,
        mount_path: str = "/mcp",
        drain_timeout: float = 5.0,
        ready_timeout: float = 10.0,
        secret_strings: SlugSecrets | None = None,
        redact: SlugRedact | None = None,
    ) -> None:
        if drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        if ready_timeout <= 0:
            raise ValueError("ready_timeout must be positive")
        self._host = host
        self._mount_path = mount_path.rstrip("/") or "/mcp"
        self._drain_timeout = drain_timeout
        self._ready_timeout = ready_timeout
        self._secret_strings = secret_strings
        self._redact = redact
        self._apps: dict[str, _Mounted] = {}
        self._lock = asyncio.Lock()

    # --- ASGI dispatch --------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # The MCP transports used here are HTTP-only; nothing else is served.
            return

        mounted, child_scope = self._resolve(scope)
        if mounted is None:
            response = error_response(404, "not_found", _NOT_FOUND_MESSAGE)
            await response(scope, receive, send)
            return

        # Only a POST can carry a JSON-RPC message that invokes a tool (including
        # the 202-Accepted form, whose tool call still runs inside this handler).
        # A GET is the long-lived server-to-client SSE stream: it holds no client
        # request, so counting it would make every retirement wait out the whole
        # drain budget for an idle client.
        if scope.get("method") != "POST":
            await mounted.app(child_scope, receive, send)
            return

        mounted.inflight += 1
        # A fresh request re-arms the drain: a request that starts after a drain
        # completed must still be waited for before the app is closed.
        mounted.drain_spent = False
        try:
            await mounted.app(child_scope, receive, send)
        finally:
            mounted.inflight -= 1
            if mounted.inflight <= 0:
                mounted.drain.set()

    def _resolve(self, scope: Scope) -> tuple[_Mounted | None, Scope]:
        """Return the current generation and rewritten child scope, or ``None``.

        Unknown and syntactically invalid slugs both yield ``None`` so the caller
        can emit the uniform JSON 404; an unknown slug must never raise.
        """
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            relative = path[len(root_path) :]
        elif path.startswith(self._mount_path):
            relative = path[len(self._mount_path) :]
        else:
            relative = path
        relative = relative.lstrip("/")
        slug, _, tail = relative.partition("/")

        if not is_valid_slug(slug):
            logger.info(
                "mcp_router_invalid_slug",
                extra={"extra_fields": {"slug": _safe_slug(slug)}},
            )
            return None, scope

        mounted = self._apps.get(slug)
        if mounted is None:
            logger.info(
                "mcp_router_unknown_slug",
                extra={"extra_fields": {"slug": slug}},
            )
            return None, scope

        child_scope = dict(scope)
        child_path = f"/{tail}" if tail else "/"
        effective_root = root_path or self._mount_path
        child_scope["path"] = child_path
        child_scope["root_path"] = f"{effective_root}/{slug}"
        child_scope["raw_path"] = child_path.encode("utf-8")
        return mounted, child_scope

    # --- lifecycle ------------------------------------------------------

    async def mount(self, runtime: PluginRuntime) -> bool:
        """Build and publish ``runtime``'s app, retiring any previous generation.

        Atomically replaces the per-slug lookup entry only after the new app's
        lifespan has been entered and signalled readiness. On readiness failure or
        timeout the slug is left unpublished (the previous generation, if any,
        keeps serving) and a warning is logged.
        """
        slug = runtime.slug
        if not is_valid_slug(slug):
            logger.warning(
                "mcp_mount_invalid_slug",
                extra={"extra_fields": {"slug": _safe_slug(slug)}},
            )
            return False
        if runtime.instance is None:
            # A failed/disabled plugin has no live instance to serve.
            logger.warning(
                "mcp_mount_without_instance",
                extra={"extra_fields": {"slug": slug}},
            )
            return False

        app = build_plugin_app(
            runtime,
            self._host,
            secrets=self._secrets_for(slug),
            redact=self._redact,
        )
        mounted = _Mounted(
            slug=slug,
            generation=runtime.generation,
            runtime=runtime,
            app=app,
        )
        mounted.task = asyncio.create_task(
            self._lifespan_owner(mounted), name=f"mcp-lifespan:{slug}"
        )

        if not await self._await_ready(mounted):
            await self._stop(mounted)
            return False

        runtime.mounted = mounted
        async with self._lock:
            previous = self._apps.get(slug)
            self._apps[slug] = mounted
        if previous is not None and previous is not mounted:
            await self._retire(previous)
        logger.info(
            "mcp_slug_mounted",
            extra={"extra_fields": {"slug": slug, "generation": runtime.generation}},
        )
        return True

    async def unmount(self, slug: str) -> None:
        """Remove ``slug`` from the lookup, drain it and stop its owner task.

        Idempotent: an unknown or invalid slug is a no-op.
        """
        if not is_valid_slug(slug):
            return
        async with self._lock:
            mounted = self._apps.pop(slug, None)
        if mounted is None:
            return
        await self._retire(mounted)
        logger.info("mcp_slug_unmounted", extra={"extra_fields": {"slug": slug}})

    async def drain(self, slug: str) -> None:
        """Wait (bounded) for in-flight requests on the current generation.

        The lifecycle coordinator calls this before shutting a plugin instance
        down, so an in-flight call always completes against a still-live instance
        and any writes it makes land before the replacement snapshots storage. The
        lookup stays published and the generation is not retired.
        """
        if not is_valid_slug(slug):
            return
        mounted = self._apps.get(slug)
        if mounted is not None:
            await self._drain(mounted)

    async def shutdown(self) -> None:
        """Retire every mounted generation; safe to call when empty.

        The whole shutdown shares one drain budget and one close budget instead
        of paying a fresh budget per generation, so the time is bounded by
        ``drain_timeout + ready_timeout`` regardless of how many plugins are
        mounted. That bound is what the container's ``stop_grace_period`` is
        sized against (see ``docker-compose.yml``).
        """
        async with self._lock:
            mounted = list(self._apps.values())
            self._apps.clear()
        loop = asyncio.get_running_loop()
        drain_deadline = loop.time() + self._drain_timeout
        close_deadline = drain_deadline + self._ready_timeout
        for entry in mounted:
            with contextlib.suppress(Exception):
                await self._retire(
                    entry,
                    drain_deadline=drain_deadline,
                    close_deadline=close_deadline,
                )
        logger.info(
            "mcp_router_shutdown",
            extra={"extra_fields": {"count": len(mounted)}},
        )

    async def on_runtime_change(
        self, slug: str, runtime: PluginRuntime | None
    ) -> None:
        """``PluginManager`` runtime-change hook: mount a new generation or unmount.

        Wired from :mod:`custom_mcp.main`; the manager stays decoupled from the
        router and this hook is optional (``None`` for store-only managers).
        """
        if runtime is None:
            await self.unmount(slug)
        else:
            await self.mount(runtime)

    # --- introspection --------------------------------------------------

    def is_mounted(self, slug: str) -> bool:
        """Return whether ``slug`` currently has a published generation."""
        return slug in self._apps

    def current(self, slug: str) -> PluginRuntime | None:
        """Return the currently published runtime for ``slug`` (or ``None``)."""
        entry = self._apps.get(slug)
        return entry.runtime if entry is not None else None

    @property
    def mounted_slugs(self) -> tuple[str, ...]:
        """The published slugs, sorted, for diagnostics and tests."""
        return tuple(sorted(self._apps))

    def _secrets_for(self, slug: str) -> SecretsProvider | None:
        """Bind the injected per-slug secret reader for ``build_plugin_app``."""
        provider = self._secret_strings
        if provider is None:
            return None

        async def _secrets() -> tuple[str, ...]:
            return await provider(slug)

        return _secrets

    # --- owner-task plumbing -------------------------------------------

    async def _lifespan_owner(self, mounted: _Mounted) -> None:
        """Enter, hold and exit one mounted app's lifespan in a single task.

        The lifespan is closed in *this* coroutine, never via ``ensure_future``/
        ``shield``: FastMCP's lifespan creates ``ContextVar`` tokens and AnyIO
        cancel scopes that only the task which entered them may reset, so handing
        the close to a different task raises "created in a different Context".
        """
        stack = AsyncExitStack()
        entered = False
        try:
            try:
                await stack.enter_async_context(mounted.app.lifespan(mounted.app))
                entered = True
            except BaseException as exc:  # record and signal readiness, never raise out
                mounted.error = exc
            finally:
                mounted.ready.set()
            if entered:
                await mounted.retire.wait()
        finally:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _await_ready(self, mounted: _Mounted) -> bool:
        """Wait for lifespan entry; return ``False`` on timeout or failure."""
        try:
            await asyncio.wait_for(mounted.ready.wait(), self._ready_timeout)
        except TimeoutError:
            logger.warning(
                "mcp_mount_ready_timeout",
                extra={"extra_fields": {"slug": mounted.slug}},
            )
            return False
        if mounted.error is not None:
            logger.warning(
                "mcp_mount_lifespan_failed",
                extra={"extra_fields": {"slug": mounted.slug}},
            )
            return False
        return True

    async def _stop(self, mounted: _Mounted) -> None:
        """Retire and join a generation that was never published."""
        mounted.retire.set()
        await self._join(mounted.task, self._ready_timeout)

    async def _retire(
        self,
        mounted: _Mounted,
        *,
        drain_deadline: float | None = None,
        close_deadline: float | None = None,
    ) -> None:
        """Drain in-flight requests, then ask the owner task to close the app."""
        await self._drain(mounted, deadline=drain_deadline)
        mounted.retire.set()
        if close_deadline is None:
            await self._join(mounted.task, self._ready_timeout)
            return
        remaining = max(0.0, close_deadline - asyncio.get_running_loop().time())
        await self._join(mounted.task, remaining)

    async def _drain(self, mounted: _Mounted, *, deadline: float | None = None) -> None:
        """Wait (bounded) for in-flight requests to finish, at most once per generation.

        The budget is charged to the *generation*, not the call. ``PluginManager``
        drains a retiring generation before it seals it (protecting the write
        ordering), and the ``mount`` that follows retires that same generation, so
        a second full wait would only double swap latency. A drain that completes
        with nothing left in flight is free, and a new request re-arms the budget
        (see :meth:`__call__`), so the guarantee is never weakened.

        ``deadline`` lets a caller that retires several generations (shutdown)
        share one budget instead of paying it per generation.
        """
        if mounted.inflight <= 0:
            return
        if mounted.drain_spent:
            # This generation already paid its budget before the seal. Anything
            # still running is sealed against writing, so waiting again cannot
            # preserve a write that the first drain did not already wait for.
            logger.info(
                "mcp_drain_skipped",
                extra={
                    "extra_fields": {
                        "slug": mounted.slug,
                        "inflight": mounted.inflight,
                    }
                },
            )
            return
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + self._drain_timeout
        while mounted.inflight > 0:
            remaining = deadline - loop.time()
            if remaining <= 0:
                mounted.drain_spent = True
                logger.warning(
                    "mcp_drain_timeout",
                    extra={
                        "extra_fields": {
                            "slug": mounted.slug,
                            "inflight": mounted.inflight,
                        }
                    },
                )
                return
            # Re-check after clearing so a decrement that lands between the
            # check and the wait cannot be missed.
            mounted.drain.clear()
            if mounted.inflight <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(mounted.drain.wait(), remaining)
        mounted.drain_spent = True
        logger.info(
            "mcp_drain_complete", extra={"extra_fields": {"slug": mounted.slug}}
        )

    async def _join(self, task: asyncio.Task[None] | None, limit: float) -> None:
        """Wait for an owner task to finish, cancelling it if it overruns."""
        if task is None:
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), limit)
            except TimeoutError:
                logger.warning("mcp_lifespan_close_timeout")
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(task), limit)
        if task.done() and not task.cancelled():
            with contextlib.suppress(BaseException):
                task.exception()


__all__ = ["MCPRouter"]
