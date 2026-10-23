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
    var stopBtn = status.can_stop
      ? '<button type="button" class="deploy-stop-btn" id="stopDeployPodBtn">Stop pod</button>'
      : "";
    return (
      '<section class="deploy-status-banner deploy-status-' +
      esc(status.phase || "unknown") +
      '" id="computeDeployStatus">' +
      '<div class="deploy-status-header"><span class="deploy-status-badge">' +
      esc(status.phase_label || "") +
      "</span> <strong>RunPod compute</strong>" +
      (hwName ? " — " + hwName + " " + vram : "") +
      '</div><ul class="deploy-status-details">' +
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
        if (!window.confirm("Stop and terminate this RunPod pod? Billing stops after termination.")) return;
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
        if (status && status.show_deploy_banner && status.phase === "running") {
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
