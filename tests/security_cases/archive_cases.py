"""Reusable, dependency-free malicious ZIP fixture builders.

These fixtures exist to prove package-validation rejections in a later integration
phase.  Everything here uses only the Python standard library and must never
import, execute, or ship in-development ``custom_mcp`` / ``mcp_plugin_sdk`` code.

Design rules:

* A fixture is *actually* malicious.  Crafted metadata is written into the ZIP
  bytes and re-read with :mod:`struct` / :mod:`zipfile` so tests cannot pass on a
  comment or a scary filename alone.
* No fixture is ever extracted to disk.  Tests read bytes and header fields only.
* Threshold fixtures use deliberately tiny limits so the suite stays fast and
  bounded; the real deployment defaults live in ``ARCHITECTURE.md``.
* ``validator_options`` carries the thresholds a validator should be invoked
  with for that case.  It is a hint for downstream integration, not a binding
  parameter mapping.
"""

from __future__ import annotations

import io
import json
import random
import stat
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import IO, Any, cast

# ---------------------------------------------------------------------------
# Safety-contract thresholds (mirrors ARCHITECTURE.md / PHASE_1_PLAN.md)
# ---------------------------------------------------------------------------

DEFAULT_MAX_SIZE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 2000
DEFAULT_MAX_RATIO = 100
# The validator derives its total-uncompressed guard from the compressed limit.
UNCOMPRESSED_EXPANSION_FACTOR = 5

# Small fixture thresholds: never allocate huge buffers in tests.
SHORT_MAX_SIZE_BYTES = 128
SHORT_MAX_ENTRIES = 8

# Content size for the expansion fixture: the compressed archive must fit inside
# this while its total uncompressed size exceeds ``UNCOMPRESSED_EXPANSION_FACTOR``
# times it, and every individual entry stays under ``DEFAULT_MAX_RATIO``.
EXPANSION_FIXTURE_MAX_SIZE_BYTES = 12288
_EXPANSION_CHUNK_SIZE = 16384
_EXPANSION_CHUNK_COUNT = 4

# Unsupported "compression method" written into crafted headers.
UNSUPPORTED_COMPRESSION_METHOD = 99
_FLAG_ENCRYPTED = 0x0001

# ---------------------------------------------------------------------------
# Minimal valid package content
# ---------------------------------------------------------------------------

VALID_SLUG = "hello"
VALID_VERSION = "1.0.0"
VALID_ENTRY = "plugin:HelloPlugin"
VALID_AUTH_MODES: tuple[str, ...] = ("none",)

PLUGIN_PY = '''"""Minimal valid plugin module; archive validation never executes code."""
from __future__ import annotations


class HelloPlugin:
    def echo(self, message: str) -> str:
        return message
'''

HELPER_INIT_PY = ""
HELPER_UTIL_PY = '''"""Nested helper module used to exercise relative-import linting."""
from __future__ import annotations

from .formatter import render


def ping() -> str:
    return render("pong")
'''
HELPER_FORMATTER_PY = '''"""Nested helper module used to exercise relative-import linting."""
from __future__ import annotations


def render(value: str) -> str:
    return value
'''

# A single added entry used for crafted-metadata cases.  Manifest.json and
# plugin.py stay untouched so each case isolates exactly one malicious field.
METADATA_ENTRY = "data.bin"
METADATA_PAYLOAD = b"malicious metadata probe payload"

NUL_PLACEHOLDER = "pkg/module.py"
NUL_TARGET = b"pkg/modul\x00.py"
if len(NUL_PLACEHOLDER) != len(NUL_TARGET):  # equal-length byte mutation required
    raise ValueError("NUL fixture placeholder and target must be the same length")


def valid_manifest() -> dict[str, object]:
    """Return a fresh, minimal valid manifest mapping."""
    return {
        "name": VALID_SLUG,
        "version": VALID_VERSION,
        "entry": VALID_ENTRY,
        "auth_modes": list(VALID_AUTH_MODES),
    }


def manifest_bytes(**overrides: object) -> bytes:
    """Encode ``valid_manifest()`` with optional field overrides."""
    manifest = valid_manifest()
    manifest.update(overrides)
    return json.dumps(manifest).encode("utf-8")


def manifest_bytes_without(*keys: str) -> bytes:
    """Encode a manifest with the named keys removed."""
    manifest = {key: value for key, value in valid_manifest().items() if key not in keys}
    return json.dumps(manifest).encode("utf-8")


def plugin_bytes() -> bytes:
    """UTF-8 bytes of the minimal normal ``plugin.py``."""
    return PLUGIN_PY.encode("utf-8")


