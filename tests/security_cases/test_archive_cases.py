"""Behavioural tests for the crafted malicious-archive fixtures.

These tests never extract an archive.  They prove each crafted case is *actually*
malicious by re-reading raw ZIP header fields with :mod:`struct` and by observing
what :mod:`zipfile` does with the bytes.  A case that only claimed to be unsafe
in a comment would fail here.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import stat
import zipfile
import zlib
from collections.abc import Callable
from typing import Any

import pytest

from .archive_cases import (
    DATA_DESCRIPTOR_PAYLOAD,
    DATA_DESCRIPTOR_SIGNATURE,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_RATIO,
    DIRECTORY_PAYLOAD_SIZE,
    EXPANSION_FIXTURE_MAX_SIZE_BYTES,
    FALSE_ACCEPT_PAYLOAD,
    FALSE_ACCEPT_PREFIX,
    FORCE_ZIP64_PAYLOAD,
    LOCAL_DATA_DESCRIPTOR_FLAG,
    METADATA_ENTRY,
    METADATA_PATCHED_CASES,
    METADATA_PAYLOAD,
    MIN_CORE_VERSION_DIGITS,
    NESTED_MANIFEST_DEPTH,
    NUL_PLACEHOLDER,
    NUL_TARGET,
    SHORT_MAX_ENTRIES,
    SHORT_MAX_SIZE_BYTES,
    UNCOMPRESSED_EXPANSION_FACTOR,
    ZIP64_EXTRA_ID,
    ArchiveCase,
    build_cases,
    build_stream_cases,
    case_names,
    central_compressed_size,
    central_crc,
    central_external_attributes,
    central_filename,
    central_flags,
    central_header_offsets,
    central_method,
    central_uncompressed_size,
    data_descriptor_crc,
    data_descriptor_offset,
    entry_compressed_bytes,
    find_central_header,
    find_local_header,
    local_compressed_size,
    local_crc,
    local_data_offset,
    local_extra,
    local_filename,
    local_flags,
    local_header_offsets,
    local_method,
    local_uncompressed_size,
    make_case,
    manifest_bytes,
    stream_case_names,
)

CASES = build_cases()
VALID_KINDS = sorted(kind for kind, case in CASES.items() if case.expected_valid)
INVALID_KINDS = sorted(kind for kind, case in CASES.items() if not case.expected_valid)

PATH_EXPECTATIONS: dict[str, str] = {
    "path_traversal_dotdot": "../",
    "path_traversal_nested_dotdot": "../",
    "path_absolute_posix": "/",
    "path_windows_drive_letter": "C:/",
    "path_windows_drive_backslash": "C:\\",
    "path_windows_backslash": "\\",
}

MAX_CASE_BYTES = 2 * 1024 * 1024
MAX_SUITE_BYTES = 8 * 1024 * 1024

# Authoritative formats from ARCHITECTURE.md, used as independent oracles so the
# defect cases prove the *field* is bad, not merely that the archive parses.
SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
SEMVER_PATTERN = re.compile(r"\d+\.\d+\.\d+\Z")


def _open(case: ArchiveCase) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(case.data))


def _manifest_name(names: list[str]) -> str:
    for name in names:
        if name == "manifest.json" or name.endswith("/manifest.json"):
            return name
    raise AssertionError(f"no manifest entry in {names!r}")


def _read_manifest(case: ArchiveCase) -> dict[str, Any]:
    with _open(case) as archive:
        payload = archive.read(_manifest_name(archive.namelist()))
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _inflate(raw: bytes) -> bytes:
    """Decompress a raw ZIP DEFLATE stream (no zlib header/trailer)."""
    return zlib.decompress(raw, -zlib.MAX_WBITS)


# ---------------------------------------------------------------------------
# Registry / model contracts
# ---------------------------------------------------------------------------


def test_registry_has_valid_and_invalid_cases() -> None:
    assert len(CASES) == len(case_names())
    assert list(CASES) == case_names()
    assert len(CASES) == 38
    assert len(VALID_KINDS) == 5
    assert len(INVALID_KINDS) == 33


@pytest.mark.parametrize("kind", sorted(CASES))
def test_case_metadata_is_self_consistent(kind: str) -> None:
    case = CASES[kind]
    assert case.name == kind
    assert isinstance(case.data, bytes)
    assert case.data
    if case.expected_valid:
        assert case.rejection_category is None
    else:
        assert case.rejection_category
    assert case.description


def test_make_case_returns_registered_case() -> None:
    # Also proves fixtures are byte-for-byte deterministic (no clock stamping).
    for kind in case_names():
        assert make_case(kind).data == CASES[kind].data, kind


def test_make_case_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown archive case"):
        make_case("not_a_real_case")


def test_make_case_accepts_threshold_overrides() -> None:
    case = make_case("too_many_entries_short", max_entries_constant_override=3)
    assert case.validator_options["max_entries_constant_override"] == 3
    with _open(case) as archive:
        assert len(archive.infolist()) > 3


def test_from_mapping_round_trip() -> None:
    original = CASES["valid_root"]
    restored = ArchiveCase.from_mapping(original.to_mapping())
    assert restored == original


def test_case_sizes_are_bounded() -> None:
    assert max(len(case.data) for case in CASES.values()) < MAX_CASE_BYTES
    assert sum(len(case.data) for case in CASES.values()) < MAX_SUITE_BYTES


# ---------------------------------------------------------------------------
# Valid cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_valid_cases_are_well_formed_packages(kind: str) -> None:
    case = CASES[kind]
    with _open(case) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        manifest = json.loads(archive.read(_manifest_name(names)))
    assert manifest["name"] == "hello"
    assert manifest["version"] == "1.0.0"
    assert manifest["entry"] == "plugin:HelloPlugin"
    assert manifest["auth_modes"] == ["none"]
    assert any(name.endswith("plugin.py") for name in names)


def test_valid_single_top_dir_encloses_all_files() -> None:
    with _open(CASES["valid_single_top_dir"]) as archive:
        assert all(name.startswith("hello/") for name in archive.namelist())


def test_valid_relative_modules_contain_nested_package() -> None:
    with _open(CASES["valid_relative_modules"]) as archive:
        names = archive.namelist()
    assert "helper/__init__.py" in names
    assert "helper/util.py" in names


# ---------------------------------------------------------------------------
# Invalid-case bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_invalid_cases_declare_a_rejection_category(kind: str) -> None:
    case = CASES[kind]
    assert case.expected_valid is False
    assert case.rejection_category


# ---------------------------------------------------------------------------
# Path traversal / absolute / backslash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(PATH_EXPECTATIONS))
def test_path_cases_contain_a_forbidden_name(kind: str) -> None:
    needle = PATH_EXPECTATIONS[kind]
    with _open(CASES[kind]) as archive:
        names = archive.namelist()
    assert any(needle in name for name in names), names
    assert any(name == "manifest.json" or name.endswith("/manifest.json") for name in names)


# ---------------------------------------------------------------------------
# Raw NUL filename
# ---------------------------------------------------------------------------


def test_nul_case_embeds_raw_nul_in_both_headers() -> None:
    data = CASES["nul_filename"].data
    assert data.count(NUL_TARGET) == 2
    assert NUL_PLACEHOLDER.encode("ascii") not in data

    local_names = [local_filename(data, offset) for offset in local_header_offsets(data)]
    central_names = [central_filename(data, offset) for offset in central_header_offsets(data)]
    assert NUL_TARGET in local_names
    assert NUL_TARGET in central_names


def test_nul_case_is_hidden_by_zipfile_but_present_raw() -> None:
    data = CASES["nul_filename"].data
    with _open(CASES["nul_filename"]) as archive:
        # ZipInfo truncates at NUL, so library-level inspection cannot see it.
        assert all("\x00" not in info.filename for info in archive.infolist())
    assert any(b"\x00" in local_filename(data, off) for off in local_header_offsets(data))


# ---------------------------------------------------------------------------
# Symlink / special files
# ---------------------------------------------------------------------------


def test_symlink_case_marks_unix_symlink_metadata() -> None:
    with _open(CASES["symlink_unix"]) as archive:
        infos = archive.infolist()
        links = [info for info in infos if stat.S_ISLNK(info.external_attr >> 16)]
        assert len(links) == 1
        assert links[0].create_system == 3
        assert archive.read(links[0]) == b"/etc/passwd"


@pytest.mark.parametrize(
    ("kind", "predicate"),
    [
        ("special_file_fifo", stat.S_ISFIFO),
        ("special_file_char", stat.S_ISCHR),
    ],
)
def test_special_file_cases_have_non_regular_modes(
    kind: str, predicate: Callable[[int], bool]
) -> None:
    with _open(CASES[kind]) as archive:
        modes = [info.external_attr >> 16 for info in archive.infolist()]
    assert any(predicate(mode) for mode in modes), modes


# ---------------------------------------------------------------------------
# Duplicate destinations / directory conflicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["duplicate_normalized_file", "duplicate_normalized_manifest"],
)
def test_duplicate_cases_collide_after_normalisation(kind: str) -> None:
    with _open(CASES[kind]) as archive:
        names = archive.namelist()
    normalised = [posixpath.normpath(name) for name in names]
    assert len(normalised) != len(set(normalised))


def test_file_directory_conflict_uses_file_name_as_directory() -> None:
    with _open(CASES["file_directory_conflict"]) as archive:
        names = archive.namelist()
    assert "pkg" in names
    assert any(name.startswith("pkg/") for name in names)
    assert not any(name == "pkg/" for name in names)


# ---------------------------------------------------------------------------
# Count / ratio / size thresholds
# ---------------------------------------------------------------------------


def test_too_many_entries_short_exceeds_threshold() -> None:
    case = CASES["too_many_entries_short"]
    limit = case.validator_options["max_entries_constant_override"]
    assert limit == SHORT_MAX_ENTRIES
    assert "MAX_ENTRY_COUNT" in case.description
    with _open(case) as archive:
        assert len(archive.infolist()) > limit


def test_too_many_entries_default_exceeds_real_bound() -> None:
    with _open(CASES["too_many_entries_default"]) as archive:
        assert len(archive.infolist()) == DEFAULT_MAX_ENTRIES + 3


def test_ratio_case_exceeds_threshold_without_huge_allocation() -> None:
    case = CASES["ratio_exceeded"]
    assert case.validator_options == {}
    with _open(case) as archive:
        ratios = [
            info.file_size / info.compress_size
            for info in archive.infolist()
            if info.compress_size
        ]
        total_uncompressed = sum(info.file_size for info in archive.infolist())
    assert max(ratios) > DEFAULT_MAX_RATIO
    assert total_uncompressed < MAX_CASE_BYTES


def test_max_compressed_size_case_exceeds_threshold() -> None:
    case = CASES["max_compressed_size_exceeded"]
    assert case.validator_options["max_size_bytes"] == SHORT_MAX_SIZE_BYTES
    assert len(case.data) > SHORT_MAX_SIZE_BYTES


def test_max_uncompressed_size_case_hits_the_expansion_guard_exactly() -> None:
    case = CASES["max_uncompressed_size_exceeded"]
    limit = case.validator_options["max_size_bytes"]
    assert limit == EXPANSION_FIXTURE_MAX_SIZE_BYTES
    # Compressed archive stays within the configured size limit ...
    assert len(case.data) <= limit
    # ... so the only violated guard is total uncompressed > factor * limit ...
    with _open(case) as archive:
        infos = archive.infolist()
        total_uncompressed = sum(info.file_size for info in infos)
        max_entry_ratio = max(
            info.file_size / info.compress_size for info in infos if info.compress_size
        )
    assert total_uncompressed > UNCOMPRESSED_EXPANSION_FACTOR * limit
    # ... and no individual entry trips the separate 100:1 ratio constant.
    assert max_entry_ratio <= DEFAULT_MAX_RATIO


# ---------------------------------------------------------------------------
# Malformed / truncated archives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["malformed_zip", "truncated_zip"])
def test_broken_archives_raise_badzipfile(kind: str) -> None:
    with pytest.raises(zipfile.BadZipFile):
        _open(CASES[kind])


# ---------------------------------------------------------------------------
# Crafted metadata cases
# ---------------------------------------------------------------------------


def test_unsupported_compression_metadata_is_patched_raw() -> None:
    data = CASES["unsupported_compression"].data
    local = find_local_header(data, METADATA_ENTRY)
    central = find_central_header(data, METADATA_ENTRY)
    assert local_method(data, local) == 99
    assert central_method(data, central) == 99
    with _open(CASES["unsupported_compression"]) as archive:
        assert archive.getinfo(METADATA_ENTRY).compress_type == 99
        assert archive.read("manifest.json")
        with pytest.raises(NotImplementedError):
            archive.read(METADATA_ENTRY)


def test_encrypted_flag_is_patched_raw() -> None:
    data = CASES["encrypted_flag"].data
    local = find_local_header(data, METADATA_ENTRY)
    central = find_central_header(data, METADATA_ENTRY)
    assert local_flags(data, local) & 0x1
    assert central_flags(data, central) & 0x1
    # Other entries stay unencrypted: only the crafted entry is hostile.
    assert not local_flags(data, find_local_header(data, "manifest.json")) & 0x1
    with _open(CASES["encrypted_flag"]) as archive:
        assert archive.read("manifest.json")
        with pytest.raises(RuntimeError):
            archive.read(METADATA_ENTRY)


def test_crc_mismatch_is_patched_raw_and_detected() -> None:
    data = CASES["crc_mismatch"].data
    local = find_local_header(data, METADATA_ENTRY)
    central = find_central_header(data, METADATA_ENTRY)
    expected_crc = zlib.crc32(METADATA_PAYLOAD)
    assert local_crc(data, local) != expected_crc
    assert central_crc(data, central) != expected_crc
    with _open(CASES["crc_mismatch"]) as archive:
        assert archive.testzip() == METADATA_ENTRY
        with pytest.raises(zipfile.BadZipFile):
            archive.read(METADATA_ENTRY)


def test_uncompressed_size_mismatch_is_patched_raw() -> None:
    data = CASES["uncompressed_size_mismatch"].data
    local = find_local_header(data, METADATA_ENTRY)
    central = find_central_header(data, METADATA_ENTRY)
    assert local_uncompressed_size(data, local) != central_uncompressed_size(data, central)
    with _open(CASES["uncompressed_size_mismatch"]) as archive:
        assert archive.getinfo(METADATA_ENTRY).file_size == len(METADATA_PAYLOAD)


def test_compressed_size_mismatch_is_patched_raw() -> None:
    data = CASES["compressed_size_mismatch"].data
    local = find_local_header(data, METADATA_ENTRY)
    central = find_central_header(data, METADATA_ENTRY)
    assert local_compressed_size(data, local) != central_compressed_size(data, central)
    with _open(CASES["compressed_size_mismatch"]) as archive:
        assert archive.getinfo(METADATA_ENTRY).compress_size == central_compressed_size(
            data, central
        )


@pytest.mark.parametrize("kind", sorted(METADATA_PATCHED_CASES))
def test_metadata_patched_cases_keep_the_rest_valid(kind: str) -> None:
    """Only the crafted entry is hostile; the manifest stays trustworthy."""
    with _open(CASES[kind]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("plugin.py")
    assert manifest == json.loads(manifest_bytes())


# ---------------------------------------------------------------------------
# Enclosing layout / manifest defects
# ---------------------------------------------------------------------------


def test_two_top_level_dirs_is_ambiguous() -> None:
    with _open(CASES["two_top_level_dirs"]) as archive:
        names = archive.namelist()
    top_level = {name.split("/", 1)[0] for name in names if "/" in name}
    assert top_level == {"a", "b"}


def test_deep_enclosing_dir_exceeds_single_strip() -> None:
    with _open(CASES["deep_enclosing_dir"]) as archive:
        names = archive.namelist()
    assert all(name.startswith("a/b/") for name in names)


def test_root_and_nested_manifest_treats_nested_as_data() -> None:
    case = CASES["root_and_nested_manifest"]
    assert case.expected_valid is True
    assert case.rejection_category is None
    with _open(case) as archive:
        names = archive.namelist()
    assert "manifest.json" in names
    assert "nested/manifest.json" in names
    # The root manifest remains authoritative (checked by the valid-case test).


def test_manifest_defects_carry_the_specific_bad_field() -> None:
    """Each manifest fixture fails for its declared reason, verified per field."""

    # missing_manifest: no manifest entry exists at all.
    with _open(CASES["missing_manifest"]) as archive:
        names = archive.namelist()
    assert not any(name == "manifest.json" or name.endswith("/manifest.json") for name in names)
    assert "plugin.py" in names

    # invalid_manifest_json: the entry exists but JSON decoding fails.
    with _open(CASES["invalid_manifest_json"]) as archive:
        payload = archive.read("manifest.json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(payload)

    # bad_entry: entry is present but is not a module:ClassName reference.
    entry = _read_manifest(CASES["bad_entry"])["entry"]
    assert entry == "plugin"
    assert ":" not in str(entry)

    # auth_mode_conflict: 'none' coexists with another supported mode.
    modes = _read_manifest(CASES["auth_mode_conflict"])["auth_modes"]
    assert isinstance(modes, list)
    assert "none" in modes
    assert len(modes) > 1

    # bad_version: version is not a full MAJOR.MINOR.PATCH semver.
    version = str(_read_manifest(CASES["bad_version"])["version"])
    assert version == "1.0"
    assert SEMVER_PATTERN.match(version) is None

    # bad_slug: name violates the slug character pattern.
    name = str(_read_manifest(CASES["bad_slug"])["name"])
    assert name == "Hello World!"
    assert SLUG_PATTERN.match(name) is None


def test_valid_manifest_satisfies_the_same_oracles() -> None:
    """Positive control: the valid fixture passes the defect oracles above."""
    manifest = _read_manifest(CASES["valid_root"])
    assert SLUG_PATTERN.match(str(manifest["name"])) is not None
    assert SEMVER_PATTERN.match(str(manifest["version"])) is not None
    assert ":" in str(manifest["entry"])
    assert manifest["auth_modes"] == ["none"]


def test_symlink_and_special_entries_use_unix_external_attributes() -> None:
    for kind in ("symlink_unix", "special_file_fifo", "special_file_char"):
        data = CASES[kind].data
        modes = [
            central_external_attributes(data, off) >> 16
            for off in central_header_offsets(data)
        ]
        assert any(mode & 0o170000 for mode in modes), kind


# ---------------------------------------------------------------------------
# Extended declared-size / streaming corpus (build_stream_cases)
# ---------------------------------------------------------------------------

STREAM_CASES = build_stream_cases()
STREAM_VALID = sorted(kind for kind, case in STREAM_CASES.items() if case.expected_valid)
STREAM_INVALID = sorted(kind for kind, case in STREAM_CASES.items() if not case.expected_valid)


def test_stream_corpus_is_separate_from_primary_corpus() -> None:
    assert len(STREAM_CASES) == 8
    assert list(STREAM_CASES) == stream_case_names()
    assert len(STREAM_VALID) == 2
    assert len(STREAM_INVALID) == 6
    # Extension must not overlap or disturb the stable primary registry.
    assert not set(STREAM_CASES) & set(CASES)


@pytest.mark.parametrize("kind", sorted(STREAM_CASES))
def test_stream_case_metadata_is_self_consistent(kind: str) -> None:
    case = STREAM_CASES[kind]
    assert case.name == kind
    assert isinstance(case.data, bytes)
    assert case.data
    if case.expected_valid:
        assert case.rejection_category is None
    else:
        assert case.rejection_category
    assert case.description


def test_stream_fixtures_are_deterministic() -> None:
    rebuilt = build_stream_cases()
    for kind in stream_case_names():
        assert rebuilt[kind].data == STREAM_CASES[kind].data, kind


@pytest.mark.parametrize("kind", STREAM_VALID)
def test_stream_valid_cases_are_well_formed(kind: str) -> None:
    with _open(STREAM_CASES[kind]) as archive:
        assert archive.testzip() is None
        assert any(name.endswith("plugin.py") for name in archive.namelist())


@pytest.mark.parametrize("kind", STREAM_INVALID)
def test_stream_invalid_cases_declare_a_category(kind: str) -> None:
    case = STREAM_CASES[kind]
    assert case.expected_valid is False
    assert case.rejection_category


def test_declared_size_prefix_crc_false_accept_is_precisely_crafted() -> None:
    data = STREAM_CASES["declared_size_prefix_crc_false_accept"].data
    local = find_local_header(data, "data.bin")
    central = find_central_header(data, "data.bin")

    declared = len(FALSE_ACCEPT_PREFIX)
    assert declared == 10
    assert local_uncompressed_size(data, local) == declared
    assert central_uncompressed_size(data, central) == declared

    prefix_crc = zlib.crc32(FALSE_ACCEPT_PREFIX)
    assert local_crc(data, local) == prefix_crc
    assert central_crc(data, central) == prefix_crc

    # Compressed size is preserved, so the stream is far larger than declared.
    compressed = central_compressed_size(data, central)
    assert compressed == local_compressed_size(data, local)
    assert compressed > declared

    raw = entry_compressed_bytes(data, "data.bin")
    # Bounded proof: reading exactly the declared size yields the prefix and the
    # stream is not yet finished, i.e. real data exceeds the declaration.
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    head = decompressor.decompress(raw, declared)
    assert head == FALSE_ACCEPT_PREFIX
    assert not decompressor.eof

    # Independent oracle: the true output length and CRC contradict the fields.
    actual = _inflate(raw)
    assert len(actual) == len(FALSE_ACCEPT_PAYLOAD) > declared
    assert zlib.crc32(actual) != prefix_crc


def test_declared_zero_actual_payload_has_real_data() -> None:
    data = STREAM_CASES["declared_zero_actual_payload"].data
    local = find_local_header(data, "data.bin")
    central = find_central_header(data, "data.bin")
    assert local_uncompressed_size(data, local) == 0
    assert central_uncompressed_size(data, central) == 0
    actual = _inflate(entry_compressed_bytes(data, "data.bin"))
    assert len(actual) > 0
    # The stored CRC still describes the real payload, contradicting size zero.
    assert central_crc(data, central) == zlib.crc32(actual)


def test_valid_force_zip64_uses_sentinel_and_local_zip64_extra() -> None:
    case = STREAM_CASES["valid_force_zip64"]
    data = case.data
    local = find_local_header(data, "data.bin")
    assert local_uncompressed_size(data, local) == 0xFFFFFFFF
    assert local_compressed_size(data, local) == 0xFFFFFFFF
    assert ZIP64_EXTRA_ID in local_extra(data, local)
    # A sibling entry is ordinary, so only the forced entry carries the sentinel.
    plain = find_local_header(data, "manifest.json")
    assert local_uncompressed_size(data, plain) != 0xFFFFFFFF
    with _open(case) as archive:
        assert archive.testzip() is None
        assert archive.read("data.bin") == FORCE_ZIP64_PAYLOAD


def test_directory_payload_entry_exceeds_ratio() -> None:
    with _open(STREAM_CASES["directory_payload_ratio_exceeded"]) as archive:
        info = archive.getinfo("bigdir/")
        assert info.is_dir()
        ratio = info.file_size / info.compress_size
    assert info.file_size >= DIRECTORY_PAYLOAD_SIZE
    assert ratio > DEFAULT_MAX_RATIO


def test_valid_data_descriptor_sets_flag_and_stays_readable() -> None:
    case = STREAM_CASES["valid_data_descriptor"]
    for name in ("manifest.json", "plugin.py", "data.bin"):
        local = find_local_header(case.data, name)
        assert local_flags(case.data, local) & LOCAL_DATA_DESCRIPTOR_FLAG, name
    with _open(case) as archive:
        assert archive.testzip() is None
        assert archive.read("data.bin") == DATA_DESCRIPTOR_PAYLOAD


def test_corrupted_descriptor_crc_is_raw_and_hidden_from_central_reads() -> None:
    data = STREAM_CASES["corrupted_descriptor_crc"].data
    local = find_local_header(data, "data.bin")
    assert local_flags(data, local) & LOCAL_DATA_DESCRIPTOR_FLAG

    offset = data_descriptor_offset(data, "data.bin")
    assert data[offset : offset + len(DATA_DESCRIPTOR_SIGNATURE)] == DATA_DESCRIPTOR_SIGNATURE
    assert offset > local_data_offset(data, local)

    actual = _inflate(entry_compressed_bytes(data, "data.bin"))
    assert data_descriptor_crc(data, "data.bin") != zlib.crc32(actual)
    # The central directory CRC is still correct, so ZipFile alone cannot see the
    # corrupted descriptor; only raw inspection detects it.
    with _open(STREAM_CASES["corrupted_descriptor_crc"]) as archive:
        assert archive.testzip() is None
        assert archive.read("data.bin") == DATA_DESCRIPTOR_PAYLOAD


def test_deeply_nested_manifest_json_raises_recursion_error() -> None:
    with _open(STREAM_CASES["deeply_nested_manifest_json"]) as archive:
        payload = archive.read("manifest.json")
    assert payload.startswith(b"[")
    assert payload.count(b"[") == NESTED_MANIFEST_DEPTH
    with pytest.raises(RecursionError):
        json.loads(payload)


def test_huge_min_core_version_is_raw_and_unparseable() -> None:
    with _open(STREAM_CASES["huge_min_core_version"]) as archive:
        payload = archive.read("manifest.json")
    assert payload.count(b"9") == MIN_CORE_VERSION_DIGITS
    assert b'"min_core_version":' in payload
    with pytest.raises(ValueError):
        json.loads(payload)
