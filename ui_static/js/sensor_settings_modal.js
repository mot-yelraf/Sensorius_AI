// ui_static/js/sensor_settings_modal.js
"use strict";

window.initSensorSettingsModal = function initSensorSettingsModal(modalEl) {
  if (!modalEl) return false;

  const menu = modalEl.querySelector("#sensorSettingsMenu");
  const panes = modalEl.querySelectorAll(".sensor-pane-view");

  function showPane(paneId) {
    panes.forEach((pane) => {
      const on = pane.id === paneId;
      pane.style.display = on ? "flex" : "none";
    });
    const items = menu ? menu.querySelectorAll("button[data-pane-target], .list-item[data-pane-target]") : [];
    items.forEach((item) => {
      const target = item.getAttribute("data-pane-target") || "";
      if (target === paneId) item.classList.add("active");
      else item.classList.remove("active");
    });
    const activePane = Array.from(panes).find((pane) => pane.id === paneId);
    if (activePane && activePane.matches("[data-stat-panel]")) {
      refreshStatsPanel(activePane);
    }
  }
  modalEl.__showSensorPane = showPane;

  const paneTriggers = modalEl.querySelectorAll("button[data-pane-target], .list-item[data-pane-target]");
  paneTriggers.forEach((item) => {
    item.addEventListener("click", () => {
      const target = item.getAttribute("data-pane-target") || "";
      if (target) showPane(target);
    });
  });

  function formatStatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "No offline events";
    let total = Math.max(0, Math.floor(seconds));
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const secs = total - minutes * 60;
    const parts = [];
    if (days) parts.push(days + "d");
    if (hours || parts.length) parts.push(hours + "h");
    if (minutes || parts.length) parts.push(minutes + "m");
    parts.push(secs + "s");
    return parts.join(" ");
  }

  function numberFromDataset(panel, name) {
    const raw = Number(panel.dataset[name] || 0);
    return Number.isFinite(raw) ? raw : 0;
  }

  function formatStatAgeFromEpoch(epoch, emptyText) {
    const raw = Number(epoch || 0);
    if (!Number.isFinite(raw) || raw <= 0) return emptyText;
    return formatStatDuration((Date.now() / 1000) - raw) + " ago";
  }

  function statStorageKey(panel) {
    const kind = String(panel.dataset.statPanel || "device");
    const id = String(panel.dataset.statId || "").trim() || "unknown";
    return "sensorius.stats." + kind + "." + id;
  }

  function readStatReset(panel) {
    try {
      const raw = window.localStorage ? window.localStorage.getItem(statStorageKey(panel)) : "";
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function writeStatReset(panel, payload) {
    try {
      if (window.localStorage) window.localStorage.setItem(statStorageKey(panel), JSON.stringify(payload));
    } catch (_err) {
      void _err;
    }
  }

  function renderStatsPanel(panel) {
    if (!panel) return;
    const reset = readStatReset(panel);
    const rawOffline = numberFromDataset(panel, "offlineEvents24h");
    const rawPackets = numberFromDataset(panel, "packetsReceived");
    const resetEpoch = Number(reset.resetEpoch || 0);
    const offlineBase = resetEpoch > 0 ? Number(reset.offlineEventsBase || 0) : 0;
    const packetsBase = resetEpoch > 0 ? Number(reset.packetsBase || 0) : 0;
    const offlineEl = panel.querySelector('[data-stat-value="offline-events"]');
    const packetsEl = panel.querySelector('[data-stat-value="packets"]');
    const uptimeEl = panel.querySelector('[data-stat-value="uptime"]');
    const lastPacketEl = panel.querySelector('[data-stat-value="last-packet"]');
    if (offlineEl) offlineEl.textContent = String(Math.max(0, rawOffline - offlineBase));
    if (packetsEl) packetsEl.textContent = String(Math.max(0, rawPackets - packetsBase));
    if (uptimeEl) {
      const lastOfflineEpoch = Number(panel.dataset.lastOfflineEpoch || 0);
      const baseEpoch = Math.max(lastOfflineEpoch > 0 ? lastOfflineEpoch : 0, resetEpoch > 0 ? resetEpoch : 0);
      uptimeEl.textContent = baseEpoch > 0 ? formatStatDuration((Date.now() / 1000) - baseEpoch) : "No offline events";
    }
    if (lastPacketEl) {
      lastPacketEl.textContent = formatStatAgeFromEpoch(panel.dataset.lastPacketEpoch, "No packets");
    }
  }

  function applyStatsPayload(panel, data) {
    if (!panel || !data || data.ok === false) return;
    if (Object.prototype.hasOwnProperty.call(data, "offline_events_24h")) {
      panel.dataset.offlineEvents24h = String(data.offline_events_24h || 0);
    }
    if (Object.prototype.hasOwnProperty.call(data, "last_offline_epoch")) {
      panel.dataset.lastOfflineEpoch = data.last_offline_epoch == null ? "" : String(data.last_offline_epoch);
    }
    if (Object.prototype.hasOwnProperty.call(data, "last_packet_epoch")) {
      panel.dataset.lastPacketEpoch = data.last_packet_epoch == null ? "" : String(data.last_packet_epoch);
    }
    if (Object.prototype.hasOwnProperty.call(data, "data_packets_received")) {
      panel.dataset.packetsReceived = String(data.data_packets_received || 0);
    }
    const lastOfflineEl = panel.querySelector('[data-stat-value="last-offline"]');
    if (lastOfflineEl && data.last_offline_event_label) {
      lastOfflineEl.textContent = String(data.last_offline_event_label);
    }
    renderStatsPanel(panel);
  }

  async function refreshStatsPanel(panel) {
    if (!panel || !panel.dataset.statsUrl) return;
    try {
      const resp = await fetch(panel.dataset.statsUrl, {
        cache: "no-store",
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.ok === false) return;
      applyStatsPayload(panel, data);
    } catch (_err) {
      void _err;
    }
  }

  function isStatsPanelVisible(panel) {
    return !!panel && window.getComputedStyle(panel).display !== "none";
  }

  function initStatsPanels() {
    const statPanels = modalEl.querySelectorAll("[data-stat-panel]");
    statPanels.forEach((panel) => renderStatsPanel(panel));
    if (!modalEl.dataset.statsResetBound) {
      modalEl.dataset.statsResetBound = "1";
      modalEl.querySelectorAll("[data-stat-reset]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const panel = btn.closest("[data-stat-panel]");
          if (!panel) return;
          writeStatReset(panel, {
            resetEpoch: Date.now() / 1000,
            offlineEventsBase: numberFromDataset(panel, "offlineEvents24h"),
            packetsBase: numberFromDataset(panel, "packetsReceived"),
          });
          renderStatsPanel(panel);
        });
      });
    }
    if (!modalEl.__statsTimer) {
      modalEl.__statsTimer = window.setInterval(() => {
        if (!document.body.contains(modalEl)) {
          window.clearInterval(modalEl.__statsTimer);
          modalEl.__statsTimer = null;
          return;
        }
        statPanels.forEach((panel) => renderStatsPanel(panel));
      }, 1000);
    }
    if (!modalEl.__statsRefreshTimer) {
      modalEl.__statsRefreshTimer = window.setInterval(() => {
        if (!document.body.contains(modalEl)) {
          window.clearInterval(modalEl.__statsRefreshTimer);
          modalEl.__statsRefreshTimer = null;
          return;
        }
        statPanels.forEach((panel) => {
          if (isStatsPanelVisible(panel)) refreshStatsPanel(panel);
        });
      }, 5000);
    }
  }

  initStatsPanels();

  const form = modalEl.querySelector("#sensorSettingsForm");
  const saveBtn = modalEl.querySelector("#saveBtn");
  const spinner = modalEl.querySelector("#saveSpinner");
  const statusEl = modalEl.querySelector("#sensorSettingsStatus");
  const restartBtn = modalEl.querySelector("#sensorRestartBtn");
  if (form && saveBtn && spinner && statusEl && form.dataset.ajaxBound !== "1") {
    form.dataset.ajaxBound = "1";

    function setBusy(isBusy) {
      saveBtn.disabled = !!isBusy;
      if (restartBtn && restartBtn.dataset.pending !== "1") restartBtn.disabled = !!isBusy;
      spinner.style.display = isBusy ? "inline-block" : "none";
    }

    function setStatus(text) {
      statusEl.textContent = text || "";
    }

    function requestDashboardRefresh() {
      try {
        document.querySelectorAll(".metric-container[data-user-display-style]").forEach((el) => {
          delete el.dataset.userDisplayStyle;
        });
        if (typeof window.updateGauges === "function") {
          window.updateGauges();
        }
      } catch (err) {
        void err;
      }
    }

    function setRestartPending(isPending) {
      if (!restartBtn) return;
      if (!restartBtn.dataset.baseLabel) {
        restartBtn.dataset.baseLabel = (restartBtn.textContent || "Restart Device").trim();
      }
      restartBtn.dataset.pending = isPending ? "1" : "0";
      restartBtn.disabled = !!isPending;
      restartBtn.textContent = isPending ? "Device Restarting..." : (restartBtn.dataset.baseLabel || "Restart Device");
    }

    async function parseResponseError(resp) {
      const contentType = String(resp.headers.get("content-type") || "").toLowerCase();
      if (contentType.includes("application/json")) {
        const js = await resp.json().catch(() => ({}));
        return String(js.error || js.message || ("HTTP " + resp.status));
      }
      return String((await resp.text().catch(() => "")) || ("HTTP " + resp.status)).trim();
    }

    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      setBusy(true);
      setStatus("Saving...");
      try {
        const body = new URLSearchParams(new FormData(form));
        const resp = await fetch(form.action, {
          method: "POST",
          headers: {
            Accept: "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          },
          body: body.toString(),
        });
        const js = await resp.json().catch(() => ({}));
        if (!resp.ok || js.ok === false) {
          throw new Error(String(js.error || js.message || ("HTTP " + resp.status)));
        }
        setStatus("Saved.");
        requestDashboardRefresh();
        if (typeof window.showToast === "function") window.showToast("Sensor settings saved", "ok");
      } catch (err) {
        const msg = err && err.message ? err.message : "Failed to save sensor settings.";
        setStatus(msg);
        if (typeof window.showToast === "function") window.showToast("Failed to save sensor settings", "error");
      } finally {
        setBusy(false);
      }
    });

    if (restartBtn) {
      setRestartPending(false);
      restartBtn.addEventListener("click", async function () {
        const sensorIdInput = form.querySelector('input[name="sensor_id"]');
        const sensorId = (sensorIdInput && sensorIdInput.value)
          ? String(sensorIdInput.value)
          : String(modalEl.dataset.sensorId || "");
        if (!sensorId) {
          setStatus("Missing sensor id.");
          return;
        }
        if (!window.confirm("Restart this device now? Unsaved changes in this modal will not be applied.")) {
          return;
        }
        setBusy(true);
        setRestartPending(true);
        setStatus("Device Restarting...");
        try {
          const body = new URLSearchParams();
          body.set("sensor_id", sensorId);
          const resp = await fetch(restartBtn.dataset.restartUrl || "/sensor-settings/restart-device", {
            method: "POST",
            headers: {
              Accept: "application/json",
              "X-Requested-With": "XMLHttpRequest",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            body: body.toString(),
          });
          if (!resp.ok) {
            throw new Error(await parseResponseError(resp));
          }
          const js = await resp.json().catch(() => ({}));
          const message = String(js.message || "Device restarting...");
          setStatus(message);
          if (typeof window.showToast === "function") window.showToast(message, "ok");
        } catch (err) {
          const msg = err && err.message ? err.message : "Failed to restart device.";
          setRestartPending(false);
          setStatus(msg);
          if (typeof window.showToast === "function") window.showToast("Failed to restart device", "error");
        } finally {
          setBusy(false);
        }
      });
    }
  }

  showPane("sensor-settings-pane");
  return true;
};