# ---------------------------------------------------------------------------
# ArchiveCase model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchiveCase:
    """One crafted archive plus the rejection contract it should trigger.

    ``rejection_category`` is intentionally a short substring rather than an
    exact message: a downstream validator may word its error differently while
    still addressing the same defect.  Valid cases carry ``None``.
    """

    name: str
    data: bytes
    expected_valid: bool
    rejection_category: str | None = None
    description: str = ""
    validator_options: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ArchiveCase.name must be non-empty")
        if not self.data:
            raise ValueError(f"ArchiveCase {self.name!r} has empty data")
        if self.expected_valid and self.rejection_category is not None:
            raise ValueError(
                f"valid ArchiveCase {self.name!r} must not declare a rejection category"
            )
        if not self.expected_valid and not self.rejection_category:
            raise ValueError(
                f"invalid ArchiveCase {self.name!r} must declare a rejection category"
            )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> ArchiveCase:
        """Build a case from a plain mapping (mapping-factory helper)."""
        category = mapping.get("rejection_category")
        options = mapping.get("validator_options") or {}
        return cls(
            name=str(mapping["name"]),
            data=bytes(mapping["data"]),
            expected_valid=bool(mapping["expected_valid"]),
            rejection_category=None if category is None else str(category),
            description=str(mapping.get("description", "")),
            validator_options={str(key): int(value) for key, value in dict(options).items()},
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping of the case metadata."""
        return {
            "name": self.name,
            "data": self.data,
            "expected_valid": self.expected_valid,
            "rejection_category": self.rejection_category,
            "description": self.description,
            "validator_options": dict(self.validator_options),
        }


def _case(
    name: str,
    data: bytes,
    *,
    valid: bool,
    category: str | None,
    description: str,
    **options: int,
) -> ArchiveCase:
    return ArchiveCase(
        name=name,
        data=bytes(data),
        expected_valid=valid,
        rejection_category=category,
        description=description,
        validator_options=options,
    )


# ---------------------------------------------------------------------------
# ZIP construction helpers
# ---------------------------------------------------------------------------


# Fixed DOS timestamp keeps fixtures byte-for-byte reproducible regardless of
# when they are built (writestr(str, ...) would otherwise stamp the clock).
_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def _zip(files: Mapping[str, bytes], *, compress_type: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE_TIME)
            info.compress_type = compress_type
            archive.writestr(info, payload)
    return buffer.getvalue()


def _zip_infos(entries: Iterable[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    return buffer.getvalue()


def _low_entropy_payload(length: int, seed: int) -> bytes:
    """Deterministic ~1-bit-per-byte payload with a stable ~6:1 deflate ratio.

    The exact bytes depend on the interpreter's Mersenne Twister output, but any
    realisation has the same entropy, so the compression-ratio properties the
    tests assert hold regardless of Python version.
    """
    rng = random.Random(seed)
    return bytes(rng.getrandbits(1) for _ in range(length))


# ---------------------------------------------------------------------------
# Raw ZIP header parsing (local + central directory)
# ---------------------------------------------------------------------------

LOCAL_SIGNATURE = b"PK\x03\x04"
CENTRAL_SIGNATURE = b"PK\x01\x02"
END_OF_CENTRAL_SIGNATURE = b"PK\x05\x06"

LOCAL_FLAGS_OFFSET = 6
LOCAL_METHOD_OFFSET = 8
LOCAL_CRC_OFFSET = 14
LOCAL_COMPRESSED_SIZE_OFFSET = 18
LOCAL_UNCOMPRESSED_SIZE_OFFSET = 22
LOCAL_FILENAME_LENGTH_OFFSET = 26
LOCAL_EXTRA_LENGTH_OFFSET = 28
LOCAL_FILENAME_OFFSET = 30
LOCAL_DATA_DESCRIPTOR_FLAG = 0x0008
DATA_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
ZIP64_EXTRA_ID = b"\x01\x00"

CENTRAL_FLAGS_OFFSET = 8
CENTRAL_METHOD_OFFSET = 10
CENTRAL_CRC_OFFSET = 16
CENTRAL_COMPRESSED_SIZE_OFFSET = 20
CENTRAL_UNCOMPRESSED_SIZE_OFFSET = 24
CENTRAL_FILENAME_LENGTH_OFFSET = 28
CENTRAL_EXTERNAL_ATTR_OFFSET = 38
CENTRAL_FILENAME_OFFSET = 46


def _offsets(data: bytes, signature: bytes) -> list[int]:
    found: list[int] = []
    start = 0
    while True:
        index = data.find(signature, start)
        if index < 0:
            return found
        found.append(index)
        start = index + len(signature)


def local_header_offsets(data: bytes) -> list[int]:
    """Offsets of every local file header signature in ``data``."""
    return _offsets(data, LOCAL_SIGNATURE)


def central_header_offsets(data: bytes) -> list[int]:
    """Offsets of every central directory header signature in ``data``."""
    return _offsets(data, CENTRAL_SIGNATURE)


def _u16(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", data, offset)[0])


def _u32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", data, offset)[0])


def _name(data: bytes, offset: int, length_field: int, name_field: int) -> bytes:
    length = _u16(data, offset + length_field)
    return data[offset + name_field : offset + name_field + length]


def local_filename(data: bytes, offset: int) -> bytes:
    return _name(data, offset, LOCAL_FILENAME_LENGTH_OFFSET, LOCAL_FILENAME_OFFSET)


def central_filename(data: bytes, offset: int) -> bytes:
    return _name(data, offset, CENTRAL_FILENAME_LENGTH_OFFSET, CENTRAL_FILENAME_OFFSET)


def local_flags(data: bytes, offset: int) -> int:
    return _u16(data, offset + LOCAL_FLAGS_OFFSET)


def central_flags(data: bytes, offset: int) -> int:
    return _u16(data, offset + CENTRAL_FLAGS_OFFSET)


def local_method(data: bytes, offset: int) -> int:
    return _u16(data, offset + LOCAL_METHOD_OFFSET)


def central_method(data: bytes, offset: int) -> int:
    return _u16(data, offset + CENTRAL_METHOD_OFFSET)


def local_crc(data: bytes, offset: int) -> int:
    return _u32(data, offset + LOCAL_CRC_OFFSET)


def central_crc(data: bytes, offset: int) -> int:
    return _u32(data, offset + CENTRAL_CRC_OFFSET)


def local_compressed_size(data: bytes, offset: int) -> int:
    return _u32(data, offset + LOCAL_COMPRESSED_SIZE_OFFSET)


def central_compressed_size(data: bytes, offset: int) -> int:
    return _u32(data, offset + CENTRAL_COMPRESSED_SIZE_OFFSET)


def local_uncompressed_size(data: bytes, offset: int) -> int:
    return _u32(data, offset + LOCAL_UNCOMPRESSED_SIZE_OFFSET)


def central_uncompressed_size(data: bytes, offset: int) -> int:
    return _u32(data, offset + CENTRAL_UNCOMPRESSED_SIZE_OFFSET)


def central_external_attributes(data: bytes, offset: int) -> int:
    return _u32(data, offset + CENTRAL_EXTERNAL_ATTR_OFFSET)


def local_extra(data: bytes, offset: int) -> bytes:
    """Return the local-header extra field bytes for the entry at ``offset``."""
    name_length = _u16(data, offset + LOCAL_FILENAME_LENGTH_OFFSET)
    extra_length = _u16(data, offset + LOCAL_EXTRA_LENGTH_OFFSET)
    start = offset + LOCAL_FILENAME_OFFSET + name_length
    return data[start : start + extra_length]


def local_data_offset(data: bytes, offset: int) -> int:
    """Byte offset where an entry's compressed data begins."""
    name_length = _u16(data, offset + LOCAL_FILENAME_LENGTH_OFFSET)
    extra_length = _u16(data, offset + LOCAL_EXTRA_LENGTH_OFFSET)
    return offset + LOCAL_FILENAME_OFFSET + name_length + extra_length


def entry_compressed_bytes(data: bytes, filename: str) -> bytes:
    """Raw compressed bytes for one entry, located via its local and central headers."""
    local = find_local_header(data, filename)
    central = find_central_header(data, filename)
    start = local_data_offset(data, local)
    return data[start : start + central_compressed_size(data, central)]


def data_descriptor_offset(data: bytes, filename: str) -> int:
    """Byte offset of the data descriptor that follows ``filename``'s data."""
    local = find_local_header(data, filename)
    central = find_central_header(data, filename)
    return local_data_offset(data, local) + central_compressed_size(data, central)


def data_descriptor_crc(data: bytes, filename: str) -> int:
    """CRC field stored in the trailing data descriptor (signature-aware)."""
    offset = data_descriptor_offset(data, filename)
    if data[offset : offset + len(DATA_DESCRIPTOR_SIGNATURE)] == DATA_DESCRIPTOR_SIGNATURE:
        offset += len(DATA_DESCRIPTOR_SIGNATURE)
    return _u32(data, offset)


def _fixed_info(name: str) -> zipfile.ZipInfo:
    """A ZipInfo with the reproducible fixture timestamp."""
    return zipfile.ZipInfo(name, date_time=_FIXED_DATE_TIME)


def find_local_header(data: bytes, filename: str) -> int:
    """Return the local header offset for ``filename`` (UTF-8 bytes match)."""
    target = filename.encode("utf-8")
    for offset in local_header_offsets(data):
        if local_filename(data, offset) == target:
            return offset
    raise KeyError(f"no local header for {filename!r}")


def find_central_header(data: bytes, filename: str) -> int:
    """Return the central header offset for ``filename`` (UTF-8 bytes match)."""
    target = filename.encode("utf-8")
    for offset in central_header_offsets(data):
        if central_filename(data, offset) == target:
            return offset
    raise KeyError(f"no central header for {filename!r}")


def _patch_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", data, offset, value & 0xFFFF)


def _patch_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value & 0xFFFFFFFF)


