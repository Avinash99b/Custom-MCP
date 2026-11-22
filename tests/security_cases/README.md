# `tests/security_cases` — malicious ZIP fixtures

Self-contained, dependency-free fixture builders that a later Phase 1 integration
test can use to prove package-validation rejects hostile archives. Nothing here
imports `custom_mcp` or `mcp_plugin_sdk`; the package only needs the standard
library, so it is safe to import while the implementation is still in flux.

## Files

- `archive_cases.py` — `ArchiveCase` model, fixture builders, raw ZIP header
  readers and the mapping factory (`build_cases()` / `make_case()`).
- `test_archive_cases.py` — proves each crafted case is *actually* malicious by
  re-reading raw header bytes and observing `zipfile`. No archive is extracted.

## Model

```python
@dataclass(frozen=True, slots=True)
class ArchiveCase:
    name: str                          # descriptive identifier == registry key
    data: bytes                        # exact ZIP bytes to feed the validator
    expected_valid: bool
    rejection_category: str | None     # short substring, None when valid
    description: str
    validator_options: Mapping[str, int]  # thresholds to invoke validation with
```

`rejection_category` is deliberately loose: a validator may word its error
differently while still addressing the same defect. Categories currently used:
`path`, `nul`, `symlink`, `special`, `duplicate`, `directory`, `entries`,
`ratio`, `size`, `malformed`, `compression`, `encrypted`, `crc`, `layout`,
`manifest`, `slug`, `auth`, `entry`, `version`.

## Public API

```python
from tests.security_cases.archive_cases import (
    ArchiveCase,
    build_cases,     # -> dict[str, ArchiveCase]  (mapping factory, ordered)
    make_case,       # make_case(kind, **threshold_overrides) -> ArchiveCase
    case_names,      # -> list[str]
    valid_manifest, manifest_bytes, manifest_bytes_without, plugin_bytes,
    # raw ZIP header readers (local + central directory)
    local_header_offsets, central_header_offsets,
    local_filename, central_filename,
    local_flags, central_flags, local_method, central_method,
    local_crc, central_crc,
    local_compressed_size, central_compressed_size,
    local_uncompressed_size, central_uncompressed_size,
    central_external_attributes,
    find_local_header, find_central_header,
)
```

Valid cases (5): `valid_root`, `valid_single_top_dir`, `valid_relative_modules`,
`valid_directory_records`, `root_and_nested_manifest`.

`root_and_nested_manifest` is intentionally **valid**: when a root
`manifest.json` exists it is authoritative, and a nested `manifest.json` is just
ordinary extra package data — not a layout violation.

Crafted-metadata cases (`METADATA_PATCHED_CASES`) touch only the added
`data.bin` entry, so the `manifest.json`/`plugin.py` in the same archive stay
readable and valid. This proves a validator rejects the hostile field rather
than the whole archive being unparseable.

## Extended corpus: `build_stream_cases()`

`build_stream_cases()` returns a **separate** `dict[str, ArchiveCase]` (same
model) covering declared-size and streaming defects. It is intentionally not
merged into `build_cases()` so the primary corpus keeps its exact 38-case
contract for existing consumers. Use `stream_case_names()` for the ordered keys.

| Case | Valid | Category | What it proves |
| --- | --- | --- | --- |
| `declared_size_prefix_crc_false_accept` | no | `size` | Declared size 10 + prefix CRC while compressed size is preserved; only the true decode reveals the 1 MiB payload. |
| `declared_zero_actual_payload` | no | `size` | Declared uncompressed size 0 but the stream still yields real bytes (CRC stays true). |
| `valid_force_zip64` | yes | — | Local header uses the `0xFFFFFFFF` sentinel plus a Zip64 extra field. |
| `directory_payload_ratio_exceeded` | no | `directory` | A `bigdir/` directory entry carries payload with >100:1 ratio. |
| `valid_data_descriptor` | yes | — | Entries streamed to an unseekable sink set the `0x0008` data-descriptor flag. |
| `corrupted_descriptor_crc` | no | `integrity` | Trailing data-descriptor CRC is wrong while the central directory stays correct. |
| `deeply_nested_manifest_json` | no | `manifest` | Valid-but-100 000-deep nested manifest JSON triggers `RecursionError`. |
| `huge_min_core_version` | no | `manifest` | Raw 5000-digit `min_core_version` integer is unparseable/unbounded. |

