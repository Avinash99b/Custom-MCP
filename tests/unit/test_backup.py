"""Unit tests for Phase A backup-archive validation and the restore report.

Phase A must be pure: :func:`validate_archive` either returns fully validated
plugins or raises :class:`InvalidBackupError`; it must never need live state.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from custom_mcp.backup import (
    FORMAT_VERSION,
    InvalidBackupError,
    RestoreReport,
    RestoreResult,
    validate_archive,
)
from custom_mcp.plugins.manifest import CORE_VERSION
from tests.helpers import build_plugin_zip

MAX_ZIP = 20 * 1024 * 1024


def _valid_package(slug: str, *, version: str = "1.0.0") -> bytes:
    return build_plugin_zip(slug=slug, version=version)


def _entry(slug: str, *, version: str = "1.0.0", has_secrets: bool = False) -> dict[str, Any]:
    return {"slug": slug, "version": version, "has_secrets": has_secrets}


def _descriptor(
    *slugs: str,
    encrypted: Any = False,
    format_version: int = FORMAT_VERSION,
) -> dict[str, Any]:
    return {
        "format_version": format_version,
        "created_at": "2026-01-01T00:00:00+00:00",
        "core_version": CORE_VERSION,
        "encrypted": encrypted,
        "plugins": [_entry(slug) for slug in slugs],
    }


def _files(
    slug: str,
    *,
    data: Any = None,
    secrets: Any = None,
    auth_mode: Any = None,
    package: bytes | None = None,
    version: str = "1.0.0",
) -> dict[str, bytes]:
    return {
        "plugin.zip": package if package is not None else _valid_package(slug, version=version),
        "data.json": json.dumps(data if data is not None else {}).encode(),
        "secrets.json": json.dumps(secrets if secrets is not None else {}).encode(),
        "meta.json": json.dumps({"auth_mode": auth_mode, "version": version}).encode(),
    }


def _archive(
    *,
    descriptor: dict[str, Any] | None,
    plugins: dict[str, dict[str, bytes]] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if descriptor is not None:
            archive.writestr("backup.json", json.dumps(descriptor))
        for slug, files in (plugins or {}).items():
            for name, content in files.items():
                archive.writestr(f"plugins/{slug}/{name}", content)
    return buffer.getvalue()


def _validate(data: bytes) -> tuple[Any, ...]:
    return validate_archive(data, max_plugin_zip_bytes=MAX_ZIP, core_version=CORE_VERSION)


# --- valid archives ---------------------------------------------------------


def test_validate_archive_round_trips_entries() -> None:
    data = _archive(
        descriptor=_descriptor("alpha", "beta"),
        plugins={
            "alpha": _files(
                "alpha",
                data={"count": 3, "nested": {"a": [1, 2]}},
                auth_mode="api_key",
            ),
            "beta": _files(
                "beta",
                secrets={"core.api_key": "plain-secret", "token": "abc"},
                auth_mode="api_key",
            ),
        },
    )
    plugins = _validate(data)
    assert [p.slug for p in plugins] == ["alpha", "beta"]
    alpha, beta = plugins
    assert alpha.auth_mode == "api_key"
    assert alpha.data == {"count": 3, "nested": {"a": [1, 2]}}
    assert alpha.secrets == {}
    assert beta.secrets == {"core.api_key": "plain-secret", "token": "abc"}
    assert alpha.package_bytes == _valid_package("alpha")


def test_validate_archive_accepts_reserved_core_keys() -> None:
    data = _archive(
        descriptor=_descriptor("alpha"),
        plugins={"alpha": _files("alpha", secrets={"core.api_key": "x"})},
    )
    (plugin,) = _validate(data)
    assert plugin.secrets == {"core.api_key": "x"}


# --- rejection --------------------------------------------------------------


def test_validate_archive_rejects_empty_input() -> None:
    with pytest.raises(InvalidBackupError):
        _validate(b"")


def test_validate_archive_rejects_non_zip() -> None:
    with pytest.raises(InvalidBackupError):
        _validate(b"not a zip archive")


def test_validate_archive_rejects_missing_descriptor() -> None:
    with pytest.raises(InvalidBackupError):
        _validate(_archive(descriptor=None))


def test_validate_archive_rejects_bad_json() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("backup.json", "{not json")
    with pytest.raises(InvalidBackupError):
        _validate(buffer.getvalue())


def test_validate_archive_rejects_wrong_format_version() -> None:
    data = _archive(
        descriptor=_descriptor("alpha", format_version=2),
        plugins={"alpha": _files("alpha")},
    )
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_encrypted() -> None:
    data = _archive(
        descriptor=_descriptor("alpha", encrypted=True),
        plugins={"alpha": _files("alpha")},
    )
    with pytest.raises(InvalidBackupError, match="Encrypted"):
        _validate(data)


def test_validate_archive_rejects_missing_encryption_marker() -> None:
    descriptor = _descriptor("alpha")
    del descriptor["encrypted"]
    data = _archive(descriptor=descriptor, plugins={"alpha": _files("alpha")})
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_duplicate_slug() -> None:
    descriptor = _descriptor("alpha")
    descriptor["plugins"].append(_entry("alpha"))
    data = _archive(descriptor=descriptor, plugins={"alpha": _files("alpha")})
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_missing_package() -> None:
    files = _files("alpha")
    del files["plugin.zip"]
    data = _archive(descriptor=_descriptor("alpha"), plugins={"alpha": files})
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_bad_package() -> None:
    data = _archive(
        descriptor=_descriptor("alpha"),
        plugins={"alpha": _files("alpha", package=b"not a zip")},
    )
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_invalid_storage_key() -> None:
    data = _archive(
        descriptor=_descriptor("alpha"),
        plugins={"alpha": _files("alpha", data={"bad key": 1})},
    )
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_oversized_value() -> None:
    data = _archive(
        descriptor=_descriptor("alpha"),
        plugins={"alpha": _files("alpha", data={"big": "x" * (1024 * 1024 + 1)})},
    )
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_version_mismatch() -> None:
    files = _files("alpha", version="1.0.0")
    files["meta.json"] = json.dumps({"auth_mode": None, "version": "2.0.0"}).encode()
    data = _archive(descriptor=_descriptor("alpha"), plugins={"alpha": files})
    with pytest.raises(InvalidBackupError):
        _validate(data)


def test_validate_archive_rejects_invalid_metadata() -> None:
    data = _archive(
        descriptor=_descriptor("alpha"),
        plugins={"alpha": _files("alpha", auth_mode=5)},
    )
    with pytest.raises(InvalidBackupError):
        _validate(data)


# --- report shape -----------------------------------------------------------


def test_restore_report_shape() -> None:
    report = RestoreReport(
        format_version=FORMAT_VERSION,
        results=(
            RestoreResult("alpha", "restored", "connected", None),
            RestoreResult("beta", "skipped", "not_connected", None),
            RestoreResult("gamma", "failed", None, "boom"),
        ),
    )
    payload = report.to_dict()
    assert payload["format_version"] == 1
    assert payload["restored"] == ["alpha"]
    assert payload["skipped"] == ["beta"]
    assert payload["failed"] == ["gamma"]
    assert payload["results"][0] == {
        "slug": "alpha",
        "outcome": "restored",
        "status": "connected",
        "detail": None,
    }
