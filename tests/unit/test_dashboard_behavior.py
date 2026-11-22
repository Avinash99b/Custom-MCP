"""Behavioural regressions for the dashboard's async route handling.

Runs the real, unmodified ``static/app.js`` inside a minimal DOM harness under
Node and reproduces three routing defects:

* a slow detail response for an old slug must not render over the current route;
* an already-open connect dialog must not submit credentials to a plugin the user
  navigated to;
* a completed action's detail refresh must not invalidate the current route's
  pending detail request.

Skipped when Node is unavailable; the source-level checks in
``test_dashboard_source.py`` still run everywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

NODE = shutil.which("node")
STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "custom_mcp" / "static"

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const html = fs.readFileSync(process.argv[3], "utf8");
const scenario = process.argv[4] || "race";

class ClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this._children = [];
    this._text = "";
    this.hidden = false;
    this.value = "";
    this.className = "";
    this.classList = new ClassList();
    this._listeners = {};
    this.open = false;
    this.attributes = {};
    this.files = null;
    this.id = "";
    this.checked = false;
  }
  get firstChild() { return this._children.length ? this._children[0] : null; }
  appendChild(c) { this._children.push(c); return c; }
  removeChild(c) {
    const i = this._children.indexOf(c);
    if (i >= 0) { this._children.splice(i, 1); }
    return c;
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "id") { this.id = String(v); }
    if (k === "value") { this.value = String(v); }
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatch(type) {
    (this._listeners[type] || []).slice().forEach((fn) =>
      fn({ preventDefault() {}, target: this })
    );
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = v; this._children = []; }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch("close"); }
}

class FormDataStub { append() {} }

const tick = () => new Promise((r) => setImmediate(r));

const detail = (slug, status) => ({
  slug,
  load_state: "loaded",
  status: status || "not_connected",
  auth_methods: ["api_key"],
  auth_mode: null,
  tools: [],
  secret_names: [],
  warnings: [],
  logs: [],
  manifest: { name: slug },
});

function urlSlug(url) {
  const match = url.match(/\/api\/plugins\/([^/?]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function makeEnv() {
  const registry = {};
  for (const m of html.matchAll(/id="([^"]+)"/g)) {
    const el = new El("div");
    el.id = m[1];
    registry[m[1]] = el;
  }
  const document = {
    getElementById(id) {
      if (!registry[id]) { registry[id] = new El("div"); }
      return registry[id];
    },
    createElement(tag) { return new El(tag); },
    createTextNode(text) { return { __text: String(text) }; },
  };

  const listeners = {};
  const deleteCalls = [];
  const connectPosts = [];
  const confirmMessages = [];
  const replaced = [];
  const assigned = [];
  const restorePosts = [];
  let currentHash = "";
  const location = {
    get hash() { return currentHash; },
    set hash(v) { currentHash = String(v); },
    href: "https://mcp.example.com/",
    replace(u) { replaced.push(u); },
    assign(u) { assigned.push(u); },
  };
  const history = {
    replaceState(_a, _b, url) {
      if (typeof url === "string" && url.startsWith("#")) { currentHash = url; }
    },
  };

  function makeResponse(status, body) {
    return {
      ok: status >= 200 && status < 300,
      status,
      async text() { return body === undefined ? "" : JSON.stringify(body); },
    };
  }

  const pluginRow = (slug, display, status, auth) => ({
    slug,
    display_name: display,
    status,
    load_state: "loaded",
    version: "1.0.0",
    tool_names: [],
    auth_methods: [auth],
  });

  const detailPending = {};
  const reloadPending = {};
  const deletePending = {};
  const connectPending = {};
  const disconnectPending = {};
  function fetchStub(url, init) {
    const method = ((init && init.method) || "GET").toUpperCase();
    if (url === "/api/session") return Promise.resolve(makeResponse(200, { authenticated: true }));
    if (url === "/api/plugins")
      return Promise.resolve(makeResponse(200, {
        plugins: [
          pluginRow("hello", "Hello", "connected", "none"),
          pluginRow("apikey-demo", "apikey_demo", "not_connected", "api_key"),
        ],
      }));
    if (url.indexOf("/api/restore") === 0 && method === "POST") {
      restorePosts.push(url);
      return Promise.resolve(makeResponse(200, {
        format_version: 1,
        results: [
          { slug: "hello", outcome: "restored", status: "connected", detail: null },
          { slug: "apikey-demo", outcome: "skipped", status: "not_connected", detail: null },
        ],
        restored: ["hello"],
        skipped: ["apikey-demo"],
        failed: [],
      }));
    }
    if (method === "DELETE") {
      deleteCalls.push(url);
      return new Promise((resolve) => { deletePending[url] = resolve; });
    }
    if (/\/connect$/.test(url) && method === "POST") {
      connectPosts.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      return new Promise((resolve) => { connectPending[url] = resolve; });
    }
    if (/\/reload$/.test(url) && method === "POST") {
      return new Promise((resolve) => { reloadPending[url] = resolve; });
    }
    if (/\/disconnect$/.test(url) && method === "POST") {
      return new Promise((resolve) => { disconnectPending[url] = resolve; });
    }
    if (/^\/api\/plugins\/[^/?]+$/.test(url) && method === "GET") {
      return new Promise((resolve) => {
        (detailPending[url] = detailPending[url] || []).push(resolve);
      });
    }
    if (url.indexOf("/logs") !== -1) return Promise.resolve(makeResponse(200, { logs: [] }));
    return Promise.resolve(makeResponse(200, {}));
  }

  const windowObj = {
    location,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    confirm(message) { confirmMessages.push(message); return true; },
    setTimeout() { return 0; },
    clearTimeout() {},
  };

  const sandbox = {
    window: windowObj,
    document,
    navigator: { clipboard: { writeText: async () => {} } },
    history,
    fetch: fetchStub,
    console,
    FormData: FormDataStub,
  };
  vm.createContext(sandbox);

  return {
    sandbox, registry, listeners, deleteCalls, connectPosts, confirmMessages,
    replaced, assigned, restorePosts, detailPending, reloadPending, deletePending,
    connectPending, disconnectPending, makeResponse,
    setHash(v) { currentHash = v; },
    getHash() { return currentHash; },
    fireHash() { (listeners["hashchange"] || []).slice().forEach((fn) => fn({})); },
    pendingDetail(slug) {
      return Object.keys(detailPending).find((u) => u.endsWith(slug));
    },
    detailQueue(slug) {
      const url = Object.keys(detailPending).find((u) => u.endsWith(slug));
      return detailPending[url] || [];
    },
    resolveDetail(slug, resp) {
      const queue = this.detailQueue(slug);
      const resolve = queue.shift();
      resolve(resp);
    },
    pendingReload(slug) {
      return Object.keys(reloadPending).find((u) => u.endsWith(slug + "/reload"));
    },
    pendingDelete(slug) {
      return Object.keys(deletePending).find((u) => u.endsWith(slug));
    },
    pendingConnect(slug) {
      return Object.keys(connectPending).find((u) => u.endsWith(slug + "/connect"));
    },
    pendingDisconnect(slug) {
      return Object.keys(disconnectPending).find((u) => u.endsWith(slug + "/disconnect"));
    },
  };
}

// The later route must win even when the older detail response resolves last,
// and the lifecycle action must target the visible route.
async function scenarioRace() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/hello");
  env.fireHash();
  await tick();
  env.setHash("#/plugins/gate-key");
  env.fireHash();
  await tick();

  env.resolveDetail("gate-key", env.makeResponse(200, detail("gate-key")));
  await tick();
  await tick();
  env.resolveDetail("hello", env.makeResponse(200, detail("hello")));
  await tick();
  await tick();

  const result = {
    detail_slug: env.registry["detail-slug"].textContent,
    dialog_open: env.registry["detail-dialog"].open,
  };
  env.registry["detail-remove"].dispatch("click");
  await tick();
  await tick();
  result.delete_calls = env.deleteCalls.slice();
  result.confirm_messages = env.confirmMessages.slice();
  return result;
}

// A connect dialog opened for one plugin must not deliver its credential to a
// plugin the user navigates to before submitting.
async function scenarioConnectNavigate() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-key-intended-for-alpha";

  env.setHash("#/plugins/beta-key");
  env.fireHash();
  await tick();

  env.registry["connect-form"].dispatch("submit");
  await tick();
  await tick();

  return {
    connect_posts: env.connectPosts.slice(),
    connect_dialog_open: env.registry["connect-dialog"].open,
    connect_error: env.registry["connect-error"].textContent,
  };
}

// A completed old action's refresh must not invalidate the new route's pending
// detail request.
async function scenarioOldRefresh() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-reload"].dispatch("click");
  await tick();

  env.setHash("#/plugins/beta-key");
  env.fireHash();
  await tick();

  env.reloadPending[env.pendingReload("alpha-key")](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();
  env.resolveDetail("beta-key", env.makeResponse(200, detail("beta-key")));
  await tick();
  await tick();

  return {
    detail_slug: env.registry["detail-slug"].textContent,
    detail_status: env.registry["detail-status"].textContent,
  };
}

// Control: submitting without navigating still connects the intended plugin.
async function scenarioConnectNormal() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "legit-key";
  env.registry["connect-form"].dispatch("submit");
  await tick();
  await tick();

  return { connect_posts: env.connectPosts.slice() };
}

// An old connect completion must not clear or close a newer connect dialog.
async function scenarioOldConnectCompletion() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-alpha";
  env.registry["connect-form"].dispatch("submit");
  await tick();

  env.setHash("#/");
  env.fireHash();
  await tick();
  env.setHash("#/plugins/beta-key");
  env.fireHash();
  await tick();
  env.resolveDetail("beta-key", env.makeResponse(200, detail("beta-key")));
  await tick();
  await tick();
  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-beta";

  env.connectPending[env.pendingConnect("alpha-key")](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  return {
    connect_dialog_open: env.registry["connect-dialog"].open,
    connect_key: env.registry["connect-key"].value,
    connect_posts: env.connectPosts.slice(),
  };
}

// An old remove completion must not close the route the user has since opened.
async function scenarioOldRemoveCompletion() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-remove"].dispatch("click");
  await tick();

  env.setHash("#/");
  env.fireHash();
  await tick();
  env.setHash("#/plugins/hello");
  env.fireHash();
  await tick();
  env.resolveDetail("hello", env.makeResponse(200, detail("hello")));
  await tick();
  await tick();

  env.deletePending[env.pendingDelete("alpha-key")](env.makeResponse(204));
  await tick();
  await tick();

  return {
    hash: env.getHash(),
    detail_slug: env.registry["detail-slug"].textContent,
    dialog_open: env.registry["detail-dialog"].open,
    delete_calls: env.deleteCalls.slice(),
  };
}

// Cancelling and reopening Connect for the *same* plugin must not let an older
// completion clear/close the newer dialog.
async function scenarioSamePluginReopen() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-alpha";
  env.registry["connect-form"].dispatch("submit");
  await tick();

  env.registry["connect-cancel"].dispatch("click");
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-alpha-retry";

  env.connectPending[env.pendingConnect("alpha-key")](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  const afterOld = {
    dialog_open: env.registry["connect-dialog"].open,
    key: env.registry["connect-key"].value,
  };

  env.registry["connect-form"].dispatch("submit");
  await tick();
  await tick();

  return {
    after_old: afterOld,
    connect_posts: env.connectPosts.slice(),
  };
}

// Returning to a route while an older action's refresh is in flight must still
// open that route's detail dialog.
async function scenarioReturnDuringAction() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "api_key";
  env.registry["connect-key"].value = "dummy-alpha";
  env.registry["connect-form"].dispatch("submit");
  await tick();

  env.setHash("#/");
  env.fireHash();
  await tick();
  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();

  // Complete the old connect and let its follow-up refresh start, then complete
  // the returned navigation's own detail response.
  env.connectPending[env.pendingConnect("alpha-key")](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();
  const queue = env.detailQueue("alpha-key");
  queue[1](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();
  queue[0](env.makeResponse(200, detail("alpha-key")));
  await tick();
  await tick();

  return {
    hash: env.getHash(),
    detail_open: env.registry["detail-dialog"].open,
    detail_slug: env.registry["detail-slug"].textContent,
  };
}

// A late navigation response must open its dialog without overwriting fresher
// data rendered by a newer post-action refresh.
async function scenarioOpenerAfterRefresh() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  env.resolveDetail("alpha-key", env.makeResponse(200, detail("alpha-key", "connected")));
  await tick();
  await tick();

  env.registry["detail-disconnect"].dispatch("click");
  await tick();

  env.setHash("#/");
  env.fireHash();
  await tick();
  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();

  env.disconnectPending[env.pendingDisconnect("alpha-key")](
    env.makeResponse(200, detail("alpha-key", "not_connected"))
  );
  await tick();
  await tick();

  const queue = env.detailQueue("alpha-key");
  queue[1](env.makeResponse(200, detail("alpha-key", "not_connected")));
  await tick();
  await tick();
  queue[0](env.makeResponse(200, detail("alpha-key", "connected")));
  await tick();
  await tick();

  return {
    detail_status: env.registry["detail-status"].textContent,
    detail_open: env.registry["detail-dialog"].open,
  };
}

// The backup controls navigate to /api/backup (optionally with a subset) and
// the restore control posts multipart to /api/restore and renders the results.
function checkboxBySlug(env, slug) {
  const rows = env.registry["backup-plugins"]._children;
  for (const row of rows) {
    const cb = row._children && row._children[0];
    if (cb && cb.value === slug) { return cb; }
  }
  return null;
}

async function scenarioBackupRestore() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  const assignedFullBefore = env.assigned.slice();

  // Subset: tick one plugin, then download.
  const cb = checkboxBySlug(env, "hello");
  cb.checked = true;
  cb.dispatch("change");
  await tick();
  env.registry["backup-download"].dispatch("click");
  await tick();
  const subsetUrl = env.assigned[env.assigned.length - 1];

  // Clear the selection: the next download backs up everything.
  env.registry["backup-select-none"].dispatch("click");
  await tick();
  env.registry["backup-download"].dispatch("click");
  await tick();
  const fullUrl = env.assigned[env.assigned.length - 1];

  // Restore with the replace mode and a slug filter.
  env.registry["restore-file"].files = [{ name: "backup.zip" }];
  env.registry["restore-mode"].value = "replace";
  env.registry["restore-slugs"].value = "hello";
  env.registry["restore-button"].dispatch("click");
  await tick();
  await tick();

  return {
    assigned_full_before: assignedFullBefore,
    subset_url: subsetUrl,
    full_url: fullUrl,
    restore_posts: env.restorePosts.slice(),
    restore_summary: env.registry["restore-summary"].textContent,
    result_count: env.registry["restore-result-list"]._children.length,
  };
}

// An OAuth connect that returns a sign-in URL must navigate the browser there
// and clear the entered client credentials.
async function scenarioOauthRedirect() {
  const env = makeEnv();
  vm.runInContext(source, env.sandbox, { filename: "app.js" });
  await tick();
  await tick();

  env.setHash("#/plugins/alpha-key");
  env.fireHash();
  await tick();
  const oauthDetail = detail("alpha-key");
  oauthDetail.auth_methods = ["oauth"];
  env.resolveDetail("alpha-key", env.makeResponse(200, oauthDetail));
  await tick();
  await tick();

  env.registry["detail-connect"].dispatch("click");
  env.registry["connect-mode"].value = "oauth";
  env.registry["connect-client-id"].value = "client-abc";
  env.registry["connect-client-secret"].value = "shh-secret";
  env.registry["connect-form"].dispatch("submit");
  await tick();

  const pending = Object.assign(detail("alpha-key"), {
    status: "oauth_required",
    redirect_url: "https://provider.example/authorize?state=xyz&code_challenge=abc",
  });
  env.connectPending[env.pendingConnect("alpha-key")](env.makeResponse(200, pending));
  await tick();
  await tick();

  return {
    connect_posts: env.connectPosts.slice(),
    assigned: env.assigned.slice(),
    dialog_open: env.registry["connect-dialog"].open,
    client_id: env.registry["connect-client-id"].value,
    client_secret: env.registry["connect-client-secret"].value,
  };
}

const scenarios = {
  race: scenarioRace,
  backup_restore: scenarioBackupRestore,
  connect_navigate: scenarioConnectNavigate,
  connect_normal: scenarioConnectNormal,
  old_refresh: scenarioOldRefresh,
  old_connect_completion: scenarioOldConnectCompletion,
  old_remove_completion: scenarioOldRemoveCompletion,
  same_plugin_reopen: scenarioSamePluginReopen,
  return_during_action: scenarioReturnDuringAction,
  opener_after_refresh: scenarioOpenerAfterRefresh,
  oauth_redirect: scenarioOauthRedirect,
};

(scenarios[scenario] || scenarioRace)()
  .then((result) => { process.stdout.write(JSON.stringify(result)); })
  .catch((err) => {
    process.stderr.write(String((err && err.stack) || err));
    process.exit(1);
  });
"""


