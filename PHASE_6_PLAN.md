# Phase 6 plan — validated backup/restore and settings UI

Goal: export/import a portable backup ZIP that round-trips each plugin's package
bytes, data, secrets (including reserved `core.*` keys), auth mode and version, and
expose it through REST endpoints and the dashboard settings area.

## Acceptance gate (TEST_MATRIX row 6)
Byte-identical package export; data/secrets/core keys/auth mode round trip; TTL
keys excluded; plaintext warning; subset and unknown slug behavior; complete
Phase A rejection writes nothing; encrypted format rejected; skip/replace/selective
restore; exact state wipe on successful replace; per-plugin runtime failure
isolation; `on_restore` failure non-fatal.

## Contract (ARCHITECTURE.md "Backup", lines 141–156)

Backup ZIP (deflated, streamed via `SpooledTemporaryFile`):

```text
backup.json
plugins/{slug}/plugin.zip     # byte-identical to DATA_DIR/packages/{slug}.zip
plugins/{slug}/data.json      # flat key/value object of decoded values
plugins/{slug}/secrets.json   # flat key/value object incl. reserved core.* keys
plugins/{slug}/meta.json      # {"auth_mode": <str|null>, "version": <str>}
```

`backup.json`: `{"format_version":1,"created_at":<UTC ISO>,"core_version":<str>,
"encrypted":false,"plugins":[{"slug","version","has_secrets"}]}`.

Rules:
- Snapshot each plugin under its stable lifecycle lock.
- Exclude any key that has a TTL (transient) from `data.json`/`secrets.json`.
- Never decrypt: an `encrypted:true` backup is rejected with 422.

Restore:
- **Phase A** validates the whole outer ZIP and every listed package/JSON/key/value
  schema without modifying live disk or Redis; any defect → 422 and no changes.
- **Phase B** applies per plugin independently: `skip` existing or transactional
  `replace`; install without auto-connect; wipe the replaced plugin's data/secrets
  for an exact snapshot; restore values with **no** TTL; set status `not_connected`;
  call `on_restore` (non-fatal on failure); then reconnect and persist status.
- A valid archive returns 200 with per-plugin `restored`/`skipped`/`failed`
  results and the resulting connection status; an invalid archive returns 422.

## REST contract

- `GET /api/backup` (optional `?plugins=a,b`) → 200 `application/zip`,
  `Content-Disposition: attachment; filename="custom-mcp-backup-<utc>.zip"`.
  Unknown requested slug → 404 `{"error":{"code":"not_found",...}}`.
- `POST /api/restore` multipart field `file`, query `mode=skip|replace`,
  optional `slugs=a,b` → 200:
  ```json
  {"format_version":1,
   "results":[{"slug":"hello","outcome":"restored","status":"connected","detail":null}],
   "restored":["hello"],"skipped":[],"failed":[]}
  ```
  Invalid/encrypted archive → 422; malformed request → 400/422; unknown filtered
  slug → 404. Restore never auto-connects before restoring that plugin's state.

## Slices (non-overlapping ownership)

### 6a backend backup/restore (agent A)
- `src/custom_mcp/backup.py`: `BackupService` (build + stream) and
  `RestoreService` (Phase A validate, Phase B apply) reusing `validate_package`,
  `StorageManager`, `PluginManager` lifecycle locks and `install`/`connect`.
- `src/custom_mcp/api/backup.py`: `GET /api/backup`, `POST /api/restore` +
  `backup_routes`.
- `src/custom_mcp/main.py`: construct the services, add `backup_routes`, expose
  `app.state.backup`.
- Minimal public accessors on `PluginManager` if needed (package path, lifecycle
  lock, known slugs).
- `tests/unit/test_backup.py`, `tests/integration/test_backup_restore.py`.

### 6b settings UI + docs (agent B)
- `src/custom_mcp/static/{index.html,styles.css,app.js}`: settings section with a
  prominent plaintext-credentials warning, backup download (with optional plugin
  selection), restore file picker + `skip`/`replace` mode + optional slug filter,
  and a results area showing per-plugin outcome and connection status; all
  DOM-only and CSP-compatible like the rest of the dashboard.
- `docs/DASHBOARD_WALKTHROUGH.md` and `README.md`: document backup/restore.