def _patched_base() -> bytes:
    return _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            METADATA_ENTRY: METADATA_PAYLOAD,
        }
    )


# ---------------------------------------------------------------------------
# Valid cases
# ---------------------------------------------------------------------------


def build_valid_root() -> ArchiveCase:
    data = _zip({"manifest.json": manifest_bytes(), "plugin.py": plugin_bytes()})
    return _case(
        "valid_root",
        data,
        valid=True,
        category=None,
        description="Root manifest.json plus plugin.py with a fully valid manifest.",
    )


def build_valid_single_top_dir() -> ArchiveCase:
    data = _zip(
        {
            "hello/manifest.json": manifest_bytes(),
            "hello/plugin.py": plugin_bytes(),
        }
    )
    return _case(
        "valid_single_top_dir",
        data,
        valid=True,
        category=None,
        description="One enclosing top-level directory that must be stripped.",
    )


def build_valid_relative_modules() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "helper/__init__.py": HELPER_INIT_PY.encode("utf-8"),
            "helper/util.py": HELPER_UTIL_PY.encode("utf-8"),
            "helper/formatter.py": HELPER_FORMATTER_PY.encode("utf-8"),
        }
    )
    return _case(
        "valid_relative_modules",
        data,
        valid=True,
        category=None,
        description="Nested package with relative imports; lint warnings never reject.",
    )


def build_valid_directory_records() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "pkg/": b"",
            "pkg/module.py": b"VALUE = 1\n",
        }
    )
    return _case(
        "valid_directory_records",
        data,
        valid=True,
        category=None,
        description="Harmless explicit directory record alongside a real package file.",
    )


# ---------------------------------------------------------------------------
# Path traversal / absolute / backslash cases
# ---------------------------------------------------------------------------


def _path_case(name: str, entries: Mapping[str, bytes], description: str) -> ArchiveCase:
    return _case(name, _zip(entries), valid=False, category="path", description=description)


def build_path_traversal_dotdot() -> ArchiveCase:
    return _path_case(
        "path_traversal_dotdot",
        {"manifest.json": manifest_bytes(), "plugin.py": plugin_bytes(), "../evil.py": b"evil"},
        "Entry escapes the archive root with a leading dot-dot segment.",
    )


def build_path_traversal_nested_dotdot() -> ArchiveCase:
    return _path_case(
        "path_traversal_nested_dotdot",
        {
            "hello/manifest.json": manifest_bytes(),
            "hello/plugin.py": plugin_bytes(),
            "hello/../evil.py": b"evil",
        },
        "Embedded dot-dot segment escapes an enclosing directory.",
    )


def build_path_absolute_posix() -> ArchiveCase:
    return _path_case(
        "path_absolute_posix",
        {"manifest.json": manifest_bytes(), "/etc/evil.py": b"evil"},
        "Absolute POSIX path entry.",
    )