def _run(scenario: str, tmp_path: Path) -> dict[str, Any]:
    node = NODE
    assert node is not None
    harness = tmp_path / f"harness-{scenario}.cjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [
            node,
            str(harness),
            str(STATIC_DIR / "app.js"),
            str(STATIC_DIR / "index.html"),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for the DOM harness")


def test_stale_detail_response_cannot_override_route(tmp_path: Path) -> None:
    result = _run("race", tmp_path)
    assert result["detail_slug"] == "gate-key", result
    assert result["dialog_open"] is True, result
    assert result["delete_calls"] == ["/api/plugins/gate-key"], result
    assert "gate-key" in result["confirm_messages"][-1], result


def test_connect_dialog_cannot_submit_to_another_plugin(tmp_path: Path) -> None:
    result = _run("connect_navigate", tmp_path)
    # The dialog was closed on navigation, so nothing was sent to beta-key.
    assert result["connect_dialog_open"] is False, result
    assert result["connect_posts"] == [], result
    assert result["connect_error"], result


def test_connect_dialog_submits_to_its_own_plugin(tmp_path: Path) -> None:
    result = _run("connect_normal", tmp_path)
    assert len(result["connect_posts"]) == 1, result
    assert result["connect_posts"][0]["url"].endswith("/api/plugins/alpha-key/connect")
    assert result["connect_posts"][0]["body"]["credentials"]["api_key"] == "legit-key"