Raw helpers for these: `entry_compressed_bytes`, `local_data_offset`,
`local_extra`, `data_descriptor_offset`, `data_descriptor_crc`, plus the
constants `DATA_DESCRIPTOR_SIGNATURE`, `ZIP64_EXTRA_ID`,
`LOCAL_DATA_DESCRIPTOR_FLAG`, and the payload constants
(`FALSE_ACCEPT_PREFIX`/`FALSE_ACCEPT_PAYLOAD`, `ZERO_DECLARED_PAYLOAD`,
`DATA_DESCRIPTOR_PAYLOAD`, `FORCE_ZIP64_PAYLOAD`, `DIRECTORY_PAYLOAD_SIZE`,
`NESTED_MANIFEST_DEPTH`, `MIN_CORE_VERSION_DIGITS`). Fixtures use fixed
timestamps and stdlib only (`zipfile`, `zlib`, `struct`, `json`, `random`); no
production imports, no extraction, no subprocess/network/Redis.

## Downstream usage

`validate_package` per `PHASE_1_PLAN.md` takes `zip_bytes`, `max_size_bytes` and
`core_version`. `validate_package` exposes exactly one configurable size knob,
`max_size_bytes`; the other guards are fixed module constants. Map them
explicitly so coverage is never ambiguous:

```python
import pytest
from tests.security_cases.archive_cases import build_cases

def _validate(case, monkeypatch=None):
    opts = case.validator_options
    if "max_entries_constant_override" in opts:
        # The API bound is a constant, so monkeypatch it for the short fixture.
        monkeypatch.setattr(packaging, "MAX_ENTRY_COUNT",
                            opts["max_entries_constant_override"])
    return validate_package(
        case.data,
        max_size_bytes=opts.get("max_size_bytes", DEFAULT_MAX_PLUGIN_BYTES),
        core_version="0.1.0",
    )

@pytest.mark.parametrize("kind", sorted(build_cases()))
def test_validation_matches_expectation(kind, monkeypatch):
    case = build_cases()[kind]
    if case.expected_valid:
        result = _validate(case, monkeypatch)
        assert result.manifest.name == "hello"
    else:
        with pytest.raises(InvalidPackage) as excinfo:
            _validate(case, monkeypatch)
        assert case.rejection_category in str(excinfo.value).lower()
```

`validator_options` keys are hints, not a binding signature:

| key | cases | meaning |
| --- | --- | --- |
| `max_size_bytes` | `max_compressed_size_exceeded` (128), `max_uncompressed_size_exceeded` (12288) | the one real validator parameter |
| `max_entries_constant_override` | `too_many_entries_short` (8) | monkeypatch `MAX_ENTRY_COUNT`, default 2000 |

Fixed constants targeted without options: `MAX_ENTRY_COUNT` (2000, exercised by
`too_many_entries_default`) and the 100:1 per-entry ratio (exercised by
`ratio_exceeded`). `max_uncompressed_size_exceeded` does **not** declare an
independent uncompressed limit: the validator derives its total-uncompressed
guard as `5 * max_size_bytes` (`UNCOMPRESSED_EXPANSION_FACTOR`). That fixture is
built so the archive fits within `max_size_bytes`, its total uncompressed bytes
exceed `5 * max_size_bytes`, and every entry stays under 100:1 — so it isolates
the total-expansion guard. The tiny limits keep the suite fast and bounded; the
real deployment defaults remain in `ARCHITECTURE.md`.

## Why raw readers exist

`zipfile.ZipInfo` truncates a filename at the first NUL, so a library-level
inspection cannot see the malicious byte. `nul_filename` therefore mutates the
local and central filename bytes in place (equal-length replacement), and the
tests assert the raw NUL is present in both headers while `zipfile` hides it.
Likewise `encrypted_flag`, `unsupported_compression`, `crc_mismatch` and the
size-mismatch cases are verified by re-reading the actual header fields with
`struct`, never by trusting a label.
