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
  if (form && saveBtn && spinner) {
    form.addEventListener("submit", () => {
      spinner.style.display = "inline-block";
      saveBtn.disabled = true;
    });
  }

  showPane("sensor-settings-pane");
  return true;
};