def test_old_action_refresh_does_not_invalidate_new_route(tmp_path: Path) -> None:
    result = _run("old_refresh", tmp_path)
    # Beta's detail must render even though alpha's reload refresh completed
    # first; a still-"Loading" status means its response was wrongly discarded.
    assert result["detail_slug"] == "beta-key", result
    assert result["detail_status"] == "Not connected", result


def test_old_connect_completion_does_not_cancel_new_dialog(tmp_path: Path) -> None:
    result = _run("old_connect_completion", tmp_path)
    # Alpha's late completion must not close beta's dialog or clear its key.
    assert result["connect_dialog_open"] is True, result
    assert result["connect_key"] == "dummy-beta", result
    assert len(result["connect_posts"]) == 1, result
    assert result["connect_posts"][0]["url"].endswith("/api/plugins/alpha-key/connect")


def test_old_remove_completion_does_not_close_new_route(tmp_path: Path) -> None:
    result = _run("old_remove_completion", tmp_path)
    assert result["delete_calls"] == ["/api/plugins/alpha-key"], result
    assert result["hash"] == "#/plugins/hello", result
    assert result["detail_slug"] == "hello", result
    assert result["dialog_open"] is True, result


def test_reopened_connect_dialog_survives_old_completion(tmp_path: Path) -> None:
    result = _run("same_plugin_reopen", tmp_path)
    # Alpha's first completion must not close or clear the reopened dialog.
    assert result["after_old"]["dialog_open"] is True, result
    assert result["after_old"]["key"] == "dummy-alpha-retry", result
    # The reopened dialog then submits its own key.
    assert len(result["connect_posts"]) == 2, result
    assert (
        result["connect_posts"][1]["body"]["credentials"]["api_key"]
        == "dummy-alpha-retry"
    )