def build_path_windows_drive_letter() -> ArchiveCase:
    return _path_case(
        "path_windows_drive_letter",
        {"manifest.json": manifest_bytes(), "C:/Windows/evil.py": b"evil"},
        "Windows drive-letter path entry (forward slash form).",
    )


def build_path_windows_drive_backslash() -> ArchiveCase:
    return _path_case(
        "path_windows_drive_backslash",
        {"manifest.json": manifest_bytes(), "C:\\Windows\\evil.py": b"evil"},
        "Windows drive-letter path entry (backslash form).",
    )


def build_path_windows_backslash() -> ArchiveCase:
    return _path_case(
        "path_windows_backslash",
        {"manifest.json": manifest_bytes(), "pkg\\evil.py": b"evil"},
        "Backslash in an otherwise relative entry name.",
    )


# ---------------------------------------------------------------------------
# Raw NUL filename (ZipInfo truncates names, so headers are patched manually)
# ---------------------------------------------------------------------------


def build_nul_filename() -> ArchiveCase:
    base = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            NUL_PLACEHOLDER: b"payload",
        }
    )
    data = base.replace(NUL_PLACEHOLDER.encode("ascii"), NUL_TARGET)
    if data == base:  # pragma: no cover - defensive
        raise AssertionError("NUL placeholder replacement did not modify the archive")
    return _case(
        "nul_filename",
        data,
        valid=False,
        category="nul",
        description=(
            "Raw NUL byte inside a local and central filename; zipfile truncates it "
            "on read so downstream validation must inspect raw headers."
        ),
    )


# ---------------------------------------------------------------------------
# Symlink / special UNIX file metadata
# ---------------------------------------------------------------------------


def _unix_entry(filename: str, mode: int, payload: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(filename)
    info.create_system = 3  # UNIX
    info.external_attr = (mode & 0xFFFF) << 16
    return info, payload


def build_symlink_unix() -> ArchiveCase:
    data = _zip_infos(
        [
            (zipfile.ZipInfo("manifest.json"), manifest_bytes()),
            (zipfile.ZipInfo("plugin.py"), plugin_bytes()),
            _unix_entry("link", stat.S_IFLNK | 0o777, b"/etc/passwd"),
        ]
    )
    return _case(
        "symlink_unix",
        data,
        valid=False,
        category="symlink",
        description="UNIX symlink entry whose external attributes mark S_IFLNK.",
    )


def build_special_file_fifo() -> ArchiveCase:
    data = _zip_infos(
        [
            (zipfile.ZipInfo("manifest.json"), manifest_bytes()),
            (zipfile.ZipInfo("plugin.py"), plugin_bytes()),
            _unix_entry("fifo", stat.S_IFIFO | 0o644, b""),
        ]
    )
    return _case(
        "special_file_fifo",
        data,
        valid=False,
        category="special",
        description="UNIX FIFO entry whose external attributes mark S_IFIFO.",
    )


def build_special_file_char() -> ArchiveCase:
    data = _zip_infos(
        [
            (zipfile.ZipInfo("manifest.json"), manifest_bytes()),
            (zipfile.ZipInfo("plugin.py"), plugin_bytes()),
            _unix_entry("device", stat.S_IFCHR | 0o600, b""),
        ]
    )
    return _case(
        "special_file_char",
        data,
        valid=False,
        category="special",
        description="UNIX character-device entry whose external attributes mark S_IFCHR.",
    )


# ---------------------------------------------------------------------------
# Duplicate normalized destinations / file-directory conflicts
# ---------------------------------------------------------------------------


def build_duplicate_normalized_file() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "pkg/module.py": b"first",
            "pkg/./module.py": b"second",
        }
    )
    return _case(
        "duplicate_normalized_file",
        data,
        valid=False,
        category="duplicate",
        description="Two distinct entry names normalise to the same destination path.",
    )


def build_duplicate_normalized_manifest() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "./manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
        }
    )
    return _case(
        "duplicate_normalized_manifest",
        data,
        valid=False,
        category="duplicate",
        description="Root and dot-prefixed manifest entries collide after normalisation.",
    )


def build_file_directory_conflict() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "pkg": b"I am a regular file",
            "pkg/module.py": b"nested module",
        }
    )
    return _case(
        "file_directory_conflict",
        data,
        valid=False,
        category="directory",
        description="An entry named 'pkg' is a file while 'pkg/module.py' needs it as a directory.",
    )


# ---------------------------------------------------------------------------
# Count / ratio / size thresholds
# ---------------------------------------------------------------------------


def build_too_many_entries_short(
    max_entries_constant_override: int = SHORT_MAX_ENTRIES,
) -> ArchiveCase:
    files: dict[str, bytes] = {
        "manifest.json": manifest_bytes(),
        "plugin.py": plugin_bytes(),
    }
    for index in range(max_entries_constant_override + 1):
        files[f"extra/file_{index}.txt"] = b"x"
    data = _zip(files)
    return _case(
        "too_many_entries_short",
        data,
        valid=False,
        category="entries",
        description=(
            "Entry count above a tiny override of the MAX_ENTRY_COUNT constant. "
            "The current API fixes the bound at 2000, so integration must monkeypatch "
            "the constant (not pass a parameter) to exercise this fixture."
        ),
        max_entries_constant_override=max_entries_constant_override,
    )


def build_too_many_entries_default() -> ArchiveCase:
    files: dict[str, bytes] = {
        "manifest.json": manifest_bytes(),
        "plugin.py": plugin_bytes(),
    }
    for index in range(DEFAULT_MAX_ENTRIES + 1):
        files[f"e/{index}"] = b""
    data = _zip(files)
    return _case(
        "too_many_entries_default",
        data,
        valid=False,
        category="entries",
        description=(
            "Entry count just above the fixed MAX_ENTRY_COUNT constant "
            f"({DEFAULT_MAX_ENTRIES}); needs no configuration."
        ),
    )


