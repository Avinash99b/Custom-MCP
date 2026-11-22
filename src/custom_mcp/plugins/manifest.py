"""Strict parsing and validation of ``manifest.json``.

This is the single source of truth for manifest rules; packaging and the
registry consume :func:`parse_manifest` / :class:`Manifest`.
"""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp_plugin_sdk import AuthMode

CORE_VERSION = "0.1.0"
MANIFEST_FILENAME = "manifest.json"

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
ENTRY_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)
_AUTH_MODES_BY_VALUE = {mode.value: mode for mode in AuthMode}


class ManifestError(ValueError):
    """Raised when ``manifest.json`` is missing or invalid."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ManifestError(f"duplicate manifest key: {key!r}")
        seen.add(key)
    return dict(pairs)


def _version_key(value: str) -> tuple[tuple[int, str], ...]:
    """Return a comparable key for an ASCII ``MAJOR.MINOR.PATCH`` version.

    Components are already validated to be ASCII digits without leading zeros,
    so (length, text) compares numerically without converting to ``int`` and
    without any unbounded / huge-integer conversion.
    """
    return tuple((len(part), part) for part in value.split("."))


def _require_str(data: Mapping[str, Any], key: str) -> str:
    if key not in data:
        raise ManifestError(f"manifest is missing required field {key!r}")
    value = data[key]
    if not isinstance(value, str):
        raise ManifestError(f"manifest field {key!r} must be a string")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(f"manifest field {key!r} must be a string")
    return value


def _parse_auth_modes(data: Mapping[str, Any]) -> tuple[AuthMode, ...]:
    raw = data.get("auth_modes")
    if not isinstance(raw, list) or not raw:
        raise ManifestError("auth_modes must be a nonempty list")
    modes: list[AuthMode] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in _AUTH_MODES_BY_VALUE:
            raise ManifestError(
                f"invalid auth mode {item!r}: expected one of none, api_key, oauth"
            )
        if item in seen:
            raise ManifestError(f"duplicate auth mode: {item!r}")
        seen.add(item)
        modes.append(_AUTH_MODES_BY_VALUE[item])
    if AuthMode.NONE.value in seen and len(seen) > 1:
        raise ManifestError("auth mode 'none' cannot be combined with other modes")
    return tuple(modes)


@dataclass(frozen=True, slots=True)
class Manifest:
    """A validated plugin manifest."""

    name: str
    version: str
    entry: str
    auth_modes: tuple[AuthMode, ...]
    display_name: str
    description: str
    min_core_version: str | None = None
    author: str | None = None
    homepage: str | None = None

    @property
    def entry_module(self) -> str:
        module, _, _ = self.entry.partition(":")
        return module

    @property
    def entry_class(self) -> str:
        _, _, class_name = self.entry.partition(":")
        return class_name

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "version": self.version,
            "entry": self.entry,
            "auth_modes": [mode.value for mode in self.auth_modes],
            "display_name": self.display_name,
            "description": self.description,
            "min_core_version": self.min_core_version,
            "author": self.author,
            "homepage": self.homepage,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        core_version: str = CORE_VERSION,
    ) -> Manifest:
        """Validate a decoded manifest mapping."""
        if not isinstance(data, Mapping):
            raise ManifestError("manifest root must be a JSON object")

        name = _require_str(data, "name")
        if SLUG_RE.fullmatch(name) is None:
            raise ManifestError(
                f"invalid slug {name!r}: expected ^[a-z0-9][a-z0-9-]{{0,39}}$"
            )

        version = _require_str(data, "version")
        if SEMVER_RE.fullmatch(version) is None:
            raise ManifestError(
                f"invalid version {version!r}: expected MAJOR.MINOR.PATCH without leading zeros"
            )

        entry = _require_str(data, "entry")
        if ENTRY_RE.fullmatch(entry) is None:
            raise ManifestError(f"invalid entry {entry!r}: expected 'module:ClassName'")
        module, _, class_name = entry.partition(":")
        if any(keyword.iskeyword(segment) for segment in module.split(".")):
            raise ManifestError(f"invalid entry {entry!r}: Python keyword in module name")
        if keyword.iskeyword(class_name):
            raise ManifestError(f"invalid entry {entry!r}: Python keyword class name")

        auth_modes = _parse_auth_modes(data)

        min_core = _optional_str(data, "min_core_version")
        if min_core is not None:
            if SEMVER_RE.fullmatch(min_core) is None:
                raise ManifestError("min_core_version must be MAJOR.MINOR.PATCH")
            if _version_key(min_core) > _version_key(core_version):
                raise ManifestError(
                    f"package requires core version {min_core} "
                    f"but this core is {core_version}"
                )

        display_name = _optional_str(data, "display_name") or name
        description = _optional_str(data, "description") or ""

        return cls(
            name=name,
            version=version,
            entry=entry,
            auth_modes=auth_modes,
            display_name=display_name,
            description=description,
            min_core_version=min_core,
            author=_optional_str(data, "author"),
            homepage=_optional_str(data, "homepage"),
        )


def parse_manifest(data: bytes, *, core_version: str = CORE_VERSION) -> Manifest:
    """Parse and validate raw ``manifest.json`` bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise ManifestError("manifest must be bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest.json is not valid UTF-8") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ManifestError:
        raise
    except RecursionError as exc:
        raise ManifestError("manifest.json is too deeply nested") from exc
    except ValueError as exc:
        raise ManifestError("manifest.json is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ManifestError("manifest root must be a JSON object")
    return Manifest.from_dict(parsed, core_version=core_version)


__all__ = [
    "CORE_VERSION",
    "ENTRY_RE",
    "MANIFEST_FILENAME",
    "SEMVER_RE",
    "SLUG_RE",
    "Manifest",
    "ManifestError",
    "parse_manifest",
]
