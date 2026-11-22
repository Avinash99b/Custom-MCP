"""Safe ZIP validation and extraction rules."""

from __future__ import annotations

import io
import json
import random
import stat
import warnings
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_mcp.plugins import packaging
from custom_mcp.plugins.packaging import (
    InvalidPluginError,
    PackageTooLargeError,
    ValidatedEntry,
    extract_package,
    validate_package,
)

MAX_SIZE = 20 * 1024 * 1024
PLUGIN_SOURCE = b"from mcp_plugin_sdk import MCPPlugin\n"


def manifest_bytes(**overrides: Any) -> bytes:
    data: dict[str, Any] = {
        "name": "demo",
        "version": "1.0.0",
        "entry": "plugin:Demo",
        "auth_modes": ["none"],
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def build_zip(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    allow_duplicates: bool = False,
) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        if allow_duplicates:
            warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return buffer.getvalue()


def build_zip_infos(pairs: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, content in pairs:
            archive.writestr(info, content)
    return buffer.getvalue()


def valid_zip(prefix: str = "", extra: list[tuple[str, bytes]] | None = None) -> bytes:
    entries = [
        (f"{prefix}manifest.json", manifest_bytes()),
        (f"{prefix}plugin.py", PLUGIN_SOURCE),
    ]
    entries.extend(extra or [])
    return build_zip(entries)


def validate(data: bytes, *, max_size_bytes: int = MAX_SIZE) -> packaging.ValidatedPackage:
    return validate_package(data, max_size_bytes=max_size_bytes)


def corrupt_first_entry_data(zip_bytes: bytes) -> bytes:
    data = bytearray(zip_bytes)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = next(entry for entry in archive.infolist() if not entry.is_dir())
    local = info.header_offset
    name_length = int.from_bytes(data[local + 26 : local + 28], "little")
    extra_length = int.from_bytes(data[local + 28 : local + 30], "little")
    data_start = local + 30 + name_length + extra_length
    data[data_start] ^= 0xFF
    return bytes(data)


_PLUGIN_NAME = "plugin.py"


def _central_header_offset(zip_bytes: bytes, name: str) -> int:
    target = name.encode("utf-8")
    signature = b"PK\x01\x02"
    position = 0
    while True:
        index = zip_bytes.find(signature, position)
        if index < 0:
            raise KeyError(name)
        name_length = int.from_bytes(zip_bytes[index + 28 : index + 30], "little")
        if zip_bytes[index + 46 : index + 46 + name_length] == target:
            return index
        position = index + 4


def _patched_plugin(zip_bytes: bytes, *, usize: int | None = None, crc: int | None = None,
                    csize_delta: int = 0) -> bytes:
    """Patch the plugin.py local+central size and CRC fields in lockstep."""
    data = bytearray(zip_bytes)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = next(entry for entry in archive.infolist() if entry.filename == _PLUGIN_NAME)
    local = info.header_offset
    central = _central_header_offset(zip_bytes, _PLUGIN_NAME)
    if usize is None:
        usize = info.file_size
    if crc is None:
        crc = info.CRC
    csize = info.compress_size + csize_delta
    data[local + 14 : local + 18] = crc.to_bytes(4, "little")
    data[local + 18 : local + 22] = csize.to_bytes(4, "little")
    data[local + 22 : local + 26] = usize.to_bytes(4, "little")
    data[central + 16 : central + 20] = crc.to_bytes(4, "little")
    data[central + 20 : central + 24] = csize.to_bytes(4, "little")
    data[central + 24 : central + 28] = usize.to_bytes(4, "little")
    return bytes(data)


def _zip_with_plugin_payload(payload: bytes) -> bytes:
    return build_zip([("manifest.json", manifest_bytes()), (_PLUGIN_NAME, payload)])


def _patch_local_name(zip_bytes: bytes, name: str, new_name: str) -> bytes:
    assert len(name) == len(new_name)
    data = bytearray(zip_bytes)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = next(entry for entry in archive.infolist() if entry.filename == name)
    start = info.header_offset + 30
    data[start : start + len(new_name)] = new_name.encode("utf-8")
    return bytes(data)


def _set_efs(zip_bytes: bytes, name: str, *, local: bool, central: bool) -> bytes:
    data = bytearray(zip_bytes)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        info = next(entry for entry in archive.infolist() if entry.filename == name)
    if local:
        offset = info.header_offset + 6
        flags = int.from_bytes(data[offset : offset + 2], "little") | 0x0800
        data[offset : offset + 2] = flags.to_bytes(2, "little")
    if central:
        offset = _central_header_offset(zip_bytes, name) + 8
        flags = int.from_bytes(data[offset : offset + 2], "little") | 0x0800
        data[offset : offset + 2] = flags.to_bytes(2, "little")
    return bytes(data)


# --- happy paths ------------------------------------------------------------


def test_valid_root_package() -> None:
    data = valid_zip()
    result = validate(data)
    assert result.manifest.name == "demo"
    assert result.root_prefix == ""
    assert result.warnings == ()
    assert result.zip_bytes == data
    assert {entry.path for entry in result.entries} == {"manifest.json", "plugin.py"}


def test_valid_nested_package_with_directory_entry() -> None:
    data = build_zip(
        [
            ("pkg/", b""),
            ("pkg/manifest.json", manifest_bytes()),
            ("pkg/sub/plugin.py", PLUGIN_SOURCE),
        ]
    )
    result = validate(data)
    assert result.root_prefix == "pkg/"
    assert {entry.path for entry in result.entries if not entry.is_dir} == {
        "manifest.json",
        "sub/plugin.py",
    }


def test_root_manifest_allows_extra_top_level_dir() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("other/helper.py", b"VALUE = 1\n"),
        ]
    )
    result = validate(data)
    assert result.root_prefix == ""
    assert {entry.path for entry in result.entries} == {"manifest.json", "other/helper.py"}