def build_ratio_exceeded(payload_size: int = 128 * 1024) -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "blob.bin": b"\x00" * payload_size,
        }
    )
    return _case(
        "ratio_exceeded",
        data,
        valid=False,
        category="ratio",
        description=(
            "One highly compressible entry exceeds the fixed "
            f"{DEFAULT_MAX_RATIO}:1 per-entry ratio constant; needs no configuration."
        ),
    )


def build_max_compressed_size_exceeded(
    max_size_bytes: int = SHORT_MAX_SIZE_BYTES,
) -> ArchiveCase:
    data = _zip({"manifest.json": manifest_bytes(), "plugin.py": plugin_bytes()})
    return _case(
        "max_compressed_size_exceeded",
        data,
        valid=False,
        category="size",
        description="Archive byte length exceeds a tiny configured compressed size limit.",
        max_size_bytes=max_size_bytes,
    )


def build_max_uncompressed_size_exceeded() -> ArchiveCase:
    files: dict[str, bytes] = {
        "manifest.json": manifest_bytes(),
        "plugin.py": plugin_bytes(),
    }
    for index in range(_EXPANSION_CHUNK_COUNT):
        files[f"blob_{index}.bin"] = _low_entropy_payload(_EXPANSION_CHUNK_SIZE, 1000 + index)
    data = _zip(files)
    return _case(
        "max_uncompressed_size_exceeded",
        data,
        valid=False,
        category="size",
        description=(
            "Archive fits within max_size_bytes, but its total uncompressed bytes "
            f"exceed {UNCOMPRESSED_EXPANSION_FACTOR} * max_size_bytes (the fixed "
            "expansion guard) while every entry stays below the 100:1 ratio constant."
        ),
        max_size_bytes=EXPANSION_FIXTURE_MAX_SIZE_BYTES,
    )


# ---------------------------------------------------------------------------
# Malformed / truncated archives
# ---------------------------------------------------------------------------


def build_malformed_zip() -> ArchiveCase:
    data = LOCAL_SIGNATURE + b"\x00" * 12
    return _case(
        "malformed_zip",
        data,
        valid=False,
        category="malformed",
        description="Signature-like prefix with no valid central directory or EOCD.",
    )


def build_truncated_zip() -> ArchiveCase:
    base = _zip({"manifest.json": manifest_bytes(), "plugin.py": plugin_bytes()})
    data = base[: -len(END_OF_CENTRAL_SIGNATURE) - 18]
    return _case(
        "truncated_zip",
        data,
        valid=False,
        category="malformed",
        description="Valid prefix truncated so the end-of-central-directory record is lost.",
    )


# ---------------------------------------------------------------------------
# Crafted metadata overriding a single entry's headers
# ---------------------------------------------------------------------------


def build_unsupported_compression() -> ArchiveCase:
    base = _patched_base()
    patched = bytearray(base)
    local = find_local_header(base, METADATA_ENTRY)
    central = find_central_header(base, METADATA_ENTRY)
    _patch_u16(patched, local + LOCAL_METHOD_OFFSET, UNSUPPORTED_COMPRESSION_METHOD)
    _patch_u16(patched, central + CENTRAL_METHOD_OFFSET, UNSUPPORTED_COMPRESSION_METHOD)
    return _case(
        "unsupported_compression",
        bytes(patched),
        valid=False,
        category="compression",
        description="Single entry declares an unsupported compression method in both headers.",
    )


def build_encrypted_flag() -> ArchiveCase:
    base = _patched_base()
    patched = bytearray(base)
    local = find_local_header(base, METADATA_ENTRY)
    central = find_central_header(base, METADATA_ENTRY)
    _patch_u16(patched, local + LOCAL_FLAGS_OFFSET, local_flags(base, local) | _FLAG_ENCRYPTED)
    _patch_u16(
        patched,
        central + CENTRAL_FLAGS_OFFSET,
        central_flags(base, central) | _FLAG_ENCRYPTED,
    )
    return _case(
        "encrypted_flag",
        bytes(patched),
        valid=False,
        category="encrypted",
        description="Single entry sets the encryption flag bit in local and central headers.",
    )


def build_crc_mismatch() -> ArchiveCase:
    base = _patched_base()
    patched = bytearray(base)
    local = find_local_header(base, METADATA_ENTRY)
    central = find_central_header(base, METADATA_ENTRY)
    _patch_u32(patched, local + LOCAL_CRC_OFFSET, local_crc(base, local) ^ 0x1)
    _patch_u32(patched, central + CENTRAL_CRC_OFFSET, central_crc(base, central) ^ 0x1)
    return _case(
        "crc_mismatch",
        bytes(patched),
        valid=False,
        category="crc",
        description="Stored CRC no longer matches the payload; zipfile.testzip detects it.",
    )


def build_uncompressed_size_mismatch() -> ArchiveCase:
    base = _patched_base()
    patched = bytearray(base)
    local = find_local_header(base, METADATA_ENTRY)
    declared = local_uncompressed_size(base, local)
    _patch_u32(patched, local + LOCAL_UNCOMPRESSED_SIZE_OFFSET, declared + 7)
    return _case(
        "uncompressed_size_mismatch",
        bytes(patched),
        valid=False,
        category="size",
        description="Local-header uncompressed size disagrees with the central directory.",
    )


def build_compressed_size_mismatch() -> ArchiveCase:
    base = _patched_base()
    patched = bytearray(base)
    local = find_local_header(base, METADATA_ENTRY)
    declared = local_compressed_size(base, local)
    _patch_u32(patched, local + LOCAL_COMPRESSED_SIZE_OFFSET, declared + 3)
    return _case(
        "compressed_size_mismatch",
        bytes(patched),
        valid=False,
        category="size",
        description="Local-header compressed size disagrees with the central directory.",
    )


# ---------------------------------------------------------------------------
# Enclosing-layout and manifest defects
# ---------------------------------------------------------------------------


