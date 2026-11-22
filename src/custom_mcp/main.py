"""Application factory, HTTP routes and the uvicorn entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from custom_mcp.api.auth import auth_routes
from custom_mcp.api.backup import backup_routes
from custom_mcp.api.errors import (
    error_response,
    http_exception_handler,
    unhandled_exception_handler,
)
from custom_mcp.api.oauth import oauth_routes
from custom_mcp.api.plugins import plugin_routes
from custom_mcp.auth.base import Authenticator
from custom_mcp.auth.middleware import AuthMiddleware
from custom_mcp.auth.ratelimit import LoginRateLimiter
from custom_mcp.auth.secret import SecretAuthenticator
from custom_mcp.auth.sessions import SessionService
from custom_mcp.backup import BackupService, RestoreService
from custom_mcp.config import Settings, get_settings
from custom_mcp.health import HealthScheduler
from custom_mcp.logging import RequestContextMiddleware, configure_logging, get_logger
from custom_mcp.mcp.router import MCPRouter
from custom_mcp.plugins.host import PluginHost
from custom_mcp.plugins.loader import PluginLoader
from custom_mcp.plugins.logs import PluginLogs
from custom_mcp.plugins.manager import PluginManager
from custom_mcp.storage.manager import StorageManager
from custom_mcp.storage.redis_client import (
    AsyncRedis,
    SyncRedis,
    create_async_redis,
    create_sync_redis,
)

logger = get_logger("main")

STATIC_DIR = Path(__file__).parent / "static"

#: Extra time, on top of ``TOOL_TIMEOUT_SECONDS``, that the router may spend
#: draining a retiring generation. ``MCPRouter`` is built with this margin so a
#: tool call that hits its own cap is still fully drained.
DRAIN_MARGIN_SECONDS = 5.0
#: Time the router allows for a mounted app's lifespan to close after draining.
READY_TIMEOUT_SECONDS = 10.0
#: Time a single plugin's ``shutdown`` hook may take. Must match the
#: ``shutdown_timeout`` the app gives :class:`~custom_mcp.plugins.host.PluginHost`;
#: ``PluginManager`` derives its shared teardown budget (3x this) from the host.
PLUGIN_SHUTDOWN_SECONDS = 5.0
#: Budget for tearing every plugin down at exit. It is shared across all slugs,
#: so it covers a few slow hooks rather than scaling with the plugin count.
PLUGIN_TEARDOWN_SECONDS = 3 * PLUGIN_SHUTDOWN_SECONDS


def connection_drain_seconds(tool_timeout: int) -> float:
    """Bound on uvicorn's wait for open connections, before the lifespan stops.

    This is the phase that lets a legitimate in-flight tool call finish. It also
    stops an idle client holding a streamable-HTTP SSE stream open from blocking
    exit: uvicorn force-closes whatever is left when the budget expires.
    """
    return float(tool_timeout) + DRAIN_MARGIN_SECONDS


def lifespan_shutdown_seconds() -> float:
    """Bound on the lifespan shutdown that runs after connections are drained.

    ``MCPRouter.shutdown`` closes every mounted generation under one shared
    close budget, then ``PluginManager.shutdown_all`` tears the plugins down
    under one shared teardown budget. Both are shared across plugins, so this
    does not scale with the number of installed plugins.
    """
    return READY_TIMEOUT_SECONDS + PLUGIN_TEARDOWN_SECONDS


def graceful_shutdown_seconds(tool_timeout: int) -> float:
    """Upper bound on the whole clean-shutdown sequence (both phases).

    The phases run one after the other, so their bounds add:

    1. uvicorn waits for open connections -- :func:`connection_drain_seconds`;
    2. the lifespan shutdown retires the MCP generations and the plugins --
       :func:`lifespan_shutdown_seconds`.

    The container's ``stop_grace_period`` (``docker-compose.yml``) is sized from
    this value with a margin, so the process exits on its own instead of being
    SIGKILLed mid-write.
    """
    return connection_drain_seconds(tool_timeout) + lifespan_shutdown_seconds()


def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


async def _close_owned_async(redis: AsyncRedis) -> None:
    """Close an app-owned async Redis client, never masking the original error."""
    try:
        await redis.aclose()
    except Exception:
        logger.warning("error closing async redis client")


def _close_owned_sync(redis: SyncRedis) -> None:
    """Close an app-owned sync Redis client, always attempted after the async one."""
    try:
        redis.close()
    except Exception:
        logger.warning("error closing sync redis client")


async def _settle(coro: Coroutine[Any, Any, Any]) -> tuple[Any, bool]:
    """Run ``coro`` to completion even while the caller is being cancelled.

    Returns ``(result, was_cancelled)``. ``was_cancelled`` records whether an
    external cancellation was observed while waiting, so the caller can finish
    every owned cleanup step and then re-raise instead of swallowing it.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancelled
        except asyncio.CancelledError:
            # Record the observed external cancellation *before* the done-check:
            # a cancel delivered coincident with the inner task finishing must
            # still propagate, never be swallowed as a clean result.
            cancelled = True
            if task.done():
                return task.result(), cancelled
            continue


