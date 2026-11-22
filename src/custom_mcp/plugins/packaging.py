"""Safe validation and extraction of plugin ZIP packages.

Validation is read-only and does not trust declared sizes or ``ZipFile`` read
truncation: every entry's raw compressed range is decoded with a bounded,
chunked inflater so the *actual* expanded byte count and CRC32 are compared to
the declared values, the per-entry 100:1 and configured 5x limits are enforced
on the real counts, and header/metadata inconsistencies are rejected before any
file is written.

ZIP format behaviour follows the PKWARE APPNOTE (6.3.10), sections 4.3.9
(data descriptors), 4.3.9.2 (ZIP64 descriptors) and 4.5.3 (local ZIP64 extra).
"""

from __future__ import annotations

import ast
import io
import os
import re
import stat
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from custom_mcp.plugins.manifest import (
    CORE_VERSION,
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    parse_manifest,
)

MAX_ENTRY_COUNT = 2000
MAX_UNCOMPRESSED_RATIO = 5
MAX_ENTRY_RATIO = 100
_CHUNK_SIZE = 64 * 1024

_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ALLOWED_THIRD_PARTY = frozenset(
    {"requests", "urllib3", "certifi", "charset_normalizer", "idna"}
)
_SDK_MODULE = "mcp_plugin_sdk"
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_LOCAL_SIG = b"PK\x03\x04"
_DATA_DESCRIPTOR_SIG = b"PK\x07\x08"
_ENCRYPTED_FLAG = 0x01
_DATA_DESCRIPTOR_FLAG = 0x08
_EFS_FLAG = 0x0800
_ZIP64_SENTINEL = 0xFFFFFFFF
_ZIP64_EXTRA_ID = 0x0001


class InvalidPluginError(ValueError):
    """A plugin package failed validation; the message is safe for clients."""


class PackageTooLargeError(InvalidPluginError):
    """The package exceeds the configured compressed size limit."""


@dataclass(frozen=True, slots=True)
class ValidatedEntry:
    """A validated archive entry, path relative to the plugin root."""

    path: str
    is_dir: bool
    file_size: int
    zip_name: str


@dataclass(frozen=True, slots=True)
class ValidatedPackage:
    """The result of validating a plugin ZIP."""

    manifest: Manifest
    entries: tuple[ValidatedEntry, ...]
    root_prefix: str
    warnings: tuple[str, ...]
    compressed_size: int
    uncompressed_size: int
    zip_bytes: bytes = field(repr=False)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _normalize_entry_path(name: str, *, is_dir: bool) -> str:
    if not name:
        raise InvalidPluginError("package contains an entry with an empty name")
    if name.startswith(("/", "//")):
        raise InvalidPluginError(f"package contains an absolute path: {name!r}")
    if "\\" in name:
        raise InvalidPluginError(f"package contains a backslash path: {name!r}")
    if "\x00" in name:
        raise InvalidPluginError("package contains a NUL byte in an entry name")
    if _DRIVE_RE.match(name):
        raise InvalidPluginError(f"package contains a drive-qualified path: {name!r}")
    segments = name.split("/")
    if is_dir:
        while segments and segments[-1] == "":
            segments.pop()
    if not segments:
        raise InvalidPluginError("package contains an entry with an empty name")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise InvalidPluginError(f"package contains an unsafe path: {name!r}")
    return "/".join(segments)


