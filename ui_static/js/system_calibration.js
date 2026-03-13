// ui_static/js/system_calibration.js
"use strict";

window.initSystemCalibrationModal = async function(modalEl) {
  if (!modalEl) {
    console.error("initSystemCalibrationModal: missing modalEl");
    return false;
  }

  // Prefer backdrop as the wider scope
  const scope    = modalEl.closest(".modal-backdrop") || modalEl;
  const backdrop = scope.closest?.(".modal-backdrop") || scope;

  const sensorId   = modalEl.dataset.sensorId || modalEl.getAttribute("data-sensor-id") || "";
  const deviceKind = modalEl.dataset.deviceKind || modalEl.getAttribute("data-device-kind") || "";
  const isApvpd    = (modalEl.dataset.isApvpd || modalEl.getAttribute("data-is-apvpd") || "")
                       .toString().toLowerCase() === "true";

  // ---- Right-side (system calibration) controls ----
  const refSelect  = modalEl.querySelector("#sysCalRefSensor");
  const rangeInput = modalEl.querySelector("#sysCalRangeHours");
  const tableBody  = modalEl.querySelector("#sysCalSensorsTable");
  const statusEl   = modalEl.querySelector("#sysCalStatus");
  const previewBtn = modalEl.querySelector("#sysCalPreviewBtn");
  const applyBtn   = modalEl.querySelector("#sysCalApplyBtn");

  // ---- Left-side (device calibration) controls ----
  const devCalRows       = modalEl.querySelectorAll("[data-dev-cal-row]");
  const devCalApplyBtn   = modalEl.querySelector("#devCalApplyBtn");
  const devCalStatus     = modalEl.querySelector("#devCalStatus");
  const soilPhBufferBtns = modalEl.querySelectorAll(".soilPhBufferBtn");
  const soilPhCalStatus  = modalEl.querySelector("#soilPhCalStatus");
  const soilPhOffsetInput = modalEl.querySelector("#soilPhOffsetInput");

  // ---- APVPD plant calibration controls ----
  const plantCalBtn     = modalEl.querySelector("#plantCalBtn");
  const plantCalStatus  = modalEl.querySelector("#plantCalStatus");

  const homeBtn  = modalEl.querySelector("#sysCalHomeBtn");
  const closeBtn = modalEl.querySelector("#sysCalCloseBtn"); // currently not present, but allowed

  // Keep the last preview payload so Apply can reuse it
  let scLastPreview = null;

  function close() {
    if (backdrop && backdrop.parentNode) {
      backdrop.parentNode.removeChild(backdrop);
    }
  }

  if (closeBtn) closeBtn.addEventListener("click", close);
  if (homeBtn)  homeBtn.addEventListener("click", close);

  // -----------------------
  // Device calibration helpers
  // -----------------------
  function collectDeviceOffsets() {
    const results = [];
    devCalRows.forEach((row) => {
      const input = row.querySelector("input.devCalInput");
      if (!input) return;
      const key = input.dataset.key;
      if (!key) return;
      const raw = input.value;
      const value = raw === "" ? 0 : Number(raw);
      if (!Number.isFinite(value)) return;
      results.push({ key, value });
    });
    return results;
  }

  if (devCalApplyBtn) {
    devCalApplyBtn.addEventListener("click", async () => {
      if (!sensorId) {
        alert("Sensor ID not available for device calibration.");
        return;
      }
      const offsets = collectDeviceOffsets();
      if (!offsets.length) {
        alert("No offsets to apply.");
        return;
      }

      devCalApplyBtn.disabled = true;
      if (devCalStatus) devCalStatus.textContent = "Applying device calibration…";

      try {
        const resp = await fetch("/calibration/device/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            sensor_id: sensorId,
            device_kind: deviceKind,
            offsets: offsets,
          }),
        });

        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }

        const result = await resp.json().catch(() => ({}));
        const status = String(result.status || "").toLowerCase();

        if (status === "success" || status === "ok") {
          if (devCalStatus) {
            devCalStatus.textContent =
              result.message || "Device calibration updated.";
          }
        } else {
          if (devCalStatus) {
            devCalStatus.textContent =
              result.message || "Failed to apply device calibration.";
          }
        }
      } catch (err) {
        console.error("Device calibration apply error", err);
        if (devCalStatus) {
          devCalStatus.textContent = "Error applying device calibration.";
        }
      } finally {
        devCalApplyBtn.disabled = false;
      }
    });
  }

  async function calibrateSoilPhBuffer(bufferPh) {
    if (!sensorId) {
      alert("Sensor ID not available for pH calibration.");
      return;
    }

    soilPhBufferBtns.forEach((btn) => { btn.disabled = true; });
    if (soilPhCalStatus) {
      soilPhCalStatus.textContent = `Calibrating against pH ${Number(bufferPh).toFixed(1)}…`;
    }

    try {
      const resp = await fetch("/calibration/soil/ph-buffer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sensor_id: sensorId,
          buffer_ph: Number(bufferPh),
        }),
      });

      const result = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(result.message || ("HTTP " + resp.status));
      }

      if (soilPhOffsetInput && Number.isFinite(Number(result.soil_ph_offset))) {
        soilPhOffsetInput.value = String(result.soil_ph_offset);
      }
      if (devCalStatus && Number.isFinite(Number(result.soil_ph_offset))) {
        devCalStatus.textContent = `Soil pH offset set to ${Number(result.soil_ph_offset).toFixed(4)}.`;
      }
      if (soilPhCalStatus) {
        const measured = Number(result.measured_ph);
        const offset = Number(result.soil_ph_offset);
        soilPhCalStatus.textContent =
          `Measured ${Number.isFinite(measured) ? measured.toFixed(3) : "?"}, offset ${Number.isFinite(offset) ? offset.toFixed(4) : "?"}.`;
      }
    } catch (err) {
      console.error("Soil pH buffer calibration error", err);
      if (soilPhCalStatus) {
        soilPhCalStatus.textContent = err && err.message ? err.message : "Error applying pH calibration.";
      }
    } finally {
      soilPhBufferBtns.forEach((btn) => { btn.disabled = false; });
    }
  }

  if (deviceKind === "soil" && soilPhBufferBtns.length) {
    soilPhBufferBtns.forEach((btn) => {
      btn.addEventListener("click", async () => {
        const bufferPh = btn.dataset.bufferPh;
        await calibrateSoilPhBuffer(bufferPh);
      });
    });
  }

  // -----------------------
  // APVPD plant calibration (moved from Sensor Settings into this modal)
  // -----------------------
  let plantPollTimer = null;

  function setPlantCalVisual(state) {
    const lower = String(state || "").toLowerCase();
    let label = "";
    let color = "#ffa500"; // orange default
    if (lower === "calibrated") {
      label = "Calibrated";
      color = "#90ee90";
    } else if (lower === "calibrating" || lower === "started") {
      label = "Calibrating…";
      color = "#ffff00";
    } else {
      label = "Not Calibrated";
      color = "#ffa500";
    }
    if (plantCalStatus) {
      plantCalStatus.textContent = `(${label})`;
    }
    if (plantCalBtn) {
      plantCalBtn.style.background = color;
    }
  }

  async function pollPlantCalibrationStatus() {
    if (!sensorId) return;
    try {
      const resp = await fetch(`/calibration-status?sensor_id=${encodeURIComponent(sensorId)}`);
      const j = await resp.json();
      const state = (j && (j.calibrated || j.state)) || "Not Calibrated";
      setPlantCalVisual(state);

      const lower = String(state || "").toLowerCase();
      const terminal = !(lower === "calibrating" || lower === "started");
      if (!terminal) {
        plantPollTimer = setTimeout(pollPlantCalibrationStatus, 5000);
      } else {
        if (plantCalBtn) plantCalBtn.disabled = false;
        plantPollTimer = null;
      }
    } catch (e) {
      plantPollTimer = setTimeout(pollPlantCalibrationStatus, 7000);
    }
  }

  async function startPlantCalibration() {
    if (!plantCalBtn || !sensorId) return;
    plantCalBtn.disabled = true;
    setPlantCalVisual("started");
    try {
      const resp = await fetch(`/calibrate?sensor_id=${encodeURIComponent(sensorId)}`, {
        method: "POST",
      });
      const result = await resp.json().catch(() => ({ status: "error" }));
      const status = String(result.status || "").toLowerCase();
      if (status === "success" || status === "started") {
        if (plantPollTimer) clearTimeout(plantPollTimer);
        pollPlantCalibrationStatus();
      } else {
        setPlantCalVisual("Not Calibrated");
        plantCalBtn.disabled = false;
      }
    } catch (err) {
      setPlantCalVisual("Not Calibrated");
      plantCalBtn.disabled = false;
    }
  }

  if (isApvpd && plantCalBtn) {
    plantCalBtn.addEventListener("click", startPlantCalibration);
    if (sensorId) {
      // Optional initial poll when the modal opens
      pollPlantCalibrationStatus();
    }
  }

  // -----------------------
  // Helpers for System Calibration Preview + Apply
  // -----------------------
  function scFormatVal(val, digits) {
    if (val === null || val === undefined || Number.isNaN(val)) {
      return "—";
    }
    const d = Number.isFinite(digits) ? digits : 2;
    const num = Number(val);
    if (!Number.isFinite(num)) return "—";
    return num.toFixed(d);
  }

  function scCollectSelectedSensorIds() {
    if (!tableBody) return [];
    const checks = tableBody.querySelectorAll("input.sysCalSensorCheck:checked");
    const ids = [];
    checks.forEach((c) => {
      if (c && c.value) {
        ids.push(c.value);
      }
    });
    return ids;
  }

  function scRenderPreviewStatus(data) {
    if (!statusEl) return;

    if (!data || !Array.isArray(data.sensors) || !data.sensors.length) {
      statusEl.textContent = "No preview data available for the selected window.";
      return;
    }

    const refId = data.reference_id || "?";
    const hours = data.range_hours != null ? data.range_hours : "?";

    const lines = [];
    lines.push(`Reference: ${refId} · Window: last ${hours}h`);
    lines.push("");

    data.sensors.forEach((row) => {
      const sid = row.sensor_id || "?";

      const tempPart =
        `Temp: ${scFormatVal(row.raw_temp, 2)} → ${scFormatVal(row.adj_temp, 2)} ` +
        `(Δ ${scFormatVal(row.temp_offset, 3)}, σ ${scFormatVal(row.temp_sigma, 3)})`;

      const rhPart =
        `RH: ${scFormatVal(row.raw_rh, 2)} → ${scFormatVal(row.adj_rh, 2)} ` +
        `(Δ ${scFormatVal(row.rh_offset, 3)}, σ ${scFormatVal(row.rh_sigma, 3)})`;

      const pairsPart = `pairs=${row.n_pairs != null ? row.n_pairs : "—"}`;

      lines.push(`${sid}: ${tempPart}; ${rhPart}; ${pairsPart}`);
    });

    statusEl.innerHTML = lines.join("<br>");
  }

  function scUpdateTableFromPreview(data) {
    if (!tableBody || !data || !Array.isArray(data.sensors)) return;
    const byId = {};
    data.sensors.forEach((s) => {
      if (s && s.sensor_id) {
        byId[s.sensor_id] = s;
      }
    });

    const rows = tableBody.querySelectorAll("tr[data-sensor-id]");
    rows.forEach((row) => {
      const sid = row.dataset.sensorId;
      if (!sid) return;
      const res = byId[sid];
      const tdTemp = row.querySelector(".sysCalDeltaTemp");
      const tdRh   = row.querySelector(".sysCalDeltaRh");
      if (!res) {
        if (tdTemp) tdTemp.textContent = "–";
        if (tdRh)   tdRh.textContent   = "–";
        return;
      }
      if (tdTemp) tdTemp.textContent = scFormatVal(res.temp_offset, 3);
      if (tdRh)   tdRh.textContent   = scFormatVal(res.rh_offset, 3);
    });
  }

  // -----------------------
  // System calibration: Preview
  // -----------------------
  async function scDoPreview() {
    if (!refSelect || !refSelect.value) {
      alert("Select a reference sensor first.");
      return;
    }

    const referenceId = refSelect.value;
    const rangeHours = (rangeInput && rangeInput.value) ? rangeInput.value : "24";
    const selectedSensors = scCollectSelectedSensorIds();

    if (statusEl) {
      statusEl.textContent = "Computing system calibration preview…";
    }
    if (applyBtn) {
      applyBtn.disabled = true;
    }

    const formData = new FormData();
    formData.append("reference_id", referenceId);
    formData.append("range_hours", String(rangeHours));
    formData.append("sensor_ids", selectedSensors.join(","));

    let resp;
    try {
      resp = await fetch("/system-calibration/preview", {
        method: "POST",
        body: formData,
      });
    } catch (err) {
      console.error("[SystemCal] preview fetch error:", err);
      if (statusEl) {
        statusEl.textContent = "Failed to contact server for system calibration preview.";
      }
      return;
    }

    let data;
    try {
      data = await resp.json();
    } catch (err) {
      console.error("[SystemCal] preview JSON error:", err);
      if (statusEl) {
        statusEl.textContent = "Server returned an invalid preview payload.";
      }
      return;
    }

    // If backend sent ok:false or HTTP error, show the message
    if (!resp.ok || !data || data.ok === false) {
      const msg = (data && data.error) || resp.statusText || "System calibration preview failed.";
      console.error("[SystemCal] preview error:", msg);
      if (statusEl) {
        statusEl.textContent = msg;
      }
      return;
    }

    // data.ok is true, render nicely and remember it for Apply
    scLastPreview = data;
    scRenderPreviewStatus(data);
    scUpdateTableFromPreview(data);
    if (applyBtn) {
      applyBtn.disabled = false;
    }
    console.debug("[SystemCal] preview result", data);
  }

  // -----------------------
  // System calibration: Apply
  // -----------------------
  async function scDoApply() {
    if (!scLastPreview || !Array.isArray(scLastPreview.sensors)) {
      if (statusEl) {
        statusEl.textContent = "Run Preview before applying system calibration.";
      }
      return;
    }

    const selectedIds = scCollectSelectedSensorIds();
    let sensors = scLastPreview.sensors;

    if (selectedIds.length > 0) {
      sensors = sensors.filter((s) => selectedIds.includes(s.sensor_id));
    }

    if (!sensors.length) {
      if (statusEl) {
        statusEl.textContent = "No sensors selected to calibrate.";
      }
      return;
    }

    const body = {
      reference_id: scLastPreview.reference_id || "",
      start_ts: scLastPreview.start_ts,
      end_ts: scLastPreview.end_ts,
      sensors: sensors,
    };

    if (applyBtn) applyBtn.disabled = true;
    if (statusEl) {
      statusEl.textContent = "Applying system calibration…";
    }

    try {
      const resp = await fetch("/system-calibration/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const data = await resp.json().catch(() => ({}));

      if (!resp.ok || (data && data.ok === false)) {
        const msg =
          (data && data.error) ||
          resp.statusText ||
          "System calibration apply failed.";
        console.error("[SystemCal] apply error:", msg);
        if (statusEl) statusEl.textContent = msg;
        return;
      }

      const okCount = Array.isArray(data.applied) ? data.applied.length : 0;
      const failCount = Array.isArray(data.failures) ? data.failures.length : 0;

      let msg = `Applied system calibration to ${okCount} sensor(s).`;
      if (failCount) {
        msg += ` ${failCount} sensor(s) failed.`;
      }

      if (statusEl) {
        statusEl.textContent = msg;
      }

      console.debug("[SystemCal] apply result", data);
    } catch (err) {
      console.error("[SystemCal] apply exception:", err);
      if (statusEl) {
        statusEl.textContent = "Error applying system calibration.";
      }
    } finally {
      if (applyBtn) applyBtn.disabled = false;
    }
  }

  // ---- wire up buttons ----
  if (previewBtn) {
    previewBtn.addEventListener("click", () => {
      void scDoPreview();
    });
  }

  if (applyBtn) {
    applyBtn.addEventListener("click", () => {
      void scDoApply();
    });
  }

  // ---- finally show (parent sets display:none initially) ----
  if (backdrop) {
    backdrop.style.display = "flex";
  }

  return true;
};