async def dashboard(request: Request) -> Response:
    """Serve the authenticated dashboard shell."""
    return HTMLResponse(_read_static("index.html"))


async def login_page(request: Request) -> Response:
    """Serve the login page shell."""
    return HTMLResponse(_read_static("login.html"))


async def healthz(request: Request) -> Response:
    """Report health, verifying Redis connectivity."""
    redis: AsyncRedis = request.app.state.redis_async
    try:
        ok = await redis.ping()
    except Exception:
        logger.warning("redis_health_check_failed", exc_info=True)
        return error_response(503, "unavailable", "Redis is unavailable.")
    if not ok:
        return error_response(503, "unavailable", "Redis is unavailable.")
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


def _routes(mcp_router: MCPRouter) -> list[Route | Mount]:
    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/login", login_page, methods=["GET"]),
        Route("/", dashboard, methods=["GET"]),
        *auth_routes,
        *plugin_routes,
        *backup_routes,
        # The public OAuth callback must precede the ``/mcp`` catchall so a
        # provider callback is never dispatched as MCP traffic.
        *oauth_routes,
        # The MCP router is a raw ASGI app mounted once at ``/mcp``; it resolves
        # ``{slug}`` itself and forwards to the matching FastMCP app. Bearer auth
        # is enforced by ``AuthMiddleware`` before any MCP path reaches here.
        Mount("/mcp", app=mcp_router, name="mcp"),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]


