/**
 * Live RunPod deploy banner on /gpu-selection (poll + render without full reload).
 */
(function (global) {
  var POLL_MS = 2500;
  var POLL_MAX = 48;

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderDeployStatusBanner(status) {
    if (!status || !status.show_deploy_banner) {
      return "";
    }
    var hw = status.hardware || {};
    var hwName = hw.name ? esc(hw.name) : "";
    var vram = hw.vram_gb != null ? "(" + esc(hw.vram_gb) + " GB)" : "";
    var cost =
      status.cost_per_hour != null
        ? "$" + Number(status.cost_per_hour).toFixed(2) + "/hr"
        : hw.price_per_hour != null
          ? "$" + Number(hw.price_per_hour).toFixed(2) + "/hr (est.)"
          : "";
    var modelsHtml = "";
    var ms = status.model_status;
    if (ms && ms.models && ms.models.length) {
      var rows = ms.models
        .map(function (m) {
          return (
            "<tr><td>" +
            esc(m.role) +
            "</td><td>" +
            esc(m.engine) +
            "</td><td><code>" +
            esc(m.model) +
            "</code>" +
            (m.detail ? '<div class="deploy-status-meta">' + esc(m.detail) + "</div>" : "") +
            "</td><td>" +
            esc(m.status) +
            "</td></tr>"
          );
        })
        .join("");
      modelsHtml =
        '<div class="deploy-models-section"><h4 class="deploy-models-title">Pipeline models</h4>' +
        '<p class="deploy-status-meta">' +
        esc(ms.note || "") +
        "</p>" +
        '<table class="deploy-models-table"><thead><tr><th>Role</th><th>Engine</th><th>Model</th><th>Status</th></tr></thead><tbody>' +
        rows +
        "</tbody></table></div>";
    }
    var stopLabel =
      status.compute_mode === "serverless"
        ? status.stop_action_label || "Stop serverless"
        : status.stop_action_label || "Stop pod";
    var stopMetrics = "";
    if (status.compute_mode === "serverless") {
      var idleSec = status.serverless_idle_timeout_sec;
      var maxUsd = status.serverless_max_cost_per_hour_usd;
      if (idleSec != null || maxUsd != null) {
        stopMetrics =
          '<span class="deploy-stop-metrics">' +
          (idleSec != null ? "Idle timeout: " + esc(idleSec) + "s" : "") +
          (idleSec != null && maxUsd != null ? " · " : "") +
          (maxUsd != null ? "Max ~$" + esc(Number(maxUsd).toFixed(2)) + "/hr at full scale" : "") +
          "</span>";
      }
    }
    var stopBtn = status.can_stop
      ? '<div class="deploy-stop-row"><button type="button" class="deploy-stop-btn" id="stopDeployPodBtn">' +
        esc(stopLabel) +
        "</button>" +
        stopMetrics +
        "</div>"
      : "";
    var deleteVolBtn =
      status.can_delete_volume
        ? '<button type="button" class="deploy-delete-volume-btn" id="deleteNetworkVolumeBtn">Delete network volume</button>'
        : "";
    var delayLine = "";
    if (status.last_delay_time_ms != null) {
      delayLine =
        '<li><span class="deploy-status-k">Last delayTime</span> ' +
        esc(String(status.last_delay_time_ms)) +
        " ms (" +
        (Number(status.last_delay_time_ms) / 1000).toFixed(1) +
        "s cold start + queue)</li>";
    }
    if (status.last_execution_time_ms != null) {
      delayLine +=
        '<li><span class="deploy-status-k">Last executionTime</span> ' +
        esc(String(status.last_execution_time_ms)) +
        " ms</li>";
    }
    return (
      '<section class="deploy-status-banner deploy-status-' +
      esc(status.phase || "unknown") +
      '" id="computeDeployStatus" data-compute-mode="' +
      esc(status.compute_mode || "") +
      '">' +
      '<div class="deploy-status-header"><span class="deploy-status-badge">' +
      esc(status.phase_label || "") +
      "</span> <strong>RunPod compute</strong>" +
      (hwName ? " — " + hwName + " " + vram : "") +
      '</div><ul class="deploy-status-details">' +
      (status.compute_mode === "serverless"
        ? '<li><span class="deploy-status-k">Mode</span> RunPod Serverless (scale to zero)</li>'
        : "") +
      (status.endpoint_id
        ? '<li><span class="deploy-status-k">Endpoint ID</span> <code>' +
          esc(status.endpoint_id) +
          "</code></li>"
        : "") +
      (status.network_volume_id
        ? '<li><span class="deploy-status-k">Network volume</span> <code>' +
          esc(status.network_volume_id) +
          "</code></li>"
        : "") +
      (status.workers_max != null
        ? '<li><span class="deploy-status-k">Workers max</span> ' + esc(status.workers_max) + "</li>"
        : "") +
      (status.serverless_models
        ? '<li><span class="deploy-status-k">Models</span> Whisper ' +
          esc(status.serverless_models.whisper) +
          ", Ollama " +
          esc(status.serverless_models.ollama) +
          "</li>"
        : "") +
      (status.warmup_status
        ? '<li><span class="deploy-status-k">Warmup</span> ' + esc(status.warmup_status) + "</li>"
        : "") +
      delayLine +
      (status.pod_id
        ? '<li><span class="deploy-status-k">Pod ID</span> <code>' + esc(status.pod_id) + "</code></li>"
        : "") +
      (status.runpod_live_status
        ? '<li><span class="deploy-status-k">RunPod status</span> ' + esc(status.runpod_live_status) + "</li>"
        : "") +
      (cost ? '<li><span class="deploy-status-k">Cost</span> ' + esc(cost) + "</li>" : "") +
      (status.data_center_id
        ? '<li><span class="deploy-status-k">Data center</span> ' + esc(status.data_center_id) + "</li>"
        : "") +
      (status.idle_auto_stop_note
        ? '<li class="deploy-status-warn"><span class="deploy-status-k">Idle stop</span> ' +
          esc(status.idle_auto_stop_note) +
          (status.idle_monitor_url
            ? ' <a href="' + esc(status.idle_monitor_url) + '" target="_blank" rel="noopener">Idle monitor</a>'
            : "") +
          "</li>"
        : "") +
      (status.spend_cap_note
        ? '<li class="deploy-status-warn"><span class="deploy-status-k">Spend cap</span> ' +
          esc(status.spend_cap_note) +
          (status.billing_docs_url
            ? ' <a href="' + esc(status.billing_docs_url) + '" target="_blank" rel="noopener">Billing</a>'
            : "") +
          "</li>"
        : "") +
      (status.deploy_message
        ? '<li><span class="deploy-status-k">Last deploy</span> ' + esc(status.deploy_message) + "</li>"
        : "") +
      (status.runpod_error
        ? '<li class="deploy-status-warn">' + esc(status.runpod_error) + "</li>"
        : "") +
      '</ul><div class="deploy-status-links">' +
      (status.console_url
        ? '<a href="' +
          esc(status.console_url) +
          '" target="_blank" rel="noopener">Open in RunPod console</a> '
        : "") +
      (status.ssh_command
        ? '<span class="deploy-status-k">SSH</span> <code class="deploy-ssh-cmd">' +
          esc(status.ssh_command) +
          "</code> "
        : "") +
      '<button type="button" class="deploy-refresh-btn" id="refreshDeployStatusBtn">Refresh status</button> ' +
      stopBtn +
      " " +
      deleteVolBtn +
      "</div>" +
      modelsHtml +
      '<p class="deploy-status-meta">Checked ' +
      esc(status.checked_at || "") +
      "</p></section>"
    );
  }

  function mountDeployStatus(status) {
    var mount = document.getElementById("deployStatusMount");
    if (!mount) return;
    mount.innerHTML = renderDeployStatusBanner(status);
    wireDeployStatusButtons();
  }

  function wireDeployStatusButtons() {
    var refreshBtn = document.getElementById("refreshDeployStatusBtn");
    if (refreshBtn && !refreshBtn.dataset.wired) {
      refreshBtn.dataset.wired = "1";
      refreshBtn.addEventListener("click", function () {
        refreshBtn.disabled = true;
        refreshBtn.textContent = "Refreshing…";
        fetchDeployStatusOnce().finally(function () {
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
        var isServerless =
          banner && banner.getAttribute("data-compute-mode") === "serverless";
        var msg = isServerless
          ? "Scale serverless workers to zero? Running GPU workers stop billing; the endpoint remains."
          : "Stop and terminate this RunPod pod? Billing stops after termination.";
        if (!window.confirm(msg)) return;
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping…";
        fetch("/api/compute-deploy-stop", { method: "POST" })
          .then(function (r) {
            return r.json();
          })
          .then(function (data) {
            if (!data.ok) alert(data.error || "Stop failed");
            if (data.deploy_status) mountDeployStatus(data.deploy_status);
            else fetchDeployStatusOnce();
          })
          .catch(function (e) {
            alert(String(e));
          });
      });
    }
    var deleteBtn = document.getElementById("deleteNetworkVolumeBtn");
    if (deleteBtn && !deleteBtn.dataset.wired) {
      deleteBtn.dataset.wired = "1";
      deleteBtn.addEventListener("click", function () {
        if (
          !window.confirm(
            "Delete the RunPod network volume? All cached model weights on this volume are permanently lost. Next cold starts re-download NLLB/Ollama/Whisper caches."
          )
        ) {
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
          .then(function (r) {
            return r.json();
          })
          .then(function (data) {
            if (!data.ok) alert(data.error || "Delete failed");
            if (data.deploy_status) mountDeployStatus(data.deploy_status);
            else fetchDeployStatusOnce();
          })
          .catch(function (e) {
            alert(String(e));
          });
      });
    }
  }

  function fetchDeployStatusOnce() {
    return fetch("/api/compute-deploy-status")
      .then(function (r) {
        return r.json();
      })
      .then(function (status) {
        mountDeployStatus(status);
        return status;
      });
  }

  var pollTimer = null;
  var pollCount = 0;

  function stopDeployStatusPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    pollCount = 0;
  }

  function startDeployStatusPolling(initialStatus) {
    stopDeployStatusPolling();
    if (initialStatus) {
      mountDeployStatus(initialStatus);
    }
    pollCount = 0;
    pollTimer = setInterval(function () {
      pollCount += 1;
      fetchDeployStatusOnce().then(function (status) {
        if (
          status &&
          status.show_deploy_banner &&
          (status.phase === "running" || status.phase === "ready")
        ) {
          stopDeployStatusPolling();
        } else if (pollCount >= POLL_MAX) {
          stopDeployStatusPolling();
        }
      });
    }, POLL_MS);
    fetchDeployStatusOnce();
  }

  global.DeployStatusUI = {
    mountDeployStatus: mountDeployStatus,
    startDeployStatusPolling: startDeployStatusPolling,
    stopDeployStatusPolling: stopDeployStatusPolling,
    fetchDeployStatusOnce: fetchDeployStatusOnce,
    wireDeployStatusButtons: wireDeployStatusButtons,
  };
})(window);