# --- layout -----------------------------------------------------------------


def test_missing_manifest_rejected() -> None:
    data = build_zip([("plugin.py", PLUGIN_SOURCE)])
    with pytest.raises(InvalidPluginError, match=r"manifest\.json"):
        validate(data)


def test_multiple_top_level_dirs_rejected() -> None:
    data = build_zip(
        [
            ("a/manifest.json", manifest_bytes()),
            ("b/plugin.py", PLUGIN_SOURCE),
        ]
    )
    with pytest.raises(InvalidPluginError, match="top-level directory"):
        validate(data)


def test_empty_archive_rejected() -> None:
    data = build_zip([])
    with pytest.raises(InvalidPluginError, match="empty"):
        validate(data)


def test_non_zip_rejected() -> None:
    with pytest.raises(InvalidPluginError, match="not a valid ZIP"):
        validate(b"this is not a zip file")


# --- unsafe paths -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("../evil.py", "unsafe path"),
        ("/etc/passwd", "absolute path"),
        ("a\\b.py", "backslash"),
        ("C:evil.py", "drive-qualified"),
        ("a/./b.py", "unsafe path"),
    ],
)
def test_unsafe_paths_rejected(name: str, match: str) -> None:
    data = build_zip([("manifest.json", manifest_bytes()), (name, b"x")])
    with pytest.raises(InvalidPluginError, match=match):
        validate(data)


def test_raw_nul_name_rejected() -> None:
    placeholder = "evil-module.py"
    target = placeholder.replace("-", "\x00")
    base = build_zip([("manifest.json", manifest_bytes()), (placeholder, b"x")])
    data = base.replace(placeholder.encode("ascii"), target.encode("ascii"))
    assert b"\x00" in data
    with pytest.raises(InvalidPluginError, match="NUL byte"):
        validate(data)


def test_symlink_rejected() -> None:
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = build_zip_infos(
        [
            (zipfile.ZipInfo("manifest.json"), manifest_bytes()),
            (info, b"target"),
        ]
    )
    with pytest.raises(InvalidPluginError, match="symbolic link"):
        validate(data)


def test_special_file_rejected() -> None:
    info = zipfile.ZipInfo("dev")
    info.external_attr = (stat.S_IFCHR | 0o644) << 16
    data = build_zip_infos(
        [
            (zipfile.ZipInfo("manifest.json"), manifest_bytes()),
            (info, b""),
        ]
    )
    with pytest.raises(InvalidPluginError, match="special file"):
        validate(data)


# --- archive integrity ------------------------------------------------------


def test_unsupported_compression_rejected() -> None:
    data = build_zip(
        [("manifest.json", manifest_bytes()), ("plugin.py", PLUGIN_SOURCE)],
        compression=zipfile.ZIP_BZIP2,
    )
    with pytest.raises(InvalidPluginError, match="unsupported compression"):
        validate(data)


def test_bad_crc_rejected() -> None:
    data = corrupt_first_entry_data(valid_zip())
    with pytest.raises(InvalidPluginError, match=r"integrity|corrupt|truncated"):
        validate(data)