def test_return_during_old_action_opens_detail_dialog(tmp_path: Path) -> None:
    result = _run("return_during_action", tmp_path)
    assert result["hash"] == "#/plugins/alpha-key", result
    assert result["detail_slug"] == "alpha-key", result
    assert result["detail_open"] is True, result

def test_late_opener_does_not_overwrite_fresher_detail(tmp_path: Path) -> None:
    result = _run("opener_after_refresh", tmp_path)
    # The disconnect refresh rendered the fresher state; the late opener must
    # still open the dialog but not overwrite that state with stale data.
    assert result["detail_open"] is True, result
    assert result["detail_status"] == "Not connected", result


def test_oauth_connect_navigates_to_sign_in_url(tmp_path: Path) -> None:
    result = _run("oauth_redirect", tmp_path)
    assert len(result["connect_posts"]) == 1, result
    post = result["connect_posts"][0]
    assert post["url"].endswith("/api/plugins/alpha-key/connect")
    assert post["body"]["mode"] == "oauth"
    assert post["body"]["credentials"]["client_id"] == "client-abc"
    assert post["body"]["credentials"]["client_secret"] == "shh-secret"
    assert result["assigned"] == [
        "https://provider.example/authorize?state=xyz&code_challenge=abc"
    ], result
    assert result["dialog_open"] is False
    assert result["client_id"] == ""
    assert result["client_secret"] == ""


def test_backup_and_restore_controls(tmp_path: Path) -> None:
    result = _run("backup_restore", tmp_path)
    # Nothing navigates until the user acts, and with no plugins ticked the whole
    # install is exported; a ticked plugin adds a subset query.
    assert result["assigned_full_before"] == [], result
    assert result["subset_url"] == "/api/backup?plugins=hello", result
    assert result["full_url"] == "/api/backup", result
    # Restore posts multipart with the chosen mode and slug filter, then renders
    # one row per result plus a summary count.
    assert result["restore_posts"] == ["/api/restore?mode=replace&slugs=hello"], result
    assert result["restore_summary"] == "Restored 1, skipped 1, failed 0.", result
    assert result["result_count"] == 2, result
