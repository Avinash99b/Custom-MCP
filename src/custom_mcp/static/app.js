"use strict";

// Custom MCP dashboard.
//
// Renders server data exclusively with DOM APIs and textContent so nothing the
// server returns is ever parsed as HTML. The page runs under the enforced CSP
// (`default-src 'self'; frame-ancestors 'none'`), so it uses no inline scripts,
// no inline styles, no inline event handlers, no external origins and no
// `eval`. The session secret and any plugin credentials are never written to
// browser storage APIs or persisted cookies.
(function () {
  var STATUS_LABELS = {
    connected: "Connected",
    not_connected: "Not connected",
    oauth_required: "OAuth required",
    auth_expired: "Auth expired",
    error: "Error",
  };
  var LOAD_LABELS = {
    loading: "Loading",
    loaded: "Loaded",
    load_failed: "Load failed",
    unloading: "Unloading",
  };

  var lastDetail = null;
  var currentSlug = null;
  var connectSlug = null;
  var connectDialogToken = null;
  var toastTimer = null;
  var renderTokens = {};
  var lastPlugins = [];
  var backupSelection = {};

  // Every asynchronous render gets a token; a response only renders when it is
  // still the newest request of its kind *and* (for plugin views) the route still
  // names the same slug. This prevents a slow, stale detail response from
  // overwriting the view or the lifecycle-action target after navigation.
  function nextToken(name) {
    var next = (renderTokens[name] || 0) + 1;
    renderTokens[name] = next;
    return next;
  }

  function tokenIsCurrent(name, token) {
    return renderTokens[name] === token;
  }

  function routeSlug() {
    var match = currentPath().match(/^\/plugins\/([^/]+)$/);
    return match ? safeDecode(match[1]) : null;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  // --- DOM helpers (no HTML parsing, ever) --------------------------------

  function append(node, children) {
    if (children === undefined || children === null || children === false) {
      return;
    }
    var list = Array.isArray(children) ? children : [children];
    for (var i = 0; i < list.length; i += 1) {
      var child = list[i];
      if (child === undefined || child === null || child === false) {
        continue;
      }
      if (typeof child === "string" || typeof child === "number") {
        node.appendChild(document.createTextNode(String(child)));
      } else {
        node.appendChild(child);
      }
    }
  }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      var keys = Object.keys(props);
      for (var i = 0; i < keys.length; i += 1) {
        var key = keys[i];
        var value = props[key];
        if (value === undefined || value === null) {
          continue;
        }
        if (key === "text") {
          node.textContent = String(value);
        } else if (key === "class") {
          node.className = String(value);
        } else if (key === "value") {
          node.value = value;
        } else if (key === "on") {
          var events = Object.keys(value);
          for (var j = 0; j < events.length; j += 1) {
            node.addEventListener(events[j], value[events[j]]);
          }
        } else {
          node.setAttribute(key, String(value));
        }
      }
    }
    append(node, children);
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function setHidden(id, hidden) {
    var node = byId(id);
    if (node) {
      node.hidden = hidden;
    }
  }

  // --- notifications ------------------------------------------------------

  function showToast(message, isError) {
    var toast = byId("toast");
    toast.textContent = message;
    toast.hidden = false;
    if (isError) {
      toast.classList.add("error");
    } else {
      toast.classList.remove("error");
    }
    if (toastTimer !== null) {
      window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(function () {
      toast.hidden = true;
    }, 5000);
  }

  function showError(message) {
    var banner = byId("error-banner");
    banner.textContent = message;
    banner.hidden = false;
  }

  function clearError() {
    var banner = byId("error-banner");
    banner.textContent = "";
    banner.hidden = true;
  }

  function reportActionError(err) {
    var message = err && err.message ? err.message : "Something went wrong.";
    showError(message);
    showToast(message, true);
    var detailError = byId("detail-error");
    if (detailError) {
      detailError.textContent = message;
      detailError.hidden = false;
    }
  }

  // --- API ----------------------------------------------------------------

  function ApiError(message, status, code) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.code = code || null;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  async function apiFetch(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    if (opts.headers) {
      var extra = Object.keys(opts.headers);
      for (var i = 0; i < extra.length; i += 1) {
        headers[extra[i]] = opts.headers[extra[i]];
      }
    }
    var init = { method: opts.method || "GET", headers: headers };
    if (opts.body !== undefined) {
      init.body = opts.body;
    }

    var response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new ApiError("Network error. Please try again.", 0, null);
    }

    var data = null;
    var text = "";
    try {
      text = await response.text();
    } catch (err) {
      text = "";
    }
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = null;
      }
    }

    if (!response.ok) {
      var message = "Request failed (" + response.status + ").";
      var code = null;
      if (data && data.error) {
        if (data.error.message) {
          message = data.error.message;
        }
        code = data.error.code || null;
      }
      throw new ApiError(message, response.status, code);
    }
    return data;
  }

  // --- session ------------------------------------------------------------

  function redirectToLogin() {
    window.location.replace("/login");
  }

  async function guardSession() {
    try {
      var data = await apiFetch("/api/session");
      if (!data || data.authenticated !== true) {
        redirectToLogin();
        return false;
      }
      byId("session-status").textContent = "Signed in.";
      return true;
    } catch (err) {
      redirectToLogin();
      return false;
    }
  }

  async function doLogout() {
    try {
      await apiFetch("/logout", { method: "POST" });
    } catch (err) {
      // Leave the session guard to redirect regardless.
    } finally {
      redirectToLogin();
    }
  }

  // --- summaries, counts and grid -----------------------------------------

  function statusLabel(status) {
    return STATUS_LABELS[status] || String(status || "Unknown");
  }

  function loadLabel(state) {
    return LOAD_LABELS[state] || String(state || "Unknown");
  }

  function safeToken(value) {
    return String(value || "").replace(/[^a-z0-9_-]/gi, "-");
  }

  function needsAttention(summary) {
    if (!summary) {
      return false;
    }
    if (summary.load_state === "load_failed") {
      return true;
    }
    if (summary.status === "error" || summary.status === "auth_expired") {
      return true;
    }
    if (summary.load_error) {
      return true;
    }
    if (summary.last_error) {
      return true;
    }
    if (Array.isArray(summary.warnings) && summary.warnings.length > 0) {
      return true;
    }
    return false;
  }

  function renderCard(summary) {
    var slug = summary.slug || "";
    var display = summary.display_name || summary.name || slug;
    var toolNames = Array.isArray(summary.tool_names) ? summary.tool_names : [];
    var authMode = summary.auth_mode || "none selected";
    return el("article", { class: "plugin-card" }, [
      el("div", { class: "card-head" }, [
        el("h2", { text: display }),
        el("span", {
          class: "badge status-" + safeToken(summary.status),
          text: statusLabel(summary.status),
        }),
      ]),
      el("p", { class: "muted", text: slug }),
      el("p", { text: "Version " + (summary.version || "unknown") }),
      el("p", { text: summary.description || "No description." }),
      el("div", { class: "card-meta" }, [
        el("span", {
          class: "badge load-" + safeToken(summary.load_state),
          text: loadLabel(summary.load_state),
        }),
        el("span", { text: "Auth: " + authMode }),
        el("span", { text: "Tools: " + String(toolNames.length) }),
      ]),
      el("div", { class: "card-actions" }, [
        el("button", {
          type: "button",
          text: "Open",
          on: {
            click: function () {
              setHash("#/plugins/" + encodeURIComponent(slug));
            },
          },
        }),
      ]),
    ]);
  }

  function renderSummaries(plugins) {
    var list = Array.isArray(plugins) ? plugins : [];
    var connected = 0;
    var notConnected = 0;
    var problem = 0;
    var attention = 0;
    var attentionSlugs = [];
    var i;

    for (i = 0; i < list.length; i += 1) {
      var summary = list[i] || {};
      if (summary.status === "connected") {
        connected += 1;
      }
      if (summary.status === "not_connected") {
        notConnected += 1;
      }
      if (summary.status === "error" || summary.status === "auth_expired") {
        problem += 1;
      }
      if (needsAttention(summary)) {
        attention += 1;
        attentionSlugs.push(summary.slug || "unknown");
      }
    }

    byId("count-total").textContent = String(list.length);
    byId("count-connected").textContent = String(connected);
    byId("count-not-connected").textContent = String(notConnected);
    byId("count-problem").textContent = String(problem);
    byId("count-attention").textContent = String(attention);

    var attentionEl = byId("attention");
    if (attention > 0) {
      attentionEl.textContent =
        attention + " plugin(s) need attention: " + attentionSlugs.join(", ");
      attentionEl.hidden = false;
    } else {
      attentionEl.textContent = "";
      attentionEl.hidden = true;
    }

    var grid = byId("plugin-grid");
    clear(grid);
    for (i = 0; i < list.length; i += 1) {
      grid.appendChild(renderCard(list[i] || {}));
    }
    byId("empty-state").hidden = list.length !== 0;

    lastPlugins = list;
    renderBackupPlugins(list);
  }

  async function loadSummaries() {
    var token = nextToken("summaries");
    try {
      var data = await apiFetch("/api/plugins");
      if (!tokenIsCurrent("summaries", token)) {
        return;
      }
      clearError();
      renderSummaries(data && data.plugins);
    } catch (err) {
      if (!tokenIsCurrent("summaries", token)) {
        return;
      }
      reportActionError(err);
    }
  }

  // --- detail dialog ------------------------------------------------------

  function renderDetailFacts(detail) {
    byId("detail-status").textContent = statusLabel(detail.status);
    byId("detail-load-state").textContent = loadLabel(detail.load_state);
    byId("detail-auth-mode").textContent = detail.auth_mode || "None selected";
    byId("detail-load-error").textContent = detail.load_error || "None";
    byId("detail-last-error").textContent = detail.last_error || "None";
    byId("detail-installed").textContent = detail.installed_at || "Unknown";
    byId("detail-last-ping").textContent = detail.last_ping_at || "Never";
  }

  function renderManifest(manifest) {
    var container = byId("detail-manifest");
    clear(container);
    if (!manifest || typeof manifest !== "object") {
      append(container, el("dd", { class: "muted", text: "Manifest unavailable." }));
      return;
    }
    var fields = [
      ["Name", manifest.name],
      ["Display name", manifest.display_name],
      ["Version", manifest.version],
      ["Description", manifest.description],
      ["Entry point", manifest.entry],
      [
        "Auth modes",
        Array.isArray(manifest.auth_modes)
          ? manifest.auth_modes.join(", ")
          : manifest.auth_modes,
      ],
      ["Min core version", manifest.min_core_version],
    ];
    for (var i = 0; i < fields.length; i += 1) {
      var value = fields[i][1];
      if (value === undefined || value === null || value === "") {
        continue;
      }
      append(container, el("dt", { text: fields[i][0] }));
      append(container, el("dd", { text: String(value) }));
    }
  }

  function renderUrl(id, value) {
    byId(id).textContent = value || "Unavailable";
  }

  function renderTools(tools) {
    var container = byId("detail-tools");
    clear(container);
    var list = Array.isArray(tools) ? tools : [];
    if (list.length === 0) {
      append(container, el("p", { class: "muted", text: "No tools reported." }));
      return;
    }
    for (var i = 0; i < list.length; i += 1) {
      var tool = list[i] || {};
      var schema = tool.input_schema === undefined ? {} : tool.input_schema;
      var pretty = "{}";
      try {
        pretty = JSON.stringify(schema, null, 2);
      } catch (err) {
        pretty = "{}";
      }
      append(
        container,
        el("div", { class: "tool" }, [
          el("h4", { text: tool.name || "unnamed" }),
          el("p", { text: tool.description || "No description." }),
          el("pre", { text: pretty }),
        ])
      );
    }
  }

  function renderStringList(id, values, emptyText) {
    var container = byId(id);
    clear(container);
    var list = Array.isArray(values) ? values : [];
    if (list.length === 0) {
      append(container, el("li", { class: "muted", text: emptyText }));
      return;
    }
    for (var i = 0; i < list.length; i += 1) {
      append(container, el("li", { text: String(list[i]) }));
    }
  }

  function renderLogs(logs) {
    var container = byId("detail-logs");
    clear(container);
    var list = Array.isArray(logs) ? logs : [];
    if (list.length === 0) {
      append(container, el("p", { class: "muted", text: "No logs." }));
      return;
    }
    for (var i = 0; i < list.length; i += 1) {
      var entry = list[i] || {};
      append(
        container,
        el("div", { class: "log-row" }, [
          el("span", { class: "badge", text: String(entry.level || "info") }),
          el("span", { class: "log-time", text: String(entry.timestamp || "") }),
          el("span", { class: "log-message", text: String(entry.message || "") }),
        ])
      );
    }
  }

  function renderDetail(detail) {
    lastDetail = detail;
    currentSlug = detail.slug || currentSlug;
    var title = detail.slug || "Plugin";
    if (detail.manifest && detail.manifest.display_name) {
      title = detail.manifest.display_name;
    } else if (detail.manifest && detail.manifest.name) {
      title = detail.manifest.name;
    }
    byId("detail-title").textContent = title;
    byId("detail-slug").textContent = detail.slug || "";
    renderDetailFacts(detail);
    renderManifest(detail.manifest);
    renderUrl("detail-endpoint", detail.endpoint_url || detail.endpoint);
    renderUrl("detail-callback", detail.callback_url);
    renderTools(detail.tools);
    renderStringList(
      "detail-secrets",
      detail.secret_names,
      "No stored credentials."
    );
    renderStringList("detail-warnings", detail.warnings, "No warnings.");
    renderLogs(detail.logs);
  }

  function renderDetailLoading(slug) {
    // Bind the visible dialog to the route's slug immediately so a stale,
    // still-visible detail can never be acted on for a different plugin.
    lastDetail = null;
    byId("detail-title").textContent = slug;
    byId("detail-slug").textContent = slug;
    byId("detail-status").textContent = "Loading";
    byId("detail-load-state").textContent = "Loading";
    byId("detail-auth-mode").textContent = "Loading";
    byId("detail-load-error").textContent = "None";
    byId("detail-last-error").textContent = "None";
    byId("detail-installed").textContent = "Loading";
    byId("detail-last-ping").textContent = "Loading";
    var manifest = byId("detail-manifest");
    clear(manifest);
    append(manifest, el("dd", { class: "muted", text: "Loading manifest…" }));
    renderUrl("detail-endpoint", "");
    renderUrl("detail-callback", "");
    renderTools([]);
    renderStringList("detail-secrets", [], "Loading");
    renderStringList("detail-warnings", [], "Loading");
    renderLogs([]);
    var detailError = byId("detail-error");
    detailError.textContent = "";
    detailError.hidden = true;
  }

  function showDialog(id) {
    var dialog = byId(id);
    if (dialog && !dialog.open) {
      dialog.showModal();
    }
  }

  function closeDialog(id) {
    var dialog = byId(id);
    if (dialog && dialog.open) {
      dialog.close();
    }
  }

  async function refreshDetail(slug) {
    // Only refresh if this slug is still the route; an old action's completion
    // must not invalidate the current route's pending detail request.
    if (routeSlug() !== slug) {
      return;
    }
    var renderToken = nextToken("detail");
    try {
      var detail = await apiFetch("/api/plugins/" + encodeURIComponent(slug));
      if (!tokenIsCurrent("detail", renderToken) || routeSlug() !== slug) {
        return;
      }
      renderDetail(detail);
    } catch (err) {
      if (!tokenIsCurrent("detail", renderToken) || routeSlug() !== slug) {
        return;
      }
      reportActionError(err);
    }
  }

  async function openDetail(slug, oauthResult) {
    // Two independent authorities: the newest detail *fetch* (opener or
    // refresher) may render data, while only the newest navigation for the route
    // may open the dialog. So a late refresh can render fresher data without
    // suppressing the dialog, and a late opener can open the dialog without
    // overwriting fresher data.
    var renderToken = nextToken("detail");
    var openToken = nextToken("detailOpen");
    clearError();
    var detailError = byId("detail-error");
    detailError.textContent = "";
    detailError.hidden = true;
    try {
      var detail = await apiFetch("/api/plugins/" + encodeURIComponent(slug));
      if (routeSlug() !== slug) {
        return;
      }
      if (tokenIsCurrent("detail", renderToken)) {
        renderDetail(detail);
      }
      if (!tokenIsCurrent("detailOpen", openToken)) {
        return;
      }
      showDialog("detail-dialog");
      if (oauthResult) {
        var message =
          oauthResult === "success"
            ? "OAuth connection completed for " + slug + "."
            : "OAuth connection failed for " + slug + ".";
        showToast(message, oauthResult !== "success");
      }
    } catch (err) {
      if (!tokenIsCurrent("detailOpen", openToken) || routeSlug() !== slug) {
        return;
      }
      reportActionError(err);
      setHash("#/");
    }
  }

  async function refreshLogs() {
    if (!currentSlug) {
      return;
    }
    var slug = currentSlug;
    var token = nextToken("logs");
    try {
      var data = await apiFetch(
        "/api/plugins/" + encodeURIComponent(slug) + "/logs?limit=50"
      );
      if (!tokenIsCurrent("logs", token) || currentSlug !== slug) {
        return;
      }
      renderLogs(data && data.logs);
      showToast("Logs refreshed.");
    } catch (err) {
      if (!tokenIsCurrent("logs", token) || currentSlug !== slug) {
        return;
      }
      reportActionError(err);
    }
  }

  async function doDisconnect() {
    if (!currentSlug) {
      return;
    }
    var slug = currentSlug;
    try {
      await apiFetch("/api/plugins/" + encodeURIComponent(slug) + "/disconnect", {
        method: "POST",
      });
      await refreshDetail(slug);
      await loadSummaries();
      showToast("Disconnected " + slug + ".");
    } catch (err) {
      reportActionError(err);
    }
  }

  async function doReload() {
    if (!currentSlug) {
      return;
    }
    var slug = currentSlug;
    try {
      await apiFetch("/api/plugins/" + encodeURIComponent(slug) + "/reload", {
        method: "POST",
      });
      await refreshDetail(slug);
      await loadSummaries();
      showToast("Reloaded " + slug + ".");
    } catch (err) {
      reportActionError(err);
    }
  }

  async function doRemove() {
    if (!currentSlug) {
      return;
    }
    var slug = currentSlug;
    var confirmed = window.confirm(
      "Remove " + slug + "? This deletes the plugin code, its data and any stored credentials."
    );
    if (!confirmed) {
      return;
    }
    try {
      await apiFetch("/api/plugins/" + encodeURIComponent(slug), {
        method: "DELETE",
      });
      // Only close the detail dialog if it is still showing the removed plugin;
      // a stale completion must not close a route the user has since opened.
      if (currentSlug === slug) {
        closeDialog("detail-dialog");
      }
      await loadSummaries();
      showToast("Removed " + slug + ".");
    } catch (err) {
      reportActionError(err);
    }
  }

  async function copyText(value) {
    var text = value || "";
    if (!text || text === "Unavailable") {
      showToast("Nothing to copy.", true);
      return;
    }
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      showToast("Clipboard is not available.", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied to clipboard.");
    } catch (err) {
      showToast("Could not copy to clipboard.", true);
    }
  }

  // --- connect dialog -----------------------------------------------------

  function updateConnectFields() {
    var mode = byId("connect-mode").value;
    byId("connect-key-field").hidden = mode !== "api_key";
    byId("connect-oauth-fields").hidden = mode !== "oauth";
  }

  function showConnectError(message) {
    var node = byId("connect-error");
    node.textContent = message;
    node.hidden = false;
  }

  function openConnectDialog() {
    if (!currentSlug || !lastDetail || lastDetail.slug !== currentSlug) {
      return;
    }
    var modes = Array.isArray(lastDetail.auth_methods)
      ? lastDetail.auth_methods
      : [];
    var select = byId("connect-mode");
    clear(select);
    if (modes.length === 0) {
      append(select, el("option", { value: "", text: "No authentication methods" }));
    } else {
      for (var i = 0; i < modes.length; i += 1) {
        append(select, el("option", { value: modes[i], text: modes[i] }));
      }
      var preferred =
        lastDetail.auth_mode && modes.indexOf(lastDetail.auth_mode) !== -1
          ? lastDetail.auth_mode
          : modes[0];
      select.value = preferred;
    }
    byId("connect-key").value = "";
    byId("connect-client-id").value = "";
    byId("connect-client-secret").value = "";
    byId("connect-error").textContent = "";
    byId("connect-error").hidden = true;
    updateConnectFields();
    // Bind the dialog to the plugin it was opened for *and* to this specific
    // dialog instance, so it can never submit credentials to another plugin the
    // user navigates to, and a stale completion cannot touch a newer dialog
    // (even one reopened for the same plugin).
    connectSlug = currentSlug;
    connectDialogToken = nextToken("connectDialog");
    showDialog("connect-dialog");
  }

  async function submitConnect(event) {
    event.preventDefault();
    var slug = connectSlug;
    var dialogToken = connectDialogToken;
    if (
      !slug ||
      dialogToken === null ||
      routeSlug() !== slug ||
      !tokenIsCurrent("connectDialog", dialogToken)
    ) {
      showConnectError("This connect dialog is no longer for the current plugin.");
      return;
    }
    var mode = byId("connect-mode").value;
    if (!mode) {
      showConnectError("No authentication method is available.");
      return;
    }
    var credentials = {};
    if (mode === "api_key") {
      var key = byId("connect-key").value;
      if (!key.trim()) {
        showConnectError("Enter an API key.");
        return;
      }
      credentials.api_key = key;
    } else if (mode === "oauth") {
      // The client id is optional: an empty value reuses the stored one.
      var clientId = byId("connect-client-id").value;
      var clientSecret = byId("connect-client-secret").value;
      if (clientId.trim()) {
        credentials.client_id = clientId;
      }
      if (clientSecret) {
        credentials.client_secret = clientSecret;
      }
    }
    try {
      var result = await apiFetch("/api/plugins/" + encodeURIComponent(slug) + "/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode, credentials: credentials }),
      });
      var redirectUrl =
        result && typeof result.redirect_url === "string" ? result.redirect_url : "";
      // Only follow an absolute http(s) URL the server produced.
      if (!/^https?:\/\//i.test(redirectUrl)) {
        redirectUrl = "";
      }
      // Only touch the dialog if it is still the one we submitted for; a stale
      // completion must not clear or close a newer dialog.
      if (connectDialogToken === dialogToken && connectSlug === slug) {
        byId("connect-key").value = "";
        byId("connect-client-id").value = "";
        byId("connect-client-secret").value = "";
        closeDialog("connect-dialog");
      }
      if (redirectUrl) {
        // Continue the OAuth flow at the provider; the callback returns here.
        window.location.assign(redirectUrl);
        return;
      }
      await refreshDetail(slug);
      await loadSummaries();
      showToast("Connect requested for " + slug + ".");
    } catch (err) {
      if (connectDialogToken === dialogToken && connectSlug === slug) {
        showConnectError(err.message || "Connect failed.");
      } else {
        reportActionError(err);
      }
    }
  }

  // --- import -------------------------------------------------------------

  function importRequest(file, mode) {
    var form = new FormData();
    form.append("file", file, file.name);
    return apiFetch("/api/plugins/import?mode=" + encodeURIComponent(mode), {
      method: "POST",
      body: form,
    });
  }

  async function doImport() {
    clearError();
    var input = byId("import-file");
    var file = input.files && input.files[0];
    if (!file) {
      showError("Choose a plugin package (.zip) first.");
      return;
    }
    try {
      var result;
      try {
        result = await importRequest(file, "reject");
      } catch (err) {
        if (err && err.code === "plugin_exists") {
          var replace = window.confirm(
            "A plugin with this slug already exists. Replace it?"
          );
          if (!replace) {
            return;
          }
          result = await importRequest(file, "replace");
        } else {
          throw err;
        }
      }
      if (input.files && input.files[0] === file) {
        input.value = "";
      }
      showToast("Imported " + (result && result.slug ? result.slug : "plugin") + ".");
      await loadSummaries();
    } catch (err) {
      reportActionError(err);
    }
  }

  // --- settings: backup and restore ---------------------------------------

  function backupRow(slug) {
    var checkbox = el("input", {
      type: "checkbox",
      value: slug,
      on: {
        change: function (event) {
          var checked = !!(event && event.target && event.target.checked);
          backupSelection[slug] = checked;
        },
      },
    });
    if (backupSelection[slug]) {
      checkbox.checked = true;
    }
    return el("label", { class: "checkbox-row" }, [
      checkbox,
      el("span", { text: slug }),
    ]);
  }

  function renderBackupPlugins(plugins) {
    var container = byId("backup-plugins");
    clear(container);
    var list = Array.isArray(plugins) ? plugins : [];
    var slugs = [];
    var retained = {};
    var i;
    for (i = 0; i < list.length; i += 1) {
      var slug = list[i] && list[i].slug;
      if (!slug) {
        continue;
      }
      slug = String(slug);
      slugs.push(slug);
      // Keep only checks for plugins that still exist; a stale check must not
      // silently back up (or omit) a plugin that is no longer installed.
      if (backupSelection[slug]) {
        retained[slug] = true;
      }
    }
    backupSelection = retained;
    if (slugs.length === 0) {
      append(container, el("p", { class: "muted", text: "No plugins installed." }));
      return;
    }
    for (i = 0; i < slugs.length; i += 1) {
      append(container, backupRow(slugs[i]));
    }
  }

  function setBackupSelection(checked) {
    var next = {};
    if (checked) {
      for (var i = 0; i < lastPlugins.length; i += 1) {
        var slug = lastPlugins[i] && lastPlugins[i].slug;
        if (slug) {
          next[String(slug)] = true;
        }
      }
    }
    backupSelection = next;
    renderBackupPlugins(lastPlugins);
  }

  function selectedBackupSlugs() {
    var slugs = [];
    var keys = Object.keys(backupSelection);
    for (var i = 0; i < keys.length; i += 1) {
      if (backupSelection[keys[i]]) {
        slugs.push(keys[i]);
      }
    }
    return slugs;
  }

  function doBackup() {
    var slugs = selectedBackupSlugs();
    var url = "/api/backup";
    if (slugs.length > 0) {
      var encoded = [];
      for (var i = 0; i < slugs.length; i += 1) {
        encoded.push(encodeURIComponent(slugs[i]));
      }
      url += "?plugins=" + encoded.join(",");
    }
    // A full-page navigation lets the browser honour the attachment response
    // and stream the ZIP to disk without buffering it in JavaScript.
    window.location.assign(url);
  }

  function outcomeLabel(outcome) {
    if (outcome === "restored") {
      return "Restored";
    }
    if (outcome === "skipped") {
      return "Skipped";
    }
    if (outcome === "failed") {
      return "Failed";
    }
    return String(outcome || "Unknown");
  }

  function restoreResultRow(result) {
    var outcome = result.outcome || "unknown";
    return el("div", { class: "result-row" }, [
      el("span", {
        class: "badge outcome-" + safeToken(outcome),
        text: outcomeLabel(outcome),
      }),
      el("span", { class: "result-slug", text: String(result.slug || "unknown") }),
      el("span", {
        class: "result-status",
        text: result.status ? statusLabel(result.status) : "No status",
      }),
      el("span", {
        class: "result-detail muted",
        text: result.detail ? String(result.detail) : "",
      }),
    ]);
  }

  function renderRestoreResults(data) {
    var container = byId("restore-result-list");
    clear(container);
    var results = data && Array.isArray(data.results) ? data.results : [];
    var restored = data && Array.isArray(data.restored) ? data.restored.length : 0;
    var skipped = data && Array.isArray(data.skipped) ? data.skipped.length : 0;
    var failed = data && Array.isArray(data.failed) ? data.failed.length : 0;
    byId("restore-summary").textContent =
      "Restored " +
      restored +
      ", skipped " +
      skipped +
      ", failed " +
      failed +
      ".";
    if (results.length === 0) {
      append(
        container,
        el("p", { class: "muted", text: "No plugins in this backup." })
      );
    } else {
      for (var i = 0; i < results.length; i += 1) {
        append(container, restoreResultRow(results[i] || {}));
      }
    }
    byId("restore-results").hidden = false;
  }

  async function doRestore() {
    clearError();
    var input = byId("restore-file");
    var file = input.files && input.files[0];
    if (!file) {
      showError("Choose a backup ZIP first.");
      return;
    }
    var mode = byId("restore-mode").value === "replace" ? "replace" : "skip";
    var slugs = byId("restore-slugs").value.trim();
    var query = "mode=" + encodeURIComponent(mode);
    if (slugs) {
      query += "&slugs=" + encodeURIComponent(slugs);
    }
    var form = new FormData();
    form.append("file", file, file.name);
    var token = nextToken("restore");
    try {
      var data = await apiFetch("/api/restore?" + query, {
        method: "POST",
        body: form,
      });
      if (!tokenIsCurrent("restore", token)) {
        return;
      }
      renderRestoreResults(data);
      if (input.files && input.files[0] === file) {
        input.value = "";
      }
      showToast("Restore finished.");
      // The restore changed live plugin state; refresh the grid afterwards.
      await loadSummaries();
    } catch (err) {
      if (!tokenIsCurrent("restore", token)) {
        return;
      }
      reportActionError(err);
    }
  }

  // --- routing ------------------------------------------------------------

  function safeDecode(value) {
    try {
      return decodeURIComponent(value);
    } catch (err) {
      // A malformed percent escape in the hash must never break routing.
      return value;
    }
  }

  function parseQuery(text) {
    var result = {};
    if (!text) {
      return result;
    }
    var pairs = text.split("&");
    for (var i = 0; i < pairs.length; i += 1) {
      var pair = pairs[i];
      if (!pair) {
        continue;
      }
      var eq = pair.indexOf("=");
      var key = eq === -1 ? pair : pair.slice(0, eq);
      var value = eq === -1 ? "" : pair.slice(eq + 1);
      result[safeDecode(key)] = safeDecode(value);
    }
    return result;
  }

  function setHash(value) {
    if (window.location.hash !== value) {
      window.location.hash = value;
    }
  }

  function currentPath() {
    var raw = window.location.hash || "#/";
    var withoutHash = raw.replace(/^#/, "");
    var path = withoutHash.split("?")[0];
    return path || "/";
  }

  function route() {
    // Invalidate any in-flight detail response and any open modals so a late
    // reply or an old dialog can never act on the route the user is now on.
    nextToken("detail");
    nextToken("detailOpen");
    closeDialog("connect-dialog");
    var raw = window.location.hash || "#/";
    var withoutHash = raw.replace(/^#/, "");
    var parts = withoutHash.split("?");
    var path = parts[0] || "/";
    var query = parseQuery(parts[1] || "");

    if (path === "/" || path === "") {
      currentSlug = null;
      lastDetail = null;
      closeDialog("detail-dialog");
      loadSummaries();
      return;
    }

    var match = path.match(/^\/plugins\/([^/]+)$/);
    if (match) {
      var slug = safeDecode(match[1]);
      // Bind the visible detail and the action target to the route immediately,
      // before the fetch resolves.
      currentSlug = slug;
      renderDetailLoading(slug);
      if (query.oauth) {
        // The OAuth result is displayed and then cleared: it must never be
        // persisted or reused, so rewrite the URL without the query.
        history.replaceState(null, "", "#/plugins/" + encodeURIComponent(slug));
      }
      openDetail(slug, query.oauth || null);
      return;
    }

    setHash("#/");
  }

  // --- wiring -------------------------------------------------------------

  function bindEvents() {
    byId("logout").addEventListener("click", doLogout);
    byId("refresh-plugins").addEventListener("click", loadSummaries);
    byId("import-button").addEventListener("click", doImport);

    byId("backup-download").addEventListener("click", doBackup);
    byId("backup-select-all").addEventListener("click", function () {
      setBackupSelection(true);
    });
    byId("backup-select-none").addEventListener("click", function () {
      setBackupSelection(false);
    });
    byId("restore-button").addEventListener("click", doRestore);

    byId("detail-close").addEventListener("click", function () {
      closeDialog("detail-dialog");
    });
    byId("detail-copy-endpoint").addEventListener("click", function () {
      copyText(byId("detail-endpoint").textContent);
    });
    byId("detail-copy-callback").addEventListener("click", function () {
      copyText(byId("detail-callback").textContent);
    });
    byId("detail-refresh-logs").addEventListener("click", refreshLogs);
    byId("detail-connect").addEventListener("click", openConnectDialog);
    byId("detail-disconnect").addEventListener("click", doDisconnect);
    byId("detail-reload").addEventListener("click", doReload);
    byId("detail-remove").addEventListener("click", doRemove);

    byId("connect-cancel").addEventListener("click", function () {
      closeDialog("connect-dialog");
    });
    byId("connect-mode").addEventListener("change", updateConnectFields);
    byId("connect-form").addEventListener("submit", submitConnect);

    byId("connect-dialog").addEventListener("close", function () {
      connectSlug = null;
      connectDialogToken = null;
    });

    byId("detail-dialog").addEventListener("close", function () {
      currentSlug = null;
      lastDetail = null;
      if (currentPath() !== "/") {
        setHash("#/");
      }
    });

    window.addEventListener("hashchange", route);
  }

  async function init() {
    bindEvents();
    var authenticated = await guardSession();
    if (!authenticated) {
      return;
    }
    route();
  }

  init();
})();
