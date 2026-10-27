/**
 * Live RunPod status on /gpu-selection: read-only polling, stale-aware rendering, and the deploy card.
 *
 * Polling only issues GET requests to /api/compute-deploy-status. It never deploys, starts, stops, or changes any
 * RunPod resource. The state label and explanation come from the server (src/serverless_live_status.py), so the
 * page does not duplicate provider logic.
 */
(function (global) {
  var POLL_INTERVAL_MS = 5000;
  var REQUEST_TIMEOUT_MS = 25000;
  var BACKOFF_MAX_MS = 60000;
  var STATUS_URL = "/api/compute-deploy-status";

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Fallback when the server did not send a live state. Mirrors deploy_state_label in the server partial.
  function fallbackLabel(status) {
    if (status.runpod_error && (status.phase === "ready" || status.phase === "stopped")) return "Status unavailable";
    switch (status.phase) {
      case "ready": return "Configured";
      case "stopped": return "Scaled to zero";
      case "deploying": return "Deploying";
      case "failed": return "Deployment failed";
      case "running": return "Running";
      case "manual": return "Manual setup";
      default: return "Status unknown";
    }
  }

  // Shown beside the status label. Set as text after mounting, so it can update without rebuilding the controls.
  function checkedTextFor(status, meta) {
    meta = meta || {};
    if (meta.stale && meta.lastOkAt) return "Last successful check " + clock(meta.lastOkAt);
    if (meta.unavailable && !meta.lastOkAt) return "Not checked successfully yet";
    if (meta.lastOkAt) return "Last checked " + clock(meta.lastOkAt);
    return status && status.checked_at ? "Checked " + status.checked_at : "";
  }

  function clock(ms) {
    if (!ms) return "";
    var d = new Date(ms);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  // meta: { stale: bool, unavailable: bool, lastOkAt: ms|null }
  function renderDeployStatusBanner(status, meta) {
    meta = meta || {};
    if (!status || (!status.show_deploy_banner && !meta.unavailable)) {
      return "";
    }
    var hw = status.hardware || {};
    var live = status.live || null;
    var label = live ? live.label : fallbackLabel(status);
    var tone = live ? live.tone : "amber";
    var explanation = live ? live.explanation : "Status could not be read.";
    var workersText = "";
    if (live && live.workers) {
      workersText = '<span class="live-workers">Reported: running ' + esc(live.workers.running) + " · idle " + esc(live.workers.idle) + "</span>";
    }
    if (meta.stale) {
      label = "Status unavailable";
      tone = "amber";
      explanation = "Showing the last successful check" +
        (meta.lastOkAt ? " from " + clock(meta.lastOkAt) : "") +
        ". The current check failed, which does not mean the endpoint is down.";
      workersText = "";
    } else if (meta.unavailable && !status.live) {
      label = "Status unavailable";
      tone = "amber";
      explanation = "No successful check yet. The current check failed.";
    }
    var workersMax = status.workers_max;
    var workersMin = status.workers_min;
    var rate = hw.price_per_hour
      ? "$" + Number(hw.price_per_hour).toFixed(3) + "/hr per active worker (list price)"
      : "Not available";
    var scaling = workersMin === 0 ? "Scales to zero when idle" : workersMin == null ? "Unknown" : "Minimum " + esc(workersMin) + " worker(s)";
    var facts =
      '<dl class="deploy-facts">' +
      "<div><dt>GPU</dt><dd>" + esc(hw.name || "Not selected") + (hw.vram_gb ? " · " + esc(hw.vram_gb) + " GB" : "") + "</dd></div>" +
      "<div><dt>Region</dt><dd>" + esc(status.data_center_id || "Not set") + "</dd></div>" +
      "<div><dt>Rate</dt><dd>" + esc(rate) + "</dd></div>" +
      "<div><dt>Scaling</dt><dd>" + esc(scaling) + "</dd></div>" +
      "<div><dt>Max workers</dt><dd>" + (workersMax == null ? "Unknown" : esc(workersMax) + " configured") + "</dd></div>" +
      "</dl>";
    var consoleLink = status.console_url
      ? '<div class="deploy-summary-actions"><a href="' + esc(status.console_url) + '" target="_blank" rel="noopener">Open cloud console</a></div>'
      : "";

    var items = [];
    if (status.endpoint_id) items.push('<li><span class="deploy-status-k">Endpoint ID</span> <code>' + esc(status.endpoint_id) + "</code></li>");
    if (status.network_volume_id) items.push('<li><span class="deploy-status-k">Network volume</span> <code>' + esc(status.network_volume_id) + "</code></li>");
    if (status.pod_id) items.push('<li><span class="deploy-status-k">Pod ID</span> <code>' + esc(status.pod_id) + "</code></li>");
    if (status.runpod_live_status) items.push('<li><span class="deploy-status-k">RunPod status</span> ' + esc(status.runpod_live_status) + "</li>");
    if (status.serverless_idle_timeout_sec != null) items.push('<li><span class="deploy-status-k">Idle timeout</span> ' + esc(status.serverless_idle_timeout_sec) + " s</li>");
    if (status.last_delay_time_ms != null) items.push('<li><span class="deploy-status-k">Last delayTime</span> ' + esc(status.last_delay_time_ms) + " ms</li>");
    if (status.last_execution_time_ms != null) items.push('<li><span class="deploy-status-k">Last executionTime</span> ' + esc(status.last_execution_time_ms) + " ms</li>");
    if (status.warmup_status) items.push('<li><span class="deploy-status-k">Warmup</span> ' + esc(status.warmup_status) + "</li>");
    if (status.deploy_message) items.push('<li><span class="deploy-status-k">Last deploy</span> ' + esc(status.deploy_message) + "</li>");
    if (status.runpod_error) items.push('<li class="deploy-status-warn">' + esc(status.runpod_error) + "</li>");
    if (status.ssh_command) items.push('<li><span class="deploy-status-k">SSH</span> <code class="deploy-ssh-cmd">' + esc(status.ssh_command) + "</code></li>");
    if (status.idle_auto_stop_note) {
      items.push('<li class="deploy-status-warn"><span class="deploy-status-k">Idle stop</span> ' + esc(status.idle_auto_stop_note) +
        (status.idle_monitor_url ? ' <a href="' + esc(status.idle_monitor_url) + '" target="_blank" rel="noopener">Idle monitor</a>' : "") + "</li>");
    }
    if (status.spend_cap_note) {
      items.push('<li class="deploy-status-warn"><span class="deploy-status-k">Spend limits</span> ' + esc(status.spend_cap_note) +
        (status.billing_docs_url ? ' <a href="' + esc(status.billing_docs_url) + '" target="_blank" rel="noopener">Billing docs</a>' : "") + "</li>");
    }
    var stopLabel = status.compute_mode === "serverless" ? "Scale workers to zero" : status.stop_action_label || "Stop pod";
    var stopBtn = status.can_stop
      ? '<button type="button" class="deploy-stop-btn" id="stopDeployPodBtn">' + esc(stopLabel) + "</button>"
      : "";
    var deleteVolBtn = status.can_delete_volume
      ? '<button type="button" class="deploy-delete-volume-btn" id="deleteNetworkVolumeBtn">Delete network volume</button>'
      : "";
    var modelsHtml = "";
    var ms = status.model_status;
    if (ms && ms.models && ms.models.length) {
      var rows = ms.models
        .map(function (m) {
          return "<tr><td>" + esc(m.role) + "</td><td>" + esc(m.engine) + "</td><td><code>" + esc(m.model) + "</code>" +
            (m.detail ? '<div class="deploy-status-meta">' + esc(m.detail) + "</div>" : "") +
            "</td><td>" + esc(m.status) + "</td></tr>";
        })
        .join("");
      modelsHtml =
        '<div class="deploy-models-section"><h4 class="deploy-models-title">Pipeline models on the endpoint</h4>' +
        '<p class="deploy-status-meta">' + esc(ms.note || "") + "</p>" +
        '<table class="deploy-models-table"><thead><tr><th>Role</th><th>Engine</th><th>Model</th><th>Status</th></tr></thead><tbody>' +
        rows + "</tbody></table></div>";
    }
    return (
      '<section class="gpu-card deploy-summary deploy-status-banner deploy-status-' + esc(status.phase || "unknown") +
      '" id="computeDeployStatus" data-compute-mode="' + esc(status.compute_mode || "") + '">' +
      '<div class="live-status live-tone-' + esc(tone) + '" id="liveStatus" role="status" aria-live="polite">' +
      '<span class="live-dot" aria-hidden="true"></span>' +
      '<strong class="live-label">' + esc(label) + "</strong> " + workersText + " " +
      '<span class="live-checked"></span> ' +
      '<button type="button" class="deploy-refresh-btn" id="refreshDeployStatusBtn">Refresh status</button>' +
      "</div>" +
      '<p class="gpu-meta live-explanation" id="liveExplanation">' + esc(explanation) + "</p>" +
      facts + consoleLink +
      '<details class="gpu-advanced deploy-advanced"><summary>Advanced details</summary>' +
      '<ul class="deploy-status-details">' + items.join("") + "</ul>" +
      '<div class="deploy-stop-row">' + stopBtn + " " + deleteVolBtn + "</div>" +
      modelsHtml +
      "</details></section>"
    );
  }

  // Polling controller. All environment access is injected, so the timing rules can be tested with a fake clock.
  // It only reads the status endpoint with GET, and it never overlaps requests.
  function createStatusPoller(deps) {
    var state = {
      running: false,
      timer: null,
      inFlight: null,
      controller: null,
      timeoutTimer: null,
      failures: 0,
      lastOk: null,
      lastOkAt: null,
    };

    function clearTimer() {
      if (state.timer != null) {
        deps.clearTimeout(state.timer);
        state.timer = null;
      }
    }

    function schedule(delayMs) {
      clearTimer();
      if (!state.running || !deps.isVisible()) return;
      state.timer = deps.setTimeout(poll, delayMs);
    }

    function poll() {
      clearTimer();
      if (!state.running) return Promise.resolve(null);
      if (state.inFlight) return state.inFlight;
      var controller = deps.createAbortController();
      state.controller = controller;
      state.timeoutTimer = deps.setTimeout(function () {
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
      var request = deps
        .fetch(STATUS_URL, {
          method: "GET",
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (status) {
          if (!state.running) return null;
          state.failures = 0;
          state.lastOk = status;
          state.lastOkAt = deps.now();
          deps.render(status, { stale: false, unavailable: false, lastOkAt: state.lastOkAt });
          schedule(POLL_INTERVAL_MS);
          return status;
        })
        .catch(function () {
          if (!state.running) return null;
          state.failures += 1;
          deps.render(state.lastOk, {
            stale: !!state.lastOk,
            unavailable: true,
            lastOkAt: state.lastOkAt,
          });
          var backoff = Math.min(POLL_INTERVAL_MS * Math.pow(2, state.failures), BACKOFF_MAX_MS);
          schedule(backoff);
          return null;
        })
        .then(function (result) {
          deps.clearTimeout(state.timeoutTimer);
          state.timeoutTimer = null;
          state.inFlight = null;
          state.controller = null;
          return result;
        });
      state.inFlight = request;
      return request;
    }

    return {
      start: function () {
        state.running = true;
        return poll();
      },
      refreshNow: function () {
        return poll();
      },
      // Called when the page is hidden: stop scheduling. A request already in flight finishes, and no new one starts.
      pause: function () {
        clearTimer();
      },
      // Called when the page is visible again: refresh immediately and resume the interval.
      resume: function () {
        if (state.running) return poll();
        return Promise.resolve(null);
      },
      stop: function () {
        state.running = false;
        clearTimer();
        if (state.timeoutTimer != null) {
          deps.clearTimeout(state.timeoutTimer);
          state.timeoutTimer = null;
        }
        if (state.controller) state.controller.abort();
        state.inFlight = null;
        state.controller = null;
      },
      _state: state,
    };
  }

  var lastHtml = null;
  var poller = null;

  function mountDeployStatus(status, meta) {
    var mount = document.getElementById("deployStatusMount");
    if (!mount) return;
    var html = renderDeployStatusBanner(status, meta);
    if (html !== lastHtml) {
      var openDetails = mount.querySelector("details.deploy-advanced");
      var wasOpen = !!(openDetails && openDetails.open);
      lastHtml = html;
      mount.innerHTML = html || noDeploymentHtml(status);
      var newDetails = mount.querySelector("details.deploy-advanced");
      if (newDetails && wasOpen) newDetails.open = true;
      wireDeployStatusButtons();
    }
    var checkedEl = mount.querySelector(".live-checked");
    if (checkedEl) checkedEl.textContent = checkedTextFor(status, meta);
  }

  function noDeploymentHtml() {
    return '<section class="gpu-card" id="noDeploymentNote"><h2 class="section-title">Current deployment</h2>' +
      '<p class="gpu-meta">No endpoint has been deployed yet. Choose a GPU below and save to deploy.</p></section>';
  }

  function wireDeployStatusButtons() {
    var refreshBtn = document.getElementById("refreshDeployStatusBtn");
    if (refreshBtn && !refreshBtn.dataset.wired) {
      refreshBtn.dataset.wired = "1";
      refreshBtn.addEventListener("click", function () {
        refreshBtn.disabled = true;
        refreshBtn.textContent = "Refreshing…";
        var pending = poller ? poller.refreshNow() : fetchDeployStatusOnce();
        Promise.resolve(pending).then(function () {
          refreshBtn.disabled = false;
          refreshBtn.textContent = "Refresh status";
        });
      });
    }
    var stopBtn = document.getElementById("stopDeployPodBtn");
    if (stopBtn && !stopBtn.dataset.wired) {
      stopBtn.dataset.wired = "1";
      stopBtn.addEventListener("click", function () {
        var banner = document.getElementById("computeDeployStatus");
        var isServerless = banner && banner.getAttribute("data-compute-mode") === "serverless";
        var msg = isServerless
          ? "Scale serverless workers to zero? Running GPU workers stop billing; the endpoint remains."
          : "Stop and terminate this pod? Billing stops after termination.";
        if (!window.confirm(msg)) return;
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping…";
        fetch("/api/compute-deploy-stop", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.ok) alert(data.error || "Stop failed");
            if (data.deploy_status) mountDeployStatus(data.deploy_status);
            else fetchDeployStatusOnce();
          })
          .catch(function (e) { alert(String(e)); });
      });
    }
    var deleteBtn = document.getElementById("deleteNetworkVolumeBtn");
    if (deleteBtn && !deleteBtn.dataset.wired) {
      deleteBtn.dataset.wired = "1";
      deleteBtn.addEventListener("click", function () {
        if (!window.confirm("Delete the network volume? All cached model weights on this volume are permanently lost. Next cold starts re-download NLLB/Ollama/Whisper caches.")) {
          return;
        }
        var typed = window.prompt('Type DELETE to confirm volume deletion:', "");
        if (typed !== "DELETE") {
          alert("Volume deletion cancelled.");
          return;
        }
        deleteBtn.disabled = true;
        deleteBtn.textContent = "Deleting…";
        fetch("/api/compute-network-volume-delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: "DELETE" }),
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.ok) alert(data.error || "Delete failed");
            if (data.deploy_status) mountDeployStatus(data.deploy_status);
            else fetchDeployStatusOnce();
          })
          .catch(function (e) { alert(String(e)); });
      });
    }
  }

  // One-off read used after mutations (stop, delete). Polling uses the controller instead.
  function fetchDeployStatusOnce() {
    return fetch(STATUS_URL, { cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (status) {
        mountDeployStatus(status, { stale: false, unavailable: false, lastOkAt: Date.now() });
        return status;
      });
  }

  function startDeployStatusPolling(initialStatus) {
    if (initialStatus) mountDeployStatus(initialStatus, { stale: false, unavailable: false, lastOkAt: Date.now() });
    if (poller) return poller.refreshNow();
    return initPoller().start();
  }

  function stopDeployStatusPolling() {
    if (poller) poller.stop();
  }

  function initPoller() {
    poller = createStatusPoller({
      fetch: function (url, opts) { return global.fetch(url, opts); },
      setTimeout: function (fn, ms) { return global.setTimeout(fn, ms); },
      clearTimeout: function (id) { global.clearTimeout(id); },
      now: function () { return Date.now(); },
      isVisible: function () { return document.visibilityState !== "hidden"; },
      createAbortController: function () { return new global.AbortController(); },
      render: function (status, meta) {
        if (!status && meta.unavailable) {
          mountDeployStatus({ show_deploy_banner: true, phase: "unknown", live: null }, meta);
          return;
        }
        mountDeployStatus(status, meta);
      },
    });
    document.addEventListener("visibilitychange", function () {
      if (!poller) return;
      if (document.visibilityState === "hidden") poller.pause();
      else poller.resume();
    });
    global.addEventListener("pagehide", function () { if (poller) poller.stop(); });
    return poller;
  }

  global.DeployStatusUI = {
    mountDeployStatus: mountDeployStatus,
    startDeployStatusPolling: startDeployStatusPolling,
    stopDeployStatusPolling: stopDeployStatusPolling,
    fetchDeployStatusOnce: fetchDeployStatusOnce,
    wireDeployStatusButtons: wireDeployStatusButtons,
    renderDeployStatusBanner: renderDeployStatusBanner,
    createStatusPoller: createStatusPoller,
    POLL_INTERVAL_MS: POLL_INTERVAL_MS,
    BACKOFF_MAX_MS: BACKOFF_MAX_MS,
  };

  // Start on the GPU page only: it has the deployment mount. Fetch immediately, then poll while visible.
  if (typeof document !== "undefined" && document.getElementById && document.getElementById("deployStatusMount")) {
    wireDeployStatusButtons();
    startDeployStatusPolling(null);
  }
})(typeof window !== "undefined" ? window : globalThis);