def test_too_many_entries_rejected() -> None:
    entries = [("manifest.json", manifest_bytes())]
    entries.extend((f"f{index}.txt", b"x") for index in range(2000))
    data = build_zip(entries, compression=zipfile.ZIP_STORED)
    with pytest.raises(InvalidPluginError, match="more than 2000"):
        validate(data)


def test_per_entry_ratio_rejected() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("big.bin", b"\x00" * (1024 * 1024)),
        ]
    )
    with pytest.raises(InvalidPluginError, match="ratio"):
        validate(data)


def test_total_uncompressed_ratio_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging, "MAX_UNCOMPRESSED_RATIO", 0)
    with pytest.raises(InvalidPluginError, match="5x"):
        validate(valid_zip())


def test_total_expansion_uses_configured_limit_not_zip_length() -> None:
    rng = random.Random(20240101)
    payload = bytes(rng.getrandbits(1) for _ in range(64 * 1024))
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("plugin.py", PLUGIN_SOURCE),
            ("blob.bin", payload),
        ]
    )
    result = validate(data)  # default configured limit is 20 MiB
    assert result.manifest.name == "demo"
    # The archive expands past 5x its own length but stays within 5x the
    # configured limit, so it must be accepted.
    assert result.uncompressed_size > 5 * len(data)
    assert result.uncompressed_size <= 5 * MAX_SIZE


def test_compressed_size_limit_enforced() -> None:
    with pytest.raises(PackageTooLargeError, match="size limit"):
        validate(valid_zip(), max_size_bytes=10)


# --- duplicates and conflicts ----------------------------------------------


def test_duplicate_entry_rejected() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("a.txt", b"one"),
            ("a.txt", b"two"),
        ],
        allow_duplicates=True,
    )
    with pytest.raises(InvalidPluginError, match="duplicate entry"):
        validate(data)


def test_file_used_as_directory_rejected() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("a", b"x"),
            ("a/b", b"y"),
        ]
    )
    with pytest.raises(InvalidPluginError, match="file/directory conflict"):
        validate(data)


def test_file_directory_same_path_rejected() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("a", b"x"),
            ("a/", b""),
        ]
    )
    with pytest.raises(InvalidPluginError, match="file/directory conflict"):
        validate(data)


# --- import lint ------------------------------------------------------------


def test_import_lint_classification() -> None:
    source = (
        b"import os\n"
        b"import requests\n"
        b"from mcp_plugin_sdk import MCPPlugin\n"
        b"from . import local\n"
        b"import helper\n"
        b"import numpy\n"
    )
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("plugin.py", source),
            ("helper.py", b"VALUE = 1\n"),
        ]
    )
    result = validate(data)
    assert any("numpy" in warning for warning in result.warnings)
    assert not any("requests" in warning for warning in result.warnings)
    assert not any("mcp_plugin_sdk" in warning for warning in result.warnings)
    assert not any("helper" in warning for warning in result.warnings)


def test_syntax_error_is_warning_not_rejection() -> None:
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("plugin.py", b"def broken(:\n"),
        ]
    )
    result = validate(data)
    assert any("syntax error" in warning for warning in result.warnings)


# --- extraction -------------------------------------------------------------


def test_extract_writes_files(tmp_path: Path) -> None:
    result = validate(valid_zip())
    destination = tmp_path / "plugin"
    extract_package(result, destination)
    assert (destination / "manifest.json").read_bytes() == manifest_bytes()
    assert (destination / "plugin.py").read_bytes() == PLUGIN_SOURCE
    assert not (tmp_path / "packages").exists()


def test_extract_refuses_non_empty_destination(tmp_path: Path) -> None:
    result = validate(valid_zip())
    destination = tmp_path / "plugin"
    destination.mkdir()
    (destination / "existing.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(InvalidPluginError, match="must be empty"):
        extract_package(result, destination)


def test_validation_creates_no_files(tmp_path: Path) -> None:
    data = build_zip([("manifest.json", manifest_bytes()), ("../evil.py", b"x")])
    with pytest.raises(InvalidPluginError):
        validate(data)
    assert list(tmp_path.iterdir()) == []


def test_raw_nul_detected_with_preamble() -> None:
    placeholder = "evil-module.py"
    target = placeholder.replace("-", "\x00")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes())
        archive.writestr(placeholder, b"x")
    body = buffer.getvalue().replace(placeholder.encode("ascii"), target.encode("ascii"))
    # A self-extracting-style preamble containing a fake EOCD must not hide the
    # raw filename NUL from the parsed central directory.
    data = b"PK\x05\x06" + b"\x00" * 18 + body
    assert b"\x00" in data
    with pytest.raises(InvalidPluginError, match="NUL byte"):
        validate(data)


