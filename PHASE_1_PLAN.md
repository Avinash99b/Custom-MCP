# Phase 1 task boundaries (pending Phase 0 gate)

This plan does not authorize implementation before Phase 0 is verified complete.

## Independent work units

1. **SDK and package validation**
   - Own `src/mcp_plugin_sdk/`, `src/custom_mcp/plugins/manifest.py`,
     `packaging.py`, relevant new package initializers and focused unit tests.
   - SDK is dependency-free and implements the exact public dataclasses,
     enums, protocols and synchronous abstract/hooks contract in ARCHITECTURE.md.
   - Manifest validation has specific errors, immutable parsed fields, valid
     slug/entry/semver/auth combinations and supported minimum core version.
   - Packaging exposes a validate-only operation returning a prepared manifest,
     safe normalized entry map and lint warnings, plus scratch-only extraction.
     It validates all metadata and manifest before writing any file.

2. **Registry and import/remove REST integration**
   - Sequence after package interfaces stabilize; own manager, API plugins,
     app wiring, necessary Redis protocol expansion, integration tests and
     packaging registration in pyproject/lock (SDK must ship in the wheel).
   - Phase 1 stores and validates code without loading/auto-connect/MCP mounts.
   - Per-slug records remain compatible with eventual PluginRuntime and stable
     lifecycle lock; no placeholder tool execution is invented.
   - Package publication uses scratch, atomic same-filesystem moves and rollback
     of disk/Redis registration on failed publication. Preserve existing code
     until replacement support is implemented transactionally in Phase 3.

## Proposed package-validation contract

Define exact signatures before registry edits, with full types and docs:

- `Manifest.from_dict(...)` / parser for UTF-8 `manifest.json`.
- `validate_package(zip_bytes, max_size_bytes, core_version=...)` returns parsed
  manifest, validated archive information, root-prefix normalization and warnings.
- `extract_package(validated, destination)` writes only to a fresh owned scratch
  directory; it cannot accept an arbitrary preexisting live plugin directory.
- Specific domain exception for invalid package, with human-readable safe reason;
  API maps to 422 `invalid_plugin` (413 `payload_too_large` for upload limit).

The implementation agent may propose a cleaner equivalent before integration,
but downstream consumers must use one deliberate shared validator, not duplicate
manifest/path/security logic.

## Security and failure cases

- Reject compressed >limit, uncompressed >5x, >2000 entries, >100:1 entry ratio.
- Reject absolute/drive/backslash/NUL paths, any `..` segment, symlinks and special
  metadata; resolve every normalized destination inside scratch before extraction.
- Reject duplicate normalized file destinations, file/directory conflicts,
  encrypted entries, invalid CRC/truncated archives and unsupported compression
  with precise failures. Handle raw NUL names before ZipInfo truncation where
  necessary; do not assume a ZIP fixture proves raw-name safety automatically.
- Permit one enclosing top-level directory only when root manifest is absent;
  distinguish harmless directory records from inconsistent enclosing layouts.
- Parse Python imports with AST, warn only outside stdlib/requests dependencies/
  SDK/package-local modules; distinguish relative imports and nested package dirs.
- AST syntax failures are load diagnostics, not a reason to violate warnings-only
  import lint or execute code during archive validation.
- API upload reads with bounds, not an unlimited request body. Verify multipart
  field and mode; never echo arbitrary manifest exception internals.
- Removing slug `foo` must not delete `foobar` or unrelated owner sessions/OAuth
  states; use exact namespace patterns and SCAN, never KEYS or broad wildcard.

## Gate evidence

SDK unit tests; every manifest rule; malicious ZIP tests and no-write assertions;
lint warning classification; root and nested import HTTP201; duplicate409;
missing/bad field and invalid mode; remove exact-target isolation; API no-store
and authentication regression; cumulative tests, Ruff, strict public type check,
wheel package inclusion, independent critic score >8.5 and manager verification.