def build_two_top_level_dirs() -> ArchiveCase:
    data = _zip(
        {
            "a/manifest.json": manifest_bytes(),
            "a/plugin.py": plugin_bytes(),
            "b/manifest.json": manifest_bytes(),
            "b/plugin.py": plugin_bytes(),
        }
    )
    return _case(
        "two_top_level_dirs",
        data,
        valid=False,
        category="layout",
        description="Two candidate enclosing directories; only one may be stripped.",
    )


def build_deep_enclosing_dir() -> ArchiveCase:
    data = _zip(
        {
            "a/b/manifest.json": manifest_bytes(),
            "a/b/plugin.py": plugin_bytes(),
        }
    )
    return _case(
        "deep_enclosing_dir",
        data,
        valid=False,
        category="layout",
        description="Manifest is two directories deep; at most one may be stripped.",
    )


def build_root_and_nested_manifest() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "nested/manifest.json": manifest_bytes(),
        }
    )
    return _case(
        "root_and_nested_manifest",
        data,
        valid=True,
        category=None,
        description=(
            "Root manifest.json wins; the nested manifest is ordinary additional "
            "package data and is not a layout violation."
        ),
    )


def build_missing_manifest() -> ArchiveCase:
    data = _zip({"plugin.py": plugin_bytes(), "README.md": b"# hello\n"})
    return _case(
        "missing_manifest",
        data,
        valid=False,
        category="manifest",
        description="No manifest.json at the root or under a single enclosing directory.",
    )


def build_invalid_manifest_json() -> ArchiveCase:
    data = _zip({"manifest.json": b"{ this is not json", "plugin.py": plugin_bytes()})
    return _case(
        "invalid_manifest_json",
        data,
        valid=False,
        category="manifest",
        description="manifest.json is present but not valid JSON.",
    )


def build_bad_slug() -> ArchiveCase:
    data = _zip(
        {"manifest.json": manifest_bytes(name="Hello World!"), "plugin.py": plugin_bytes()}
    )
    return _case(
        "bad_slug",
        data,
        valid=False,
        category="slug",
        description="Manifest name violates the slug character pattern.",
    )


def build_auth_mode_conflict() -> ArchiveCase:
    data = _zip(
        {
            "manifest.json": manifest_bytes(auth_modes=["none", "api_key"]),
            "plugin.py": plugin_bytes(),
        }
    )
    return _case(
        "auth_mode_conflict",
        data,
        valid=False,
        category="auth",
        description="auth_modes contains 'none' alongside another mode.",
    )


def build_bad_entry() -> ArchiveCase:
    data = _zip(
        {"manifest.json": manifest_bytes(entry="plugin"), "plugin.py": plugin_bytes()}
    )
    return _case(
        "bad_entry",
        data,
        valid=False,
        category="entry",
        description="Manifest entry is not a dotted module:ClassName reference.",
    )


def build_bad_version() -> ArchiveCase:
    data = _zip(
        {"manifest.json": manifest_bytes(version="1.0"), "plugin.py": plugin_bytes()}
    )
    return _case(
        "bad_version",
        data,
        valid=False,
        category="version",
        description="Manifest version is not a full MAJOR.MINOR.PATCH semantic version.",
    )


# ---------------------------------------------------------------------------
# Extended declared-size / streaming corpus (separate from build_cases)
#
# These cases cover declared-size and streaming defects the original corpus does
# not.  They are exposed via ``build_stream_cases`` so the stable ``build_cases``
# consumer keeps its exact 38-case contract.
# ---------------------------------------------------------------------------

FALSE_ACCEPT_PREFIX = b"VALUE = 1\n"  # exactly 10 bytes
FALSE_ACCEPT_PAYLOAD = FALSE_ACCEPT_PREFIX + b"Z" * (1024 * 1024)
ZERO_DECLARED_PAYLOAD = b"nonempty payload behind a zero-size declaration\n"
DATA_DESCRIPTOR_PAYLOAD = b"streamed data-descriptor payload\n"
FORCE_ZIP64_PAYLOAD = b"force zip64 sentinel payload\n"
DIRECTORY_PAYLOAD_SIZE = 128 * 1024
NESTED_MANIFEST_DEPTH = 100_000
MIN_CORE_VERSION_DIGITS = 5000


