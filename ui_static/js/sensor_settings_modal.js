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
  }
  modalEl.__showSensorPane = showPane;

  const paneTriggers = modalEl.querySelectorAll("button[data-pane-target], .list-item[data-pane-target]");
  paneTriggers.forEach((item) => {
    item.addEventListener("click", () => {
      const target = item.getAttribute("data-pane-target") || "";
      if (target) showPane(target);
    });
  });

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
