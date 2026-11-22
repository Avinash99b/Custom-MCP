"""Source-level assertions for the static dashboard.

The browser assets must stay CSP-compatible and must never parse server data as
HTML or persist credentials client-side. These checks are intentionally textual
so they run without a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "custom_mcp" / "static"
)
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"

#: Substrings that would either violate the CSP or risk injecting server data.
BANNED_IN_SOURCE = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "localStorage",
    "sessionStorage",
    "document.cookie",
)

#: Element ids the dashboard script depends on (and the integration test checks).
REQUIRED_IDS = (
    "app-header",
    "session-status",
    "logout",
    "refresh-plugins",
    "error-banner",
    "toast",
    "count-total",
    "count-connected",
    "count-not-connected",
    "count-problem",
    "count-attention",
    "attention",
    "import-file",
    "import-button",
    "plugin-grid",
    "empty-state",
    "detail-dialog",
    "detail-title",
    "detail-slug",
    "detail-close",
    "detail-manifest",
    "detail-status",
    "detail-load-state",
    "detail-auth-mode",
    "detail-endpoint",
    "detail-copy-endpoint",
    "detail-callback",
    "detail-copy-callback",
    "detail-tools",
    "detail-secrets",
    "detail-warnings",
    "detail-logs",
    "detail-refresh-logs",
    "detail-connect",
    "detail-disconnect",
    "detail-reload",
    "detail-remove",
    "settings-section",
    "backup-warning",
    "backup-plugins",
    "backup-download",
    "backup-select-all",
    "backup-select-none",
    "restore-file",
    "restore-mode",
    "restore-slugs",
    "restore-button",
    "restore-results",
    "restore-summary",
    "restore-result-list",
    "connect-dialog",
    "connect-form",
    "connect-mode",
    "connect-key-field",
    "connect-key",
    "connect-info",
    "connect-submit",
    "connect-cancel",
)

_HANDLER_ATTR = re.compile(r"\son[a-z]+\s*=")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_app_js_has_no_banned_constructs() -> None:
    source = _read(APP_JS)
    for token in BANNED_IN_SOURCE:
        assert token not in source, f"app.js must not contain {token!r}"


def test_index_html_has_no_banned_constructs() -> None:
    source = _read(INDEX_HTML)
    for token in BANNED_IN_SOURCE:
        assert token not in source, f"index.html must not contain {token!r}"


def test_sources_have_no_absolute_urls() -> None:
    for path in (APP_JS, INDEX_HTML):
        source = _read(path)
        assert "http://" not in source, f"{path.name} must not use absolute URLs"
        assert "https://" not in source, f"{path.name} must not use absolute URLs"


def test_index_html_has_no_inline_script_or_style() -> None:
    source = _read(INDEX_HTML)
    assert "<script>" not in source
    assert "<style" not in source
    assert _HANDLER_ATTR.search(source) is None, "no inline event handler attributes"


def test_index_html_references_static_assets() -> None:
    source = _read(INDEX_HTML)
    assert '<script src="/static/app.js"' in source
    assert 'href="/static/styles.css"' in source


def test_index_html_declares_required_ids() -> None:
    source = _read(INDEX_HTML)
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in source, f"missing element id {element_id!r}"


def test_app_js_is_strict_iife() -> None:
    source = _read(APP_JS)
    assert '"use strict";' in source
    assert "(function () {" in source


def test_app_js_uses_text_content_not_html() -> None:
    source = _read(APP_JS)
    assert "textContent" in source


def test_index_html_settings_has_plaintext_warning() -> None:
    source = _read(INDEX_HTML)
    assert "plaintext" in source.lower(), "backup warning must mention plaintext"
    assert (
        "Backup and restore are not available yet" not in source
    ), "the settings placeholder must be removed"


def test_app_js_drives_backup_and_restore_endpoints() -> None:
    source = _read(APP_JS)
    assert "/api/backup" in source
    assert "/api/restore" in source
