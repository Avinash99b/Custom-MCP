"""Isolated loading and unloading of synchronous plugin packages.

Import and construction both run through :class:`PluginHost` workers. A load
that times out is marked failed and cleaned up; a late worker may not resurrect
the isolated module namespace or become the runtime's instance.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
import threading
import types
from collections.abc import Awaitable, Callable
from pathlib import Path

from custom_mcp.logging import get_logger
from custom_mcp.plugins.host import (
    LogCallback,
    PluginCallError,
    PluginHost,
    _HostContractError,
)
from custom_mcp.plugins.runtime import LoadState, PluginRuntime
from mcp_plugin_sdk import MCPPlugin, PluginContext

logger = get_logger("plugins.loader")

SanitizeError = Callable[[str, str], str | Awaitable[str]]


class PluginLoadError(Exception):
    """Optional failure type for callers that prefer an exception over state."""

    def __init__(self, slug: str, safe_message: str) -> None:
        self.slug = slug
        self.safe_message = safe_message
        super().__init__(safe_message)


class _LoaderContractError(_HostContractError):
    """A safe, loader-domain contract failure owned by the host contract type."""


class _AbandonedImport(Exception):
    pass


def _module_prefix(slug: str) -> str:
    return f"mcp_plugin_{slug}"


def _remove_modules(prefix: str) -> None:
    """Remove only ``prefix`` and its dotted children, never ``prefix-*``."""
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            sys.modules.pop(name, None)


def _remove_created(created: dict[str, types.ModuleType]) -> None:
    """Remove the exact module objects installed by one load attempt."""
    for name, module in list(created.items()):
        if sys.modules.get(name) is module:
            sys.modules.pop(name, None)


def _ensure_init_file(package_dir: Path) -> None:
    if package_dir.is_symlink() or not package_dir.is_dir():
        raise OSError("plugin package directory is missing or unsafe")
    init_path = package_dir / "__init__.py"
    if not init_path.exists():
        init_path.write_text("", encoding="utf-8")


def _install_package(
    prefix: str, package_dir: Path, abandon: threading.Event
) -> types.ModuleType:
    if abandon.is_set():
        raise _AbandonedImport
    init_path = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        prefix,
        init_path,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("plugin package could not be initialized")
    module = importlib.util.module_from_spec(spec)
    if abandon.is_set():
        raise _AbandonedImport
    sys.modules[prefix] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(prefix) is module:
            sys.modules.pop(prefix, None)
        raise
    return module


def _import_entry(
    package_dir: Path,
    prefix: str,
    module_name: str,
    class_name: str,
    abandon: threading.Event,
    guard: threading.Lock,
    created: dict[str, types.ModuleType],
) -> type[MCPPlugin]:
    if abandon.is_set():
        raise _AbandonedImport
    try:
        package_module = _install_package(prefix, package_dir, abandon)
        with guard:
            created[prefix] = package_module
        if abandon.is_set():
            raise _AbandonedImport
        module = importlib.import_module(f"{prefix}.{module_name}")
        with guard:
            created[module.__name__] = module
        entry = getattr(module, class_name)
        if (
            not isinstance(entry, type)
            or entry is MCPPlugin
            or not issubclass(entry, MCPPlugin)
        ):
            raise _LoaderContractError(
                "The manifest entry is not an MCPPlugin subclass."
            )
        return entry
    finally:
        with guard:
            if abandon.is_set():
                _remove_created(created)
                raise _AbandonedImport


def _construct_instance(
    entry: type[MCPPlugin],
    context: PluginContext,
    abandon: threading.Event,
    guard: threading.Lock,
    prefix: str,
    created: dict[str, types.ModuleType],
) -> MCPPlugin:
    try:
        instance = entry(context)
        if not isinstance(instance, MCPPlugin):
            raise _LoaderContractError(
                "Plugin constructor did not return an MCPPlugin."
            )
        # ``ctx`` may be a plugin property; this identity check must stay in
        # the bounded constructor worker, never on the event loop.
        if getattr(instance, "ctx", None) is not context:
            raise _LoaderContractError(
                "Plugin instance is not bound to its context."
            )
    except BaseException:
        with guard:
            if abandon.is_set():
                _remove_created(created)
        raise
    with guard:
        if abandon.is_set():
            _remove_created(created)
            raise _AbandonedImport
    return instance


class PluginLoader:
    """Load and unload one plugin per runtime generation."""

    def __init__(
        self,
        host: PluginHost,
        *,
        shutdown_timeout: float | None = None,
        load_timeout: float = 15.0,
        sanitize_error: SanitizeError | None = None,
        log: LogCallback | None = None,
    ) -> None:
        self._host = host
        self._shutdown_timeout = (
            host.shutdown_timeout if shutdown_timeout is None else shutdown_timeout
        )
        self._load_timeout = load_timeout
        self._sanitize_error = sanitize_error
        self._log = log
        # Exact module identities per runtime attempt, so a late worker from an
        # old generation cannot delete a newer generation's modules.
        self._attempts: dict[str, dict[str, types.ModuleType]] = {}

    async def load(
        self,
        runtime: PluginRuntime,
        package_dir: Path,
        *,
        raise_on_error: bool = False,
    ) -> LoadState:
        """Populate ``runtime`` from extracted package files.

        Normalized failures leave the runtime visible as ``LOAD_FAILED`` with a
        safe ``load_error``. With ``raise_on_error=True`` a safe
        :class:`PluginLoadError` is raised instead. Cancellation is propagated
        after cleanup.
        """
        prefix = _module_prefix(runtime.slug)
        runtime.load_state = LoadState.LOADING
        runtime.load_error = None
        runtime.instance = None
        runtime.tools = []
        runtime.auth_methods = []
        _remove_modules(prefix)

        attempt = self._attempts.setdefault(runtime.runtime_id, {})
        attempt.clear()
        abandon = threading.Event()
        guard = threading.Lock()

        try:
            await asyncio.to_thread(_ensure_init_file, package_dir)
            entry = await self._host.call_callable(
                runtime,
                _import_entry,
                package_dir,
                prefix,
                runtime.manifest.entry_module,
                runtime.manifest.entry_class,
                abandon,
                guard,
                attempt,
                timeout=self._load_timeout,
            )
            instance = await self._host.call_callable(
                runtime,
                _construct_instance,
                entry,
                runtime.context,
                abandon,
                guard,
                prefix,
                attempt,
                timeout=self._load_timeout,
            )
            if abandon.is_set():
                raise _AbandonedImport

            runtime.instance = instance
            auth_methods = await self._host.call(
                runtime, "auth_methods", timeout=self._load_timeout
            )
            if set(auth_methods) != set(runtime.manifest.auth_modes):
                raise _LoaderContractError(
                    "Plugin auth modes do not match the manifest."
                )
            # ``host.call(list_tools)`` already validated names, uniqueness and
            # JSON Schema inside the worker; do not re-validate on the loop.
            runtime.tools = await self._host.call(
                runtime, "list_tools", timeout=self._load_timeout
            )
            runtime.auth_methods = list(auth_methods)
            runtime.load_state = LoadState.LOADED
            runtime.load_error = None
            return runtime.load_state
        except BaseException as exc:
            safe = await self._fail(runtime, exc, abandon, guard, attempt)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if raise_on_error:
                raise PluginLoadError(runtime.slug, safe) from None
            return runtime.load_state

    async def unload(self, runtime: PluginRuntime) -> None:
        """Best-effort bounded shutdown, then drop code and cached state."""
        prefix = _module_prefix(runtime.slug)
        attempt = self._attempts.pop(runtime.runtime_id, {})
        runtime.load_state = LoadState.UNLOADING
        try:
            await self._shutdown_instance(runtime)
        finally:
            runtime.instance = None
            runtime.tools = []
            runtime.auth_methods = []
            # The runtime is retiring under the coordinator's slug lock, so the
            # exact prefix can be cleared without touching ``prefix-other``.
            _remove_modules(prefix)
            attempt.clear()

    async def _shutdown_instance(self, runtime: PluginRuntime) -> None:
        """Best-effort bounded host-mediated shutdown, never overriding status."""
        if runtime.instance is None:
            return
        try:
            await asyncio.wait_for(
                self._host.call(
                    runtime,
                    "shutdown",
                    timeout=self._shutdown_timeout,
                ),
                timeout=self._shutdown_timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "plugin_shutdown_timed_out",
                extra={"extra_fields": {"slug": runtime.slug}},
            )
            await self._notify_log(runtime.slug, "error", "Plugin shutdown timed out.")
        except PluginCallError as exc:
            logger.warning(
                "plugin_shutdown_failed",
                extra={
                    "extra_fields": {
                        "slug": runtime.slug,
                        "kind": exc.kind,
                        "message": exc.detail_safe,
                    }
                },
            )
            await self._notify_log(runtime.slug, "error", exc.detail_safe)
        except Exception:
            logger.warning(
                "plugin_shutdown_failed",
                extra={"extra_fields": {"slug": runtime.slug}},
            )
            await self._notify_log(runtime.slug, "error", "Plugin shutdown failed.")

    async def _fail(
        self,
        runtime: PluginRuntime,
        exc: BaseException,
        abandon: threading.Event,
        guard: threading.Lock,
        attempt: dict[str, types.ModuleType],
    ) -> str:
        safe = self._safe_error(exc)
        runtime.load_error = await self._sanitize(runtime.slug, safe)
        with guard:
            abandon.set()
        try:
            # A successfully constructed instance may still own resources even
            # though auth/list failed. Shut it down while its modules are still
            # installed, then clear references and the isolated namespace.
            await self._shutdown_instance(runtime)
        finally:
            with guard:
                # At timeout/return-failure no other generation can be live yet
                # (manager holds the slug lock), so a full exact-prefix cleanup
                # is safe. Late worker cleanup remains identity-scoped.
                _remove_modules(_module_prefix(runtime.slug))
                attempt.clear()
            runtime.instance = None
            runtime.tools = []
            runtime.auth_methods = []
            runtime.load_state = LoadState.LOAD_FAILED
        logger.warning(
            "plugin_load_failed",
            extra={"extra_fields": {"slug": runtime.slug, "message": runtime.load_error}},
        )
        await self._notify_log(runtime.slug, "error", runtime.load_error)
        return runtime.load_error

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        if isinstance(exc, PluginCallError):
            return exc.detail_safe
        if isinstance(exc, (_LoaderContractError, _AbandonedImport)):
            return str(exc)
        return "Plugin failed to load."

    async def _sanitize(self, slug: str, message: str) -> str:
        if self._sanitize_error is None:
            return message
        result = self._sanitize_error(slug, message)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    async def _notify_log(self, slug: str, level: str, message: str) -> None:
        if self._log is None:
            return
        try:
            await self._log(slug, level, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "plugin_log_callback_failed",
                extra={"extra_fields": {"slug": slug}},
            )


__all__ = ["PluginLoadError", "PluginLoader"]
