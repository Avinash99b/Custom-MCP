"""Integration of the real validator against the malicious-archive corpus.

The corpus lives in ``tests/security_cases`` (owned by a separate fixture work
unit) and is read-only here. ``validator_options`` is a hint mapping; only
``max_size_bytes`` is a first-class argument, so the remaining bound hints are
applied by patching the module constants the validator actually uses.
"""

from __future__ import annotations

import pytest

from custom_mcp.plugins import packaging
from custom_mcp.plugins.packaging import InvalidPluginError, validate_package
from tests.security_cases.archive_cases import build_cases, build_stream_cases

DEFAULT_MAX_SIZE = 20 * 1024 * 1024

# Precise, validator-specific error fragments per invalid case. Multiple entries
# allow genuinely equivalent diagnostics.
EXPECTED_FRAGMENT: dict[str, tuple[str, ...]] = {
    "path_traversal_dotdot": ("unsafe path",),
    "path_traversal_nested_dotdot": ("unsafe path",),
    "path_absolute_posix": ("absolute path",),
    "path_windows_drive_letter": ("drive-qualified",),
    "path_windows_drive_backslash": ("backslash",),
    "path_windows_backslash": ("backslash",),
    "nul_filename": ("NUL byte",),
    "symlink_unix": ("symbolic link",),
    "special_file_fifo": ("special file",),
    "special_file_char": ("special file",),
    "duplicate_normalized_file": ("unsafe path",),
    "duplicate_normalized_manifest": ("unsafe path",),
    "file_directory_conflict": ("file/directory conflict",),
    "too_many_entries_short": ("more than",),
    "too_many_entries_default": ("more than",),
    "ratio_exceeded": ("ratio",),
    "max_compressed_size_exceeded": ("size limit",),
    "max_uncompressed_size_exceeded": ("exceeds",),
    "malformed_zip": ("not a valid ZIP",),
    "truncated_zip": ("not a valid ZIP",),
    "unsupported_compression": ("unsupported compression",),
    "encrypted_flag": ("encrypted",),
    "crc_mismatch": ("integrity",),
    "uncompressed_size_mismatch": ("mismatch",),
    "compressed_size_mismatch": ("mismatch",),
    "two_top_level_dirs": ("top-level directory",),
    "deep_enclosing_dir": ("manifest.json",),
    "missing_manifest": ("manifest.json",),
    "invalid_manifest_json": ("not valid JSON",),
    "bad_slug": ("slug",),
    "auth_mode_conflict": ("auth mode",),
    "bad_entry": ("entry",),
    "bad_version": ("version",),
}

CASES = build_cases()
PARAM_NAMES = sorted(CASES)


def test_expectations_cover_every_invalid_case() -> None:
    invalid = {name for name, case in CASES.items() if not case.expected_valid}
    assert invalid == set(EXPECTED_FRAGMENT)


def test_corpus_has_expected_shape() -> None:
    assert len(CASES) == 38
    assert sum(1 for case in CASES.values() if case.expected_valid) == 5


@pytest.mark.parametrize("name", PARAM_NAMES)
def test_security_case_matches_expectation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = CASES[name]
    options = dict(case.validator_options)
    if "max_entries_constant_override" in options:
        monkeypatch.setattr(
            packaging, "MAX_ENTRY_COUNT", options["max_entries_constant_override"]
        )
    max_size = options.get("max_size_bytes", DEFAULT_MAX_SIZE)

    if case.expected_valid:
        result = validate_package(case.data, max_size_bytes=max_size)
        assert result.manifest.name == "hello"
        return

    with pytest.raises(InvalidPluginError) as excinfo:
        validate_package(case.data, max_size_bytes=max_size)
    message = str(excinfo.value)
    assert any(fragment in message for fragment in EXPECTED_FRAGMENT[name]), message


def test_root_manifest_authoritative_over_nested_manifest() -> None:
    """A root manifest wins; additional nested files are legitimate data."""
    case = CASES["root_and_nested_manifest"]
    result = validate_package(case.data, max_size_bytes=DEFAULT_MAX_SIZE)
    assert result.root_prefix == ""
    assert result.manifest.name == "hello"
    assert "nested/manifest.json" in {entry.path for entry in result.entries}


@pytest.mark.parametrize("name", ["valid_root", "valid_single_top_dir"])
def test_valid_cases_produce_no_warnings(name: str) -> None:
    case = CASES[name]
    result = validate_package(case.data, max_size_bytes=DEFAULT_MAX_SIZE)
    assert result.warnings == ()


# --- extended declared-size / streaming corpus ------------------------------

STREAM_CASES = build_stream_cases()
STREAM_VALID = {"valid_force_zip64", "valid_data_descriptor"}
STREAM_FRAGMENT: dict[str, tuple[str, ...]] = {
    "declared_size_prefix_crc_false_accept": ("expands beyond", "declared size"),
    "declared_zero_actual_payload": ("expands beyond", "declared size"),
    "directory_payload_ratio_exceeded": ("directory entry contains data", "directory"),
    "corrupted_descriptor_crc": ("data descriptor",),
    "deeply_nested_manifest_json": ("ratio", "too deeply nested"),
    "huge_min_core_version": ("not valid JSON", "manifest"),
}


def test_stream_expectations_cover_cases() -> None:
    assert len(STREAM_CASES) == 8
    assert {name for name, case in STREAM_CASES.items() if case.expected_valid} == STREAM_VALID
    invalid = {name for name, case in STREAM_CASES.items() if not case.expected_valid}
    assert invalid == set(STREAM_FRAGMENT)


@pytest.mark.parametrize("name", sorted(STREAM_CASES))
def test_stream_case_matches_expectation(name: str) -> None:
    case = STREAM_CASES[name]
    max_size = dict(case.validator_options).get("max_size_bytes", DEFAULT_MAX_SIZE)
    if case.expected_valid:
        result = validate_package(case.data, max_size_bytes=max_size)
        assert result.manifest.name == "hello"
        return
    with pytest.raises(InvalidPluginError) as excinfo:
        validate_package(case.data, max_size_bytes=max_size)
    message = str(excinfo.value)
    assert any(fragment in message for fragment in STREAM_FRAGMENT[name]), message