def test_local_only_nul_name_rejected() -> None:
    data = bytearray(valid_zip())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = next(entry for entry in archive.infolist() if entry.filename == "plugin.py")
    data[info.header_offset + 30] = 0  # first byte of the local filename
    with pytest.raises(InvalidPluginError, match="NUL byte"):
        validate(bytes(data))


class _UnseekableSink(io.RawIOBase):
    """Write-only sink so ``zipfile`` emits data descriptors (flag bit 3)."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: Any) -> int:
        self._buffer.extend(data)
        return len(data)

    def tell(self) -> int:
        return len(self._buffer)

    def getvalue(self) -> bytes:
        return bytes(self._buffer)


def test_data_descriptor_zip_is_accepted() -> None:
    sink = _UnseekableSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes())
        archive.writestr("plugin.py", PLUGIN_SOURCE)
    data = sink.getvalue()
    assert any(entry.flag_bits & 0x08 for entry in zipfile.ZipFile(io.BytesIO(data)).infolist())
    result = validate(data)
    assert result.manifest.name == "demo"


def test_force_zip64_small_entry_accepted() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open(zipfile.ZipInfo("manifest.json"), "w", force_zip64=True) as handle:
            handle.write(manifest_bytes())
        archive.writestr("plugin.py", PLUGIN_SOURCE)
    result = validate(buffer.getvalue())
    assert result.manifest.name == "demo"


def test_prefix_crc_declared_size_attack_rejected() -> None:
    prefix = b"VALUE = 1\n"
    payload = prefix + b"Z" * (1024 * 1024)
    data = _zip_with_plugin_payload(payload)
    patched = _patched_plugin(
        data, usize=len(prefix), crc=zlib.crc32(prefix) & 0xFFFFFFFF
    )
    with pytest.raises(InvalidPluginError, match=r"expands beyond|declared size"):
        validate(patched, max_size_bytes=8192)


def test_prefix_crc_declared_zero_attack_rejected() -> None:
    data = _zip_with_plugin_payload(b"nonempty payload behind a zero declaration\n")
    patched = _patched_plugin(data, usize=0)
    with pytest.raises(InvalidPluginError, match=r"expands beyond|declared size"):
        validate(patched, max_size_bytes=8192)


def test_real_crc_mismatch_rejected() -> None:
    data = _zip_with_plugin_payload(PLUGIN_SOURCE)
    patched = _patched_plugin(data, crc=(zlib.crc32(PLUGIN_SOURCE) & 0xFFFFFFFF) ^ 1)
    with pytest.raises(InvalidPluginError, match="integrity"):
        validate(patched)


def test_truncated_deflate_stream_rejected() -> None:
    data = _zip_with_plugin_payload(b"compressible content " * 256)
    patched = _patched_plugin(data, csize_delta=-4)
    with pytest.raises(InvalidPluginError, match=r"truncated|corrupt|trailing"):
        validate(patched)


def test_inflated_compressed_size_rejected() -> None:
    data = _zip_with_plugin_payload(PLUGIN_SOURCE)
    patched = _patched_plugin(data, csize_delta=4)
    with pytest.raises(InvalidPluginError):
        validate(patched)


def test_directory_entry_with_data_rejected() -> None:
    data = build_zip(
        [("manifest.json", manifest_bytes()), ("bigdir/", b"Z" * (128 * 1024))]
    )
    with pytest.raises(InvalidPluginError, match="directory entry contains data"):
        validate(data)


def test_empty_deflated_directory_accepted() -> None:
    data = build_zip([("manifest.json", manifest_bytes()), ("pkg/", b"")])
    result = validate(data)
    assert result.manifest.name == "demo"


def test_deeply_nested_manifest_in_package_rejected() -> None:
    nested = b"[" * 100_000 + b"0" + b"]" * 100_000
    data = build_zip([("manifest.json", nested), ("plugin.py", PLUGIN_SOURCE)])
    with pytest.raises(InvalidPluginError, match=r"ratio|too deeply nested|JSON object"):
        validate(data)


def test_total_cap_follows_configured_limit() -> None:
    rng = random.Random(20240202)
    payload = bytes(rng.getrandbits(1) for _ in range(256 * 1024))
    data = build_zip(
        [
            ("manifest.json", manifest_bytes()),
            ("plugin.py", PLUGIN_SOURCE),
            ("blob.bin", payload),
        ]
    )
    declared = len(manifest_bytes()) + len(PLUGIN_SOURCE) + len(payload)
    reject_size = declared // packaging.MAX_UNCOMPRESSED_RATIO - 1024
    accept_size = declared // packaging.MAX_UNCOMPRESSED_RATIO + 1024
    assert len(data) <= reject_size

    with pytest.raises(InvalidPluginError, match="5x the configured"):
        validate(data, max_size_bytes=reject_size)
    result = validate(data, max_size_bytes=accept_size)
    assert result.manifest.name == "demo"


def test_total_cap_has_no_fixed_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aggregate cap is 5x the configured limit, with no fixed ceiling."""
    infos = [
        SimpleNamespace(filename=f"f{index}.bin", file_size=8 * 1024 * 1024, header_offset=0)
        for index in range(14)
    ]
    monkeypatch.setattr(
        packaging,
        "_decode_entry",
        lambda data, info, *, data_limit, cap, collect: (info.file_size, None),
    )
    archive = SimpleNamespace(start_dir=0)
    collected, used = packaging._verify_entries(
        b"",
        archive,  # type: ignore[arg-type]
        infos,  # type: ignore[arg-type]
        max_size_bytes=30 * 1024 * 1024,
        collect_names=set(),
    )
    assert used == 112 * 1024 * 1024  # 112 MiB > 100 MiB, within 5 * 30 MiB
    assert collected == {}

    with pytest.raises(InvalidPluginError, match="5x the configured"):
        packaging._verify_entries(
            b"",
            archive,  # type: ignore[arg-type]
            infos,  # type: ignore[arg-type]
            max_size_bytes=20 * 1024 * 1024,
            collect_names=set(),
        )