class _UnseekableWriter:
    """Write-only, non-seekable byte sink that forces ZIP data descriptors."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def seekable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        raise io.UnsupportedOperation("stream is not seekable")

    def tell(self) -> int:
        raise io.UnsupportedOperation("stream is not seekable")

    def close(self) -> None:
        return None

    def getvalue(self) -> bytes:
        return b"".join(self._chunks)


def _write_unseekable(*, force_zip64: bool) -> bytes:
    """Write a valid package to an unseekable sink, forcing data descriptors."""
    writer = _UnseekableWriter()
    stream = cast("IO[bytes]", writer)
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_fixed_info("manifest.json"), manifest_bytes())
        archive.writestr(_fixed_info("plugin.py"), plugin_bytes())
        info = _fixed_info("data.bin")
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=force_zip64) as handle:
            handle.write(FORCE_ZIP64_PAYLOAD if force_zip64 else DATA_DESCRIPTOR_PAYLOAD)
    return writer.getvalue()


def _patch_entry_declared_size(
    base: bytes,
    filename: str,
    *,
    uncompressed: int,
    crc: int | None = None,
) -> bytes:
    patched = bytearray(base)
    local = find_local_header(base, filename)
    central = find_central_header(base, filename)
    _patch_u32(patched, local + LOCAL_UNCOMPRESSED_SIZE_OFFSET, uncompressed)
    _patch_u32(patched, central + CENTRAL_UNCOMPRESSED_SIZE_OFFSET, uncompressed)
    if crc is not None:
        _patch_u32(patched, local + LOCAL_CRC_OFFSET, crc)
        _patch_u32(patched, central + CENTRAL_CRC_OFFSET, crc)
    return bytes(patched)


def build_declared_size_prefix_crc_false_accept() -> ArchiveCase:
    base = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "data.bin": FALSE_ACCEPT_PAYLOAD,
        }
    )
    data = _patch_entry_declared_size(
        base,
        "data.bin",
        uncompressed=len(FALSE_ACCEPT_PREFIX),
        crc=zlib.crc32(FALSE_ACCEPT_PREFIX),
    )
    return _case(
        "declared_size_prefix_crc_false_accept",
        data,
        valid=False,
        category="size",
        description=(
            "Declared uncompressed size is 10 with the CRC of the 10-byte prefix while "
            "the compressed size is preserved, so a validator trusting the declared "
            "size and CRC falsely accepts a 1 MiB payload."
        ),
    )


def build_declared_zero_actual_payload() -> ArchiveCase:
    base = _zip(
        {
            "manifest.json": manifest_bytes(),
            "plugin.py": plugin_bytes(),
            "data.bin": ZERO_DECLARED_PAYLOAD,
        }
    )
    data = _patch_entry_declared_size(base, "data.bin", uncompressed=0)
    return _case(
        "declared_zero_actual_payload",
        data,
        valid=False,
        category="size",
        description=(
            "Declared uncompressed size is zero while the stream still decompresses "
            "to a nonempty payload (the true CRC is left in place)."
        ),
    )


def build_valid_force_zip64() -> ArchiveCase:
    return _case(
        "valid_force_zip64",
        _write_unseekable(force_zip64=True),
        valid=True,
        category=None,
        description=(
            "Small streamed entry written with force_zip64: its local header uses the "
            "0xFFFFFFFF size sentinel plus a Zip64 extra field, and the archive is valid."
        ),
    )


def build_directory_payload_ratio_exceeded() -> ArchiveCase:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_fixed_info("manifest.json"), manifest_bytes())
        archive.writestr(_fixed_info("plugin.py"), plugin_bytes())
        info = _fixed_info("bigdir/")
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, b"\x00" * DIRECTORY_PAYLOAD_SIZE)
    return _case(
        "directory_payload_ratio_exceeded",
        buffer.getvalue(),
        valid=False,
        category="directory",
        description=(
            "A directory-named entry ('bigdir/') carries real payload that compresses "
            "far beyond the 100:1 ratio; validators that skip directory entries miss it."
        ),
    )


def build_valid_data_descriptor() -> ArchiveCase:
    return _case(
        "valid_data_descriptor",
        _write_unseekable(force_zip64=False),
        valid=True,
        category=None,
        description=(
            "Whole archive streamed to an unseekable sink, so entries set the "
            "data-descriptor flag (0x0008) and remain readable."
        ),
    )


def build_corrupted_descriptor_crc() -> ArchiveCase:
    raw = _write_unseekable(force_zip64=False)
    offset = data_descriptor_offset(raw, "data.bin")
    if raw[offset : offset + len(DATA_DESCRIPTOR_SIGNATURE)] == DATA_DESCRIPTOR_SIGNATURE:
        offset += len(DATA_DESCRIPTOR_SIGNATURE)
    patched = bytearray(raw)
    _patch_u32(patched, offset, _u32(raw, offset) ^ 0x1)
    return _case(
        "corrupted_descriptor_crc",
        bytes(patched),
        valid=False,
        category="integrity",
        description=(
            "The trailing data-descriptor CRC is corrupted while the central directory "
            "stays correct, so only raw descriptor inspection can detect it."
        ),
    )


def build_deeply_nested_manifest_json() -> ArchiveCase:
    nested = b"[" * NESTED_MANIFEST_DEPTH + b"0" + b"]" * NESTED_MANIFEST_DEPTH
    data = _zip({"manifest.json": nested, "plugin.py": plugin_bytes()})
    return _case(
        "deeply_nested_manifest_json",
        data,
        valid=False,
        category="manifest",
        description=(
            f"manifest.json is syntactically valid but nested {NESTED_MANIFEST_DEPTH} "
            "levels deep, so a recursive JSON parse raises RecursionError."
        ),
    )


def build_huge_min_core_version() -> ArchiveCase:
    digits = b"9" * MIN_CORE_VERSION_DIGITS
    manifest = (
        b'{"name": "hello", "version": "1.0.0", "entry": "plugin:HelloPlugin", '
        b'"auth_modes": ["none"], "min_core_version": ' + digits + b"}"
    )
    data = _zip({"manifest.json": manifest, "plugin.py": plugin_bytes()})
    return _case(
        "huge_min_core_version",
        data,
        valid=False,
        category="manifest",
        description=(
            f"manifest.json min_core_version is a {MIN_CORE_VERSION_DIGITS}-digit integer "
            "built as raw bytes; parsing exceeds the integer digit limit and the value "
            "cannot satisfy any installed core version."
        ),
    )


_STREAM_BUILDERS: dict[str, Callable[[], ArchiveCase]] = {
    "declared_size_prefix_crc_false_accept": build_declared_size_prefix_crc_false_accept,
    "declared_zero_actual_payload": build_declared_zero_actual_payload,
    "valid_force_zip64": build_valid_force_zip64,
    "directory_payload_ratio_exceeded": build_directory_payload_ratio_exceeded,
    "valid_data_descriptor": build_valid_data_descriptor,
    "corrupted_descriptor_crc": build_corrupted_descriptor_crc,
    "deeply_nested_manifest_json": build_deeply_nested_manifest_json,
    "huge_min_core_version": build_huge_min_core_version,
}


def stream_case_names() -> list[str]:
    """Return every extended-corpus case name in registration order."""
    return list(_STREAM_BUILDERS)


def build_stream_cases() -> dict[str, ArchiveCase]:
    """Extended declared-size/streaming corpus, separate from ``build_cases()``."""
    return {kind: builder() for kind, builder in _STREAM_BUILDERS.items()}


# ---------------------------------------------------------------------------
# Registry and mapping factory
# ---------------------------------------------------------------------------

_BUILDERS: dict[str, Callable[..., ArchiveCase]] = {
    "valid_root": build_valid_root,
    "valid_single_top_dir": build_valid_single_top_dir,
    "valid_relative_modules": build_valid_relative_modules,
    "valid_directory_records": build_valid_directory_records,
    "path_traversal_dotdot": build_path_traversal_dotdot,
    "path_traversal_nested_dotdot": build_path_traversal_nested_dotdot,
    "path_absolute_posix": build_path_absolute_posix,
    "path_windows_drive_letter": build_path_windows_drive_letter,
    "path_windows_drive_backslash": build_path_windows_drive_backslash,
    "path_windows_backslash": build_path_windows_backslash,
    "nul_filename": build_nul_filename,
    "symlink_unix": build_symlink_unix,
    "special_file_fifo": build_special_file_fifo,
    "special_file_char": build_special_file_char,
    "duplicate_normalized_file": build_duplicate_normalized_file,
    "duplicate_normalized_manifest": build_duplicate_normalized_manifest,
    "file_directory_conflict": build_file_directory_conflict,
    "too_many_entries_short": build_too_many_entries_short,
    "too_many_entries_default": build_too_many_entries_default,
    "ratio_exceeded": build_ratio_exceeded,
    "max_compressed_size_exceeded": build_max_compressed_size_exceeded,
    "max_uncompressed_size_exceeded": build_max_uncompressed_size_exceeded,
    "malformed_zip": build_malformed_zip,
    "truncated_zip": build_truncated_zip,
    "unsupported_compression": build_unsupported_compression,
    "encrypted_flag": build_encrypted_flag,
    "crc_mismatch": build_crc_mismatch,
    "uncompressed_size_mismatch": build_uncompressed_size_mismatch,
    "compressed_size_mismatch": build_compressed_size_mismatch,
    "two_top_level_dirs": build_two_top_level_dirs,
    "deep_enclosing_dir": build_deep_enclosing_dir,
    "root_and_nested_manifest": build_root_and_nested_manifest,
    "missing_manifest": build_missing_manifest,
    "invalid_manifest_json": build_invalid_manifest_json,
    "bad_slug": build_bad_slug,
    "auth_mode_conflict": build_auth_mode_conflict,
    "bad_entry": build_bad_entry,
    "bad_version": build_bad_version,
}

METADATA_PATCHED_CASES = frozenset(
    {
        "unsupported_compression",
        "encrypted_flag",
        "crc_mismatch",
        "uncompressed_size_mismatch",
        "compressed_size_mismatch",
    }
)


def case_names() -> list[str]:
    """Return every registered case name in registration order."""
    return list(_BUILDERS)


def make_case(kind: str, **options: Any) -> ArchiveCase:
    """Build one case by name, optionally overriding fixture thresholds."""
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"unknown archive case: {kind!r}; known cases: {sorted(_BUILDERS)}")
    return builder(**options)


def build_cases() -> dict[str, ArchiveCase]:
    """Mapping factory: every registered case, keyed by name, in order."""
    return {kind: builder() for kind, builder in _BUILDERS.items()}


__all__ = [
    "DATA_DESCRIPTOR_PAYLOAD",
    "DATA_DESCRIPTOR_SIGNATURE",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_RATIO",
    "DEFAULT_MAX_SIZE_BYTES",
    "DIRECTORY_PAYLOAD_SIZE",
    "EXPANSION_FIXTURE_MAX_SIZE_BYTES",
    "FALSE_ACCEPT_PAYLOAD",
    "FALSE_ACCEPT_PREFIX",
    "FORCE_ZIP64_PAYLOAD",
    "LOCAL_DATA_DESCRIPTOR_FLAG",
    "LOCAL_EXTRA_LENGTH_OFFSET",
    "METADATA_ENTRY",
    "METADATA_PATCHED_CASES",
    "METADATA_PAYLOAD",
    "MIN_CORE_VERSION_DIGITS",
    "NESTED_MANIFEST_DEPTH",
    "NUL_PLACEHOLDER",
    "NUL_TARGET",
    "SHORT_MAX_ENTRIES",
    "SHORT_MAX_SIZE_BYTES",
    "UNCOMPRESSED_EXPANSION_FACTOR",
    "UNSUPPORTED_COMPRESSION_METHOD",
    "VALID_AUTH_MODES",
    "VALID_ENTRY",
    "VALID_SLUG",
    "VALID_VERSION",
    "ZERO_DECLARED_PAYLOAD",
    "ZIP64_EXTRA_ID",
    "ArchiveCase",
    "build_cases",
    "build_corrupted_descriptor_crc",
    "build_declared_size_prefix_crc_false_accept",
    "build_declared_zero_actual_payload",
    "build_deeply_nested_manifest_json",
    "build_directory_payload_ratio_exceeded",
    "build_huge_min_core_version",
    "build_stream_cases",
    "build_valid_data_descriptor",
    "build_valid_force_zip64",
    "case_names",
    "central_compressed_size",
    "central_crc",
    "central_external_attributes",
    "central_filename",
    "central_flags",
    "central_header_offsets",
    "central_method",
    "central_uncompressed_size",
    "data_descriptor_crc",
    "data_descriptor_offset",
    "entry_compressed_bytes",
    "find_central_header",
    "find_local_header",
    "local_compressed_size",
    "local_crc",
    "local_data_offset",
    "local_extra",
    "local_filename",
    "local_flags",
    "local_header_offsets",
    "local_method",
    "local_uncompressed_size",
    "make_case",
    "manifest_bytes",
    "manifest_bytes_without",
    "plugin_bytes",
    "stream_case_names",
    "valid_manifest",
]