def create_app(
    *,
    settings: Settings | None = None,
    redis_async: AsyncRedis | None = None,
    redis_sync: SyncRedis | None = None,
    authenticator: Authenticator | None = None,
    plugin_host: PluginHost | None = None,
    plugin_loader: PluginLoader | None = None,
    plugin_storage: StorageManager | None = None,
    plugin_logs: PluginLogs | None = None,
    mcp_router: MCPRouter | None = None,
) -> Starlette:
    """Build the ASGI application.

    Settings, both Redis clients and the plugin runtime collaborators can be
    injected for tests. Clients created here are owned by the app and closed on
    shutdown; injected ones are not. A ``PluginHost`` created here owns its
    thread pool and is closed on shutdown; an injected host owns its own. An
    ``mcp_router`` may be injected; otherwise one is built around the host.
    """
    resolved = settings if settings is not None else get_settings()
    owns_async = redis_async is None
    owns_sync = redis_sync is None
    if plugin_loader is not None and plugin_host is None:
        raise ValueError(
            "plugin_loader requires an explicit plugin_host to avoid an orphaned executor"
        )
    owns_host = plugin_host is None
    async_redis = redis_async if redis_async is not None else create_async_redis(resolved.REDIS_URL)
    sync_redis = redis_sync if redis_sync is not None else create_sync_redis(resolved.REDIS_URL)

    sessions = SessionService(
        async_redis,
        ttl_seconds=resolved.session_ttl_seconds,
        cookie_secure=resolved.COOKIE_SECURE,
    )
    auth = authenticator if authenticator is not None else SecretAuthenticator(
        resolved.MCP_SECRET, sessions
    )
    limiter = LoginRateLimiter()

    logs = plugin_logs if plugin_logs is not None else PluginLogs(sync_redis, async_redis)
    storage = (
        plugin_storage
        if plugin_storage is not None
        else StorageManager(sync_redis, async_redis)
    )
    host = (
        plugin_host
        if plugin_host is not None
        else PluginHost(
            worker_threads=resolved.WORKER_THREADS,
            per_plugin_concurrency=resolved.PER_PLUGIN_CONCURRENCY,
            call_timeout=resolved.PLUGIN_CALL_TIMEOUT_SECONDS,
            tool_timeout=resolved.TOOL_TIMEOUT_SECONDS,
            shutdown_timeout=5.0,
            log=logs.write,
        )
    )
    loader = (
        plugin_loader
        if plugin_loader is not None
        else PluginLoader(
            host,
            load_timeout=float(resolved.PLUGIN_CALL_TIMEOUT_SECONDS),
            sanitize_error=logs.redact,
            log=logs.write,
        )
    )
    plugin_manager = PluginManager(
        async_redis,
        data_dir=resolved.DATA_DIR,
        max_plugin_zip_bytes=resolved.MAX_PLUGIN_ZIP_MB * 1024 * 1024,
        base_url=resolved.BASE_URL,
        host=host,
        loader=loader,
        storage=storage,
        logs=logs,
        per_plugin_concurrency=resolved.PER_PLUGIN_CONCURRENCY,
    )
    router = (
        mcp_router
        if mcp_router is not None
        else MCPRouter(
            host,
            # The drain before a swap must outlast any legitimate in-flight tool
            # call so its writes land before the old generation is sealed and the
            # replacement snapshots storage (see ``MCPRouter.drain``). The host
            # already caps a call at ``TOOL_TIMEOUT_SECONDS``; allow a small
            # margin so a call that hits that cap is fully drained too. The budget
            # is charged once per generation, and ``shutdown`` shares a single
            # budget across every mounted generation, so the total stays bounded.
            drain_timeout=float(resolved.TOOL_TIMEOUT_SECONDS) + DRAIN_MARGIN_SECONDS,
            ready_timeout=READY_TIMEOUT_SECONDS,
            secret_strings=logs.redaction_strings,
            redact=logs.redact_secrets,
        )
    )
    backup_service = BackupService(plugin_manager, sync_redis)
    restore_service = RestoreService(plugin_manager, storage, host)
    health_scheduler = HealthScheduler(
        plugin_manager,
        host,
        interval=resolved.PING_INTERVAL_SECONDS,
        timeout=resolved.PING_TIMEOUT_SECONDS,
        threshold=resolved.PING_FAIL_THRESHOLD,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            try:
                ok = await async_redis.ping()
            except Exception:
                logger.error("startup redis check failed")
                raise
            if not ok:
                raise RuntimeError("Redis ping returned a falsy result")
            await plugin_manager.startup()
            # Keep the router in sync with live lifecycle changes from here on,
            # then mount every runtime the manager already loaded during startup
            # (the hook is installed afterwards so startup does not double-mount).
            plugin_manager.on_runtime_change = router.on_runtime_change
            plugin_manager.on_runtime_drain = router.drain
            for summary in await plugin_manager.summaries():
                slug = summary.get("slug")
                if not isinstance(slug, str):
                    continue
                runtime = plugin_manager.runtime_for(slug)
                if runtime is not None and runtime.instance is not None:
                    await router.mount(runtime)
            await health_scheduler.start()
            logger.info("startup complete")
            yield
        finally:
            # Shut down in an order that keeps the total time bounded: retire
            # every mounted MCP generation first, under one shared drain + close
            # budget (``MCPRouter.shutdown``), so an open streamable-HTTP SSE
            # stream or a hung POST cannot multiply the budget by the number of
            # plugins. Then tear the plugins down -- their apps are already gone,
            # so each ``unmount`` is a no-op and pays no second drain. Finally
            # close only the host executor this app owns, then the owned Redis
            # clients. Cleanup settles to completion even if the lifespan task is
            # being cancelled, and a pending cancellation is re-raised after the
            # remaining steps rather than swallowed.
            cancelled: BaseException | None = None
            # Stop health pings before plugins are torn down so no ping can race
            # shutdown or run after the host executor closes.
            try:
                _, was = await _settle(health_scheduler.stop())
                if was and cancelled is None:
                    cancelled = asyncio.CancelledError()
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception:
                logger.warning("error stopping health scheduler")
            try:
                _, was = await _settle(router.shutdown())
                if was and cancelled is None:
                    cancelled = asyncio.CancelledError()
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception:
                logger.warning("error shutting down mcp router")
            try:
                _, was = await _settle(plugin_manager.shutdown_all())
                if was and cancelled is None:
                    cancelled = asyncio.CancelledError()
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except Exception:
                logger.warning("error shutting down plugins")
            if owns_host:
                try:
                    _, was = await _settle(host.aclose())
                    if was and cancelled is None:
                        cancelled = asyncio.CancelledError()
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception:
                    logger.warning("error closing plugin host")
            if owns_async:
                try:
                    _, was = await _settle(_close_owned_async(async_redis))
                    if was and cancelled is None:
                        cancelled = asyncio.CancelledError()
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
            if owns_sync:
                _close_owned_sync(sync_redis)
            if cancelled is not None:
                raise cancelled

    app = Starlette(
        routes=_routes(router),
        lifespan=lifespan,
        exception_handlers={
            HTTPException: http_exception_handler,
            Exception: unhandled_exception_handler,
        },
    )
    app.state.settings = resolved
    app.state.redis_async = async_redis
    app.state.redis_sync = sync_redis
    app.state.sessions = sessions
    app.state.authenticator = auth
    app.state.login_rate_limiter = limiter
    app.state.plugin_manager = plugin_manager
    app.state.plugin_host = host
    app.state.plugin_loader = loader
    app.state.plugin_storage = storage
    app.state.plugin_logs = logs
    app.state.mcp_router = router
    app.state.backup = backup_service
    app.state.restore = restore_service
    app.state.health = health_scheduler

    app.add_middleware(AuthMiddleware, settings=resolved, authenticator=auth)
    app.add_middleware(RequestContextMiddleware)
    return app


def run() -> None:
    """Console-script entry point: configure logging and serve with uvicorn."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
        access_log=False,
        log_level=settings.LOG_LEVEL.lower(),
        # Bound the graceful wait so a client holding an SSE stream open cannot
        # keep the process alive past the container's stop grace period.
        timeout_graceful_shutdown=int(
            graceful_shutdown_seconds(settings.TOOL_TIMEOUT_SECONDS)
        ),
    )


__all__ = [
    "DRAIN_MARGIN_SECONDS",
    "PLUGIN_SHUTDOWN_SECONDS",
    "READY_TIMEOUT_SECONDS",
    "create_app",
    "dashboard",
    "graceful_shutdown_seconds",
    "healthz",
    "login_page",
    "run",
]