def _validate_entry_metadata(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & _ENCRYPTED_FLAG:
        raise InvalidPluginError(f"package contains an encrypted entry: {info.filename!r}")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise InvalidPluginError(
            f"package uses unsupported compression for {info.filename!r}"
        )
    mode = info.external_attr >> 16
    if mode == 0:
        return
    file_type = stat.S_IFMT(mode)
    if file_type in (0, stat.S_IFREG, stat.S_IFDIR):
        return
    if file_type == stat.S_IFLNK:
        raise InvalidPluginError(f"package contains a symbolic link: {info.filename!r}")
    raise InvalidPluginError(f"package contains a special file: {info.filename!r}")


def _zip64_extra_body(extra: bytes) -> bytes | None:
    position = 0
    while position + 4 <= len(extra):
        field_id = _u16(extra, position)
        field_length = _u16(extra, position + 2)
        body_start = position + 4
        body_end = body_start + field_length
        if body_end > len(extra):
            return None
        if field_id == _ZIP64_EXTRA_ID:
            return extra[body_start:body_end]
        position = body_end
    return None


def _resolve_zip64_sizes(usize32: int, csize32: int, extra: bytes) -> tuple[int, int]:
    """Resolve local ZIP64 size sentinels (APPNOTE 4.5.3).

    The local ZIP64 extra stores the original uncompressed size then the
    compressed size, in that order, for whichever fields are sentinels.
    """
    if usize32 != _ZIP64_SENTINEL and csize32 != _ZIP64_SENTINEL:
        return usize32, csize32
    body = _zip64_extra_body(extra)
    if body is None:
        raise InvalidPluginError("package is missing local ZIP64 size metadata")
    index = 0
    uncompressed = usize32
    compressed = csize32
    if usize32 == _ZIP64_SENTINEL:
        if index + 8 > len(body):
            raise InvalidPluginError("package has corrupt local ZIP64 metadata")
        uncompressed = int.from_bytes(body[index : index + 8], "little")
        index += 8
    if csize32 == _ZIP64_SENTINEL:
        if index + 8 > len(body):
            raise InvalidPluginError("package has corrupt local ZIP64 metadata")
        compressed = int.from_bytes(body[index : index + 8], "little")
    return uncompressed, compressed


def _parse_local_header(
    data: bytes, info: zipfile.ZipInfo
) -> tuple[int, int, int, int, int, bool]:
    offset = info.header_offset
    if offset < 0 or offset + 30 > len(data) or data[offset : offset + 4] != _LOCAL_SIG:
        raise InvalidPluginError("package has a corrupt local file header")
    flags = _u16(data, offset + 6)
    method = _u16(data, offset + 8)
    crc = _u32(data, offset + 14)
    csize32 = _u32(data, offset + 18)
    usize32 = _u32(data, offset + 22)
    name_length = _u16(data, offset + 26)
    extra_length = _u16(data, offset + 28)
    name_start = offset + 30
    name_end = name_start + name_length
    extra_start = name_end
    extra_end = extra_start + extra_length
    if extra_end > len(data):
        raise InvalidPluginError("package has a corrupt local file header")
    name_bytes = data[name_start:name_end]
    if b"\x00" in name_bytes:
        raise InvalidPluginError("package contains a NUL byte in an entry name")
    if method != info.compress_type:
        raise InvalidPluginError("package has inconsistent compression metadata")
    if (flags | info.flag_bits) & _ENCRYPTED_FLAG:
        raise InvalidPluginError(f"package contains an encrypted entry: {info.filename!r}")
    if (flags ^ info.flag_bits) & _DATA_DESCRIPTOR_FLAG:
        raise InvalidPluginError("package has inconsistent data-descriptor metadata")
    if (flags ^ info.flag_bits) & _EFS_FLAG:
        raise InvalidPluginError("package has an inconsistent filename encoding flag")
    encoding = "utf-8" if flags & _EFS_FLAG else "cp437"
    try:
        local_name = name_bytes.decode(encoding)
    except UnicodeDecodeError as exc:
        raise InvalidPluginError("package has an undecodable local filename") from exc
    if local_name != info.orig_filename:
        raise InvalidPluginError(
            "package local filename does not match the central directory"
        )
    zip64 = csize32 == _ZIP64_SENTINEL or usize32 == _ZIP64_SENTINEL
    uncompressed, compressed = _resolve_zip64_sizes(
        usize32, csize32, data[extra_start:extra_end]
    )
    return flags, crc, uncompressed, compressed, extra_end, zip64


def _entry_data_limit(
    info: zipfile.ZipInfo, local_starts: list[int], central_start: int, size: int
) -> int:
    limit = size
    if 0 <= central_start < limit:
        limit = central_start
    for start in local_starts:
        if info.header_offset < start < limit:
            limit = start
    return limit


def _verify_data_descriptor(
    data: bytes, position: int, info: zipfile.ZipInfo, *, zip64: bool
) -> None:
    """Validate the optional data descriptor that follows the compressed data.

    A ZIP64 entry (local size sentinels) uses 8-byte size fields (APPNOTE
    4.3.9.2); otherwise 4-byte fields are used.
    """
    cursor = position
    if data[cursor : cursor + 4] == _DATA_DESCRIPTOR_SIG:
        cursor += 4
    width = 8 if zip64 else 4
    size_fields = cursor + 4
    if size_fields + 2 * width > len(data):
        raise InvalidPluginError("package has a truncated data descriptor")
    crc = _u32(data, cursor)
    compressed = int.from_bytes(data[size_fields : size_fields + width], "little")
    uncompressed = int.from_bytes(
        data[size_fields + width : size_fields + 2 * width], "little"
    )
    if (
        crc != info.CRC
        or compressed != info.compress_size
        or uncompressed != info.file_size
    ):
        raise InvalidPluginError(
            "package data descriptor disagrees with the central directory"
        )


def _inflate_bounded(
    data: bytes, start: int, end: int, cap: int, collect: bool
) -> tuple[bytes | None, int, int]:
    """Stream-decompress ``data[start:end]`` with bounded chunked memory.

    Never produces more than ``cap`` bytes; returns the decoded buffer (only
    when ``collect``), the actual byte count and the actual CRC32.
    """
    decompressor = zlib.decompressobj(-15)
    crc = 0
    produced = 0
    buffers: list[bytes] | None = [] if collect else None
    position = start
    pending = b""
    tail = b""
    try:
        while True:
            if not pending:
                if position >= end:
                    break
                pending = data[position : min(position + _CHUNK_SIZE, end)]
                position += len(pending)
            out = decompressor.decompress(pending, _CHUNK_SIZE)
            pending = decompressor.unconsumed_tail
            if out:
                produced += len(out)
                if produced > cap:
                    raise InvalidPluginError(
                        "package entry expands beyond its declared or allowed size"
                    )
                crc = zlib.crc32(out, crc)
                if buffers is not None:
                    buffers.append(out)
            if decompressor.eof:
                break
        if not decompressor.eof:
            raise InvalidPluginError("package entry is truncated")
        if decompressor.unused_data:
            raise InvalidPluginError("package entry has trailing data")
        if position < end:
            raise InvalidPluginError("package entry has trailing data")
        tail = decompressor.flush()
    except zlib.error as exc:
        raise InvalidPluginError("package entry is corrupt or truncated") from exc
    if tail:
        produced += len(tail)
        if produced > cap:
            raise InvalidPluginError(
                "package entry expands beyond its declared or allowed size"
            )
        crc = zlib.crc32(tail, crc)
        if buffers is not None:
            buffers.append(tail)
    decoded = b"".join(buffers) if buffers is not None else None
    return decoded, produced, crc & 0xFFFFFFFF


def _decode_entry(
    data: bytes,
    info: zipfile.ZipInfo,
    *,
    data_limit: int,
    cap: int,
    collect: bool,
) -> tuple[int, bytes | None]:
    if info.is_dir() and info.file_size != 0:
        raise InvalidPluginError(f"directory entry contains data: {info.filename!r}")
    flags, local_crc, local_usize, local_csize, data_start, zip64 = _parse_local_header(
        data, info
    )
    has_descriptor = bool(flags & _DATA_DESCRIPTOR_FLAG)
    compressed_len = info.compress_size
    if compressed_len < 0:
        raise InvalidPluginError("package has an invalid compressed size")
    data_end = data_start + compressed_len
    if data_end > len(data) or data_end > data_limit:
        raise InvalidPluginError(f"package entry data is out of bounds: {info.filename!r}")
    if has_descriptor:
        _verify_data_descriptor(data, data_end, info, zip64=zip64)
    elif (
        local_crc != info.CRC
        or local_csize != compressed_len
        or local_usize != info.file_size
    ):
        raise InvalidPluginError("package has a size or CRC mismatch between headers")

    if info.compress_type == zipfile.ZIP_STORED:
        if compressed_len != info.file_size:
            raise InvalidPluginError(
                f"package has inconsistent stored sizes: {info.filename!r}"
            )
        crc = 0
        view = memoryview(data)
        for index in range(data_start, data_end, _CHUNK_SIZE):
            crc = zlib.crc32(view[index : min(index + _CHUNK_SIZE, data_end)], crc)
        crc &= 0xFFFFFFFF
        produced = compressed_len
        decoded = bytes(view[data_start:data_end]) if collect else None
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        decoded, produced, crc = _inflate_bounded(data, data_start, data_end, cap, collect)
    else:
        raise InvalidPluginError(f"package uses unsupported compression for {info.filename!r}")

    if produced != info.file_size:
        raise InvalidPluginError(
            f"package decompressed size does not match the declared size: {info.filename!r}"
        )
    if crc != info.CRC:
        raise InvalidPluginError(f"package entry failed its integrity check: {info.filename!r}")
    if compressed_len > 0:
        if produced > MAX_ENTRY_RATIO * compressed_len:
            raise InvalidPluginError(f"package entry ratio too high: {info.filename!r}")
    elif produced > 0:
        raise InvalidPluginError(f"package entry ratio too high: {info.filename!r}")
    return produced, decoded


def _verify_entries(
    data: bytes,
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    *,
    max_size_bytes: int,
    collect_names: set[str],
) -> tuple[dict[str, bytes], int]:
    configured_cap = MAX_UNCOMPRESSED_RATIO * max_size_bytes
    declared_total = sum(info.file_size for info in infos)
    if declared_total > configured_cap:
        raise InvalidPluginError(
            f"package uncompressed size exceeds 5x the configured {max_size_bytes} byte limit"
        )
    local_starts = sorted(info.header_offset for info in infos)
    central_start = int(getattr(archive, "start_dir", len(data)))
    collected: dict[str, bytes] = {}
    used = 0
    for info in infos:
        collect = info.filename in collect_names
        cap = max(0, min(info.file_size, configured_cap - used))
        data_limit = _entry_data_limit(info, local_starts, central_start, len(data))
        produced, decoded = _decode_entry(
            data, info, data_limit=data_limit, cap=cap, collect=collect
        )
        used += produced
        if decoded is not None:
            collected[info.filename] = decoded
    if used > configured_cap:
        raise InvalidPluginError(
            f"package uncompressed size exceeds 5x the configured {max_size_bytes} byte limit"
        )
    return collected, used


def _allowed_import(module: str, local_modules: set[str]) -> bool:
    top = module.split(".")[0]
    return (
        top in sys.stdlib_module_names
        or top in _ALLOWED_THIRD_PARTY
        or top == _SDK_MODULE
        or top in local_modules
    )


def _lint_python(source: str, path: str, local_modules: set[str]) -> list[str]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"syntax error in {path}: {exc.msg}"]
    warnings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed_import(alias.name, local_modules):
                    warnings.append(f"unrecognized import {alias.name!r} in {path}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module is None:
                continue
            if not _allowed_import(node.module, local_modules):
                warnings.append(f"unrecognized import {node.module!r} in {path}")
    return warnings


@dataclass(frozen=True, slots=True)
class _RawEntry:
    path: str
    is_dir: bool
    file_size: int
    zip_name: str


def _read_entries(zf: zipfile.ZipFile) -> list[_RawEntry]:
    entries: list[_RawEntry] = []
    for info in zf.infolist():
        # ``ZipInfo.filename`` is truncated at the first NUL while
        # ``orig_filename`` keeps the raw decoded name.
        if "\x00" in info.orig_filename:
            raise InvalidPluginError("package contains a NUL byte in an entry name")
        is_dir = info.is_dir()
        path = _normalize_entry_path(info.filename, is_dir=is_dir)
        _validate_entry_metadata(info)
        entries.append(
            _RawEntry(
                path=path,
                is_dir=is_dir,
                file_size=info.file_size,
                zip_name=info.filename,
            )
        )
    return entries


def _resolve_root(entries: list[_RawEntry]) -> str:
    files = {entry.path for entry in entries if not entry.is_dir}
    if MANIFEST_FILENAME in files:
        return ""
    top_segments = {entry.path.split("/", 1)[0] for entry in entries}
    if len(top_segments) != 1:
        raise InvalidPluginError(
            "package must contain manifest.json at its root or inside one top-level directory"
        )
    top = next(iter(top_segments))
    if f"{top}/{MANIFEST_FILENAME}" not in files:
        raise InvalidPluginError("package is missing manifest.json")
    return f"{top}/"


def _deduplicate(entries: list[_RawEntry], root_prefix: str) -> list[_RawEntry]:
    top_dir = root_prefix[:-1] if root_prefix else ""
    relative: list[_RawEntry] = []
    for entry in entries:
        if entry.is_dir and top_dir and entry.path == top_dir:
            continue
        if not entry.path.startswith(root_prefix):
            raise InvalidPluginError("package has an inconsistent directory layout")
        path = entry.path[len(root_prefix) :]
        if not path:
            continue
        relative.append(_RawEntry(path, entry.is_dir, entry.file_size, entry.zip_name))
    return relative


def _check_conflicts(entries: list[_RawEntry]) -> None:
    files: set[str] = set()
    dirs: set[str] = set()
    for entry in entries:
        if entry.is_dir:
            dirs.add(entry.path)
            continue
        if entry.path in files:
            raise InvalidPluginError(f"package contains a duplicate entry: {entry.path!r}")
        files.add(entry.path)

    for path in files:
        if path in dirs:
            raise InvalidPluginError(f"package has a file/directory conflict at {path!r}")
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise InvalidPluginError(
                    f"package has a file/directory conflict at {path!r}"
                )

    for path in dirs:
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise InvalidPluginError(
                    f"package has a file/directory conflict at {path!r}"
                )


def _local_modules(entries: list[_RawEntry]) -> set[str]:
    modules: set[str] = set()
    for entry in entries:
        parts = entry.path.split("/")
        if entry.is_dir:
            modules.add(parts[0])
        elif entry.path.endswith(".py"):
            modules.add(parts[0][:-3] if len(parts) == 1 else parts[0])
    return modules


def _collect_names(infos: list[zipfile.ZipInfo]) -> set[str]:
    names: set[str] = set()
    for info in infos:
        if info.is_dir():
            continue
        if info.filename.endswith(".py") or info.filename.rsplit("/", 1)[-1] == MANIFEST_FILENAME:
            names.add(info.filename)
    return names


def validate_package(
    zip_bytes: bytes,
    *,
    max_size_bytes: int,
    core_version: str = CORE_VERSION,
) -> ValidatedPackage:
    """Validate a plugin ZIP without writing anything.

    Raises :class:`PackageTooLargeError` when the archive exceeds
    ``max_size_bytes`` and :class:`InvalidPluginError` for any other defect.
    """
    if not isinstance(zip_bytes, (bytes, bytearray)):
        raise InvalidPluginError("package must be bytes")
    data = bytes(zip_bytes)
    if not data:
        raise InvalidPluginError("package is empty")
    if len(data) > max_size_bytes:
        raise PackageTooLargeError(
            f"package exceeds the {max_size_bytes} byte compressed size limit"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidPluginError("package is not a valid ZIP archive") from exc
    except UnicodeDecodeError as exc:
        raise InvalidPluginError("package has an undecodable entry name") from exc

    with archive:
        infos = archive.infolist()
        if not infos:
            raise InvalidPluginError("package is empty")
        if len(infos) > MAX_ENTRY_COUNT:
            raise InvalidPluginError(
                f"package has more than {MAX_ENTRY_COUNT} entries"
            )

        raw_entries = _read_entries(archive)
        collected, actual_total = _verify_entries(
            data,
            archive,
            infos,
            max_size_bytes=max_size_bytes,
            collect_names=_collect_names(infos),
        )

        root_prefix = _resolve_root(raw_entries)
        relative = _deduplicate(raw_entries, root_prefix)
        _check_conflicts(relative)

        manifest_bytes = _read_manifest(relative, collected)
        try:
            manifest = parse_manifest(manifest_bytes, core_version=core_version)
        except ManifestError as exc:
            raise InvalidPluginError(str(exc)) from exc

        warnings = _lint(relative, collected)

    return ValidatedPackage(
        manifest=manifest,
        entries=tuple(
            ValidatedEntry(
                path=entry.path,
                is_dir=entry.is_dir,
                file_size=entry.file_size,
                zip_name=entry.zip_name,
            )
            for entry in relative
        ),
        root_prefix=root_prefix,
        warnings=tuple(warnings),
        compressed_size=len(data),
        uncompressed_size=actual_total,
        zip_bytes=data,
    )


def _read_manifest(entries: list[_RawEntry], collected: dict[str, bytes]) -> bytes:
    for entry in entries:
        if not entry.is_dir and entry.path == MANIFEST_FILENAME:
            data = collected.get(entry.zip_name)
            if data is None:
                raise InvalidPluginError("package is missing manifest.json")
            return data
    raise InvalidPluginError("package is missing manifest.json")


def _lint(entries: list[_RawEntry], collected: dict[str, bytes]) -> list[str]:
    local_modules = _local_modules(entries)
    warnings: list[str] = []
    for entry in entries:
        if entry.is_dir or not entry.path.endswith(".py"):
            continue
        source = collected.get(entry.zip_name)
        if source is None:
            continue
        warnings.extend(
            _lint_python(
                source.decode("utf-8", errors="replace"), entry.path, local_modules
            )
        )
    return warnings


def _is_within(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        return False


def extract_package(validated: ValidatedPackage, destination: Path) -> None:
    """Extract a validated package into a fresh, empty scratch directory.

    All path and containment checks for every entry (files and directories) run
    before any file is created, and the destination itself may not be a
    symbolic link. ``ValidatedPackage`` is public, so entry paths are
    re-validated here rather than assumed safe. The caller owns the destination
    and must not race another writer into it (owner-trusted, no sandboxing).
    """
    dest = Path(destination)
    if dest.is_symlink():
        raise InvalidPluginError("extraction destination must not be a symbolic link")
    if dest.exists():
        if not dest.is_dir():
            raise InvalidPluginError("extraction destination is not a directory")
        if any(dest.iterdir()):
            raise InvalidPluginError("extraction destination must be empty")

    base_real = os.path.realpath(dest)
    targets: list[tuple[ValidatedEntry, Path]] = []
    for entry in validated.entries:
        normalized = _normalize_entry_path(entry.path, is_dir=entry.is_dir)
        if normalized != entry.path:
            raise InvalidPluginError("validated package entry path is not normalized")
        target = dest / normalized
        if not _is_within(os.path.realpath(target), base_real):
            raise InvalidPluginError(
                f"entry escapes the extraction directory: {entry.path!r}"
            )
        targets.append((entry, target))

    if not dest.exists():
        dest.mkdir(parents=True)

    with zipfile.ZipFile(io.BytesIO(validated.zip_bytes)) as archive:
        for entry, target in targets:
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry.zip_name))


__all__ = [
    "MAX_ENTRY_COUNT",
    "MAX_ENTRY_RATIO",
    "MAX_UNCOMPRESSED_RATIO",
    "InvalidPluginError",
    "PackageTooLargeError",
    "ValidatedEntry",
    "ValidatedPackage",
    "extract_package",
    "validate_package",
]