def test_local_filename_mismatch_rejected() -> None:
    data = _patch_local_name(valid_zip(), "plugin.py", "plug1n.py")
    with pytest.raises(InvalidPluginError, match="local filename"):
        validate(data)


def test_local_filename_traversal_mismatch_rejected() -> None:
    data = _patch_local_name(valid_zip(), "plugin.py", "../../x.y")
    with pytest.raises(InvalidPluginError):
        validate(data)


def test_inconsistent_efs_flag_rejected() -> None:
    data = _set_efs(valid_zip(), "plugin.py", local=True, central=False)
    with pytest.raises(InvalidPluginError, match="encoding flag"):
        validate(data)


def test_utf8_filename_accepted() -> None:
    data = build_zip([("manifest.json", manifest_bytes()), ("héllo.py", PLUGIN_SOURCE)])
    result = validate(data)
    assert "héllo.py" in {entry.path for entry in result.entries}


def test_invalid_utf8_entry_name_normalized() -> None:
    base = _set_efs(valid_zip(), "plugin.py", local=True, central=True)
    central = _central_header_offset(base, "plugin.py")
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        info = next(entry for entry in archive.infolist() if entry.filename == "plugin.py")
    data = bytearray(base)
    data[info.header_offset + 30] = 0xFF
    data[central + 46] = 0xFF
    with pytest.raises(InvalidPluginError, match="undecodable entry name"):
        validate(bytes(data))


def test_extract_rejects_symlink_destination(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    result = validate(valid_zip())
    with pytest.raises(InvalidPluginError, match="symbolic link"):
        extract_package(result, link)
    assert list(real.iterdir()) == []


def test_extract_rejects_dangling_symlink_destination(tmp_path: Path) -> None:
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "missing")
    result = validate(valid_zip())
    with pytest.raises(InvalidPluginError, match="symbolic link"):
        extract_package(result, link)


def test_extract_rejects_forged_escaping_file(tmp_path: Path) -> None:
    result = validate(valid_zip())
    forged = replace(
        result,
        entries=(
            ValidatedEntry(path="../evil.py", is_dir=False, file_size=1, zip_name="plugin.py"),
        ),
    )
    dest = tmp_path / "dest"
    with pytest.raises(InvalidPluginError):
        extract_package(forged, dest)
    assert not dest.exists()
    assert not (tmp_path / "evil.py").exists()


def test_extract_rejects_forged_directory_traversal(tmp_path: Path) -> None:
    result = validate(valid_zip())
    forged = replace(
        result,
        entries=(ValidatedEntry(path="../evil", is_dir=True, file_size=0, zip_name=""),),
    )
    dest = tmp_path / "dest"
    with pytest.raises(InvalidPluginError):
        extract_package(forged, dest)
    assert not dest.exists()
