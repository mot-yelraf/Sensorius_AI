(function () {
  const forecastDialog = document.getElementById("forecastDialog");
  forecastDialog?.querySelectorAll("[data-close-forecast]").forEach((button) => {
    button.addEventListener("click", () => forecastDialog.close());
  });
  forecastDialog?.addEventListener("click", (event) => {
    if (event.target === forecastDialog) forecastDialog.close();
  });

  function initializeHourlyCarousels(root = document) {
    root.querySelectorAll("[data-hourly-carousel]").forEach((carousel) => {
      if (carousel.dataset.initialized === "true") return;
      carousel.dataset.initialized = "true";
      const hours = Array.from(carousel.querySelectorAll("[data-hourly-index]"));
      const previousButton = carousel.querySelector("[data-hourly-previous]");
      const nextButton = carousel.querySelector("[data-hourly-next]");
      const status = carousel.parentElement?.querySelector("[data-hourly-status]");
      const pageSize = Number(carousel.dataset.pageSize) || 8;
      let pageStart = 0;

      function showPage() {
        const pageEnd = Math.min(pageStart + pageSize, hours.length);
        hours.forEach((hour, index) => {
          hour.hidden = index < pageStart || index >= pageEnd;
        });
        if (previousButton) previousButton.hidden = pageStart === 0;
        if (nextButton) nextButton.hidden = pageEnd >= hours.length;
        if (status) status.textContent = `Hours ${pageStart + 1}–${pageEnd} of ${hours.length}`;
      }

      previousButton?.addEventListener("click", () => {
        pageStart = Math.max(0, pageStart - 1);
        showPage();
      });
      nextButton?.addEventListener("click", () => {
        pageStart = Math.min(Math.max(0, hours.length - pageSize), pageStart + 1);
        showPage();
      });
      showPage();
    });
  }

  function initializeForecastButtons(root = document) {
    root.querySelectorAll("[data-open-forecast]").forEach((button) => {
      if (button.dataset.initialized === "true") return;
      button.dataset.initialized = "true";
      button.addEventListener("click", () => forecastDialog?.showModal());
    });
  }

  initializeHourlyCarousels();
  initializeForecastButtons();

  const dashboardReturn = document.getElementById("dashboardReturn");
  dashboardReturn?.addEventListener("click", () => dashboardReturn.classList.add("is-loading"));
  window.addEventListener("pageshow", () => dashboardReturn?.classList.remove("is-loading"));

  const caelusThemeView = document.getElementById("caelusThemeView");
  const caelusThemeButtons = Array.from(document.querySelectorAll("[data-caelus-preview-theme]"));
  const caelusThemeCloseButton = document.getElementById("closeCaelusThemeBtn");
  let caelusThemeReturnFocus = null;
  let savedCaelusTheme = Array.from(document.body.classList)
    .find((name) => name.startsWith("theme-"))?.slice(6) || "pollinator";
  let savedCaelusCustomStyle = document.body.getAttribute("style") || "";

  function applyCaelusPreviewTheme(theme) {
    const supported = new Set(["pollinator", "garden", "island", "river", "desert"]);
    const nextTheme = supported.has(theme) ? theme : "pollinator";
    Array.from(document.body.classList)
      .filter((name) => name.startsWith("theme-"))
      .forEach((name) => document.body.classList.remove(name));
    document.body.classList.add(`theme-${nextTheme}`);
    caelusThemeButtons.forEach((button) => {
      const active = button.dataset.caelusPreviewTheme === nextTheme;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function openCaelusThemeView() {
    if (!caelusThemeView || document.body.classList.contains("caelus-theme-preview-mode")) return;
    caelusThemeReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    savedCaelusTheme = Array.from(document.body.classList)
      .find((name) => name.startsWith("theme-"))?.slice(6) || savedCaelusTheme;
    savedCaelusCustomStyle = document.body.getAttribute("style") || "";
    if (savedCaelusTheme === "custom") {
      document.body.removeAttribute("style");
      applyCaelusPreviewTheme("pollinator");
    } else {
      applyCaelusPreviewTheme(savedCaelusTheme);
    }
    caelusThemeView.hidden = false;
    document.body.classList.add("caelus-theme-preview-mode");
    document.querySelector("main.dashboard-shell")?.setAttribute("aria-hidden", "true");
    caelusThemeCloseButton?.focus({preventScroll: true});
  }

  function closeCaelusThemeView() {
    if (!caelusThemeView || !document.body.classList.contains("caelus-theme-preview-mode")) return;
    document.body.classList.remove("caelus-theme-preview-mode");
    caelusThemeView.hidden = true;
    document.querySelector("main.dashboard-shell")?.removeAttribute("aria-hidden");
    if (savedCaelusTheme === "custom") {
      Array.from(document.body.classList)
        .filter((name) => name.startsWith("theme-"))
        .forEach((name) => document.body.classList.remove(name));
      document.body.classList.add("theme-custom");
      if (savedCaelusCustomStyle) document.body.setAttribute("style", savedCaelusCustomStyle);
    } else {
      applyCaelusPreviewTheme(savedCaelusTheme);
    }
    if (caelusThemeReturnFocus?.isConnected) caelusThemeReturnFocus.focus({preventScroll: true});
    caelusThemeReturnFocus = null;
  }

  document.querySelectorAll("[data-open-caelus-theme]").forEach((button) => {
    button.addEventListener("click", openCaelusThemeView);
  });
  caelusThemeButtons.forEach((button) => {
    button.addEventListener("click", () => applyCaelusPreviewTheme(button.dataset.caelusPreviewTheme || "pollinator"));
  });
  caelusThemeCloseButton?.addEventListener("click", closeCaelusThemeView);
  caelusThemeView?.addEventListener("click", (event) => {
    if (event.target === caelusThemeView) closeCaelusThemeView();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("caelus-theme-preview-mode")) {
      event.preventDefault();
      closeCaelusThemeView();
    }
  });

  const renderMoonDisk = window.CaelusMoon?.renderMoonDisk || (() => {});

  function updateMoonViewLabel(moon) {
    const label = document.getElementById("moonLocalView");
    if (!label) return;
    if (window.CaelusMoon?.getViewMode() === "reference") {
      label.textContent = "Reference orientation · lunar north up";
      return;
    }
    const rawAltitude = moon?.moon_altitude;
    const altitude = Number(rawAltitude);
    label.textContent = rawAltitude !== "" && rawAltitude != null && Number.isFinite(altitude)
      ? `Observer-local orientation · ${altitude}° altitude${altitude < 0 ? " (below horizon)" : ""}`
      : "Observer-local orientation";
  }

  function displayReading(value) {
    return value == null ? "—" : String(value);
  }

  function renderCurrentReadings(payload) {
    const panel = document.querySelector("[data-current-readings]");
    if (!panel) return;
    const metrics = Array.isArray(payload.display_metrics) ? payload.display_metrics : [];
    const primary = metrics[0];
    const sensorName = payload.sensor_id || "Sensor not selected";
    panel.querySelector("[data-readings-sensor]").textContent = payload.location || sensorName;
    panel.querySelector("[data-readings-badge]").textContent = payload.ok ? "Live" : "Waiting";
    panel.querySelector("[data-readings-primary-value]").textContent = displayReading(primary?.value);
    panel.querySelector("[data-readings-primary-unit]").textContent = primary?.unit || "";
    panel.querySelector("[data-readings-primary-name]").textContent = primary?.name || "No Display Metrics configured";
    const list = panel.querySelector("[data-readings-list]");
    list.replaceChildren();
    metrics.slice(1).forEach((metric) => {
      const row = document.createElement("div");
      const name = document.createElement("span");
      const value = document.createElement("strong");
      name.textContent = metric.name || "Metric";
      value.textContent = `${displayReading(metric.value)}${metric.value == null || !metric.unit ? "" : ` ${metric.unit}`}`;
      row.append(name, value);
      list.append(row);
    });
    const footer = panel.querySelector("[data-readings-footer]");
    footer.classList.toggle("is-live", Boolean(payload.ok));
    footer.classList.toggle("is-waiting", !payload.ok);
    panel.querySelector("[data-readings-status]").textContent = payload.ok ? "Station reporting" : "Sensor standing by";
    panel.dataset.refreshIntervalSec = String(payload.refresh_interval_sec || 60);
    const subtitle = document.getElementById("stationSubtitle");
    if (subtitle && payload.ok) {
      subtitle.replaceChildren(document.createTextNode(`${sensorName} · Last observation `));
      const time = document.createElement("time");
      time.dateTime = payload.timestamp || "";
      const observed = new Date(payload.timestamp || "");
      time.textContent = Number.isNaN(observed.valueOf())
        ? "just now"
        : observed.toLocaleString([], {year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
      subtitle.append(time);
    }
  }

  let readingsTimer = 0;
  async function refreshCurrentReadings() {
    if (!document.hidden) {
      try {
        const response = await fetch("/api/weather-forecast-app/current-readings", {cache: "no-store"});
        if (response.ok) renderCurrentReadings(await response.json());
      } catch (_error) {
        // Keep the last good reading visible until the next sensor interval.
      }
    }
    const panel = document.querySelector("[data-current-readings]");
    const seconds = Math.max(15, Math.min(3600, Number(panel?.dataset.refreshIntervalSec) || 60));
    window.clearTimeout(readingsTimer);
    readingsTimer = window.setTimeout(refreshCurrentReadings, seconds * 1000);
  }

  let forecastHour = Math.floor(Date.now() / (60 * 60 * 1000));
  let forecastTimer = 0;
  async function refreshHourlyForecast() {
    if (!document.hidden) {
      try {
        const response = await fetch("/weather-forecast?force_refresh=true", {cache: "no-store"});
        if (response.ok) {
          const nextDocument = new DOMParser().parseFromString(await response.text(), "text/html");
          const nextPanel = nextDocument.querySelector(".forecast-panel");
          const currentPanel = document.querySelector(".forecast-panel");
          if (nextPanel && currentPanel) {
            currentPanel.replaceWith(nextPanel);
            initializeHourlyCarousels(nextPanel);
            initializeForecastButtons(nextPanel);
          }
          forecastHour = Math.floor(Date.now() / (60 * 60 * 1000));
        }
      } catch (_error) {
        // Keep the last good hourly forecast visible until the next attempt.
      }
    }
    scheduleHourlyForecast();
  }

  function scheduleHourlyForecast() {
    const hourMs = 60 * 60 * 1000;
    const delay = hourMs - (Date.now() % hourMs);
    window.clearTimeout(forecastTimer);
    forecastTimer = window.setTimeout(refreshHourlyForecast, delay + 50);
  }

  const initialReadingsInterval = Math.max(15, Math.min(3600,
    Number(document.querySelector("[data-current-readings]")?.dataset.refreshIntervalSec) || 60));
  readingsTimer = window.setTimeout(refreshCurrentReadings, initialReadingsInterval * 1000);
  scheduleHourlyForecast();

  function phaseFromCanvas(canvas) {
    return {
      index: Number(canvas.dataset.phaseIndex),
      illumination: canvas.dataset.illumination,
      bright_limb_angle: canvas.dataset.brightLimbAngle,
      disk_rotation: canvas.dataset.diskRotation,
      representative_date: canvas.dataset.representativeDate,
      name: canvas.closest(".lunar-step")?.querySelector("[data-phase-name]")?.textContent || "Moon phase",
    };
  }

  function updatePhaseStrip(period, phases) {
    const steps = document.querySelectorAll(`[data-lunar-period="${period}"] .lunar-step`);
    phases.slice(0, steps.length).forEach((phase, position) => {
      const step = steps[position];
      const canvas = step.querySelector("[data-phase-moon]");
      const name = step.querySelector("[data-phase-name]");
      const date = step.querySelector("[data-phase-date]");
      if (!canvas) return;
      canvas.dataset.phaseIndex = phase.index;
      canvas.dataset.illumination = phase.illumination;
      canvas.dataset.brightLimbAngle = phase.bright_limb_angle;
      canvas.dataset.diskRotation = phase.disk_rotation;
      canvas.dataset.representativeDate = phase.representative_date;
      if (name) name.textContent = phase.name;
      if (date) {
        date.textContent = phase.date_label;
        date.setAttribute("datetime", phase.representative_date);
      }
      renderMoonDisk(canvas, phase);
    });
  }

  function updateDaylightTrack(progressValue) {
    const sunMarker = document.getElementById("daylightSun");
    if (!sunMarker) return;
    const progress = Math.max(0, Math.min(100, Number(progressValue) || 0));
    const horizontalOffset = progress / 50 - 1;
    sunMarker.style.setProperty("--daylight-progress", `${5 + progress * 0.9}%`);
    sunMarker.style.setProperty("--sun-rise", String(Math.sqrt(1 - horizontalOffset ** 2)));
    sunMarker.dataset.daylightProgress = String(progress);
  }

  function formatSolarTime(value) {
    const match = String(value || "").match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return "—";
    const hour24 = Number(match[1]);
    if (!Number.isFinite(hour24) || hour24 > 23) return "—";
    const hour12 = (hour24 % 12) || 12;
    return `${hour12}:${match[2]} ${hour24 < 12 ? "AM" : "PM"}`;
  }

  const initialMoonDisk = document.getElementById("currentMoonDisk");
  if (initialMoonDisk) {
    renderMoonDisk(initialMoonDisk, {
      illumination: initialMoonDisk.dataset.illumination,
      bright_limb_angle: initialMoonDisk.dataset.brightLimbAngle,
      disk_rotation: initialMoonDisk.dataset.diskRotation,
      name: document.getElementById("currentMoonName")?.textContent || "Moon",
    });
    updateMoonViewLabel({moon_altitude: initialMoonDisk.dataset.moonAltitude});
  }
  document.querySelectorAll("[data-phase-moon]").forEach((canvas) => {
    renderMoonDisk(canvas, phaseFromCanvas(canvas));
  });
  updateDaylightTrack(document.getElementById("daylightSun")?.dataset.daylightProgress || 0);

  const windyFrame = document.querySelector("[data-windy-map]");
  const windyResetButton = document.querySelector("[data-reset-windy]");
  const windyInteraction = document.querySelector("[data-windy-interaction]");
  const windyGuard = document.querySelector("[data-windy-guard]");
  if (windyFrame && windyInteraction && windyGuard) {
    const setWindyActive = (active) => {
      windyInteraction.classList.toggle("is-active", active);
      windyFrame.setAttribute("tabindex", active ? "0" : "-1");
    };
    windyGuard.addEventListener("click", () => {
      setWindyActive(true);
      windyFrame.focus();
    });
    windyInteraction.addEventListener("mouseleave", () => setWindyActive(false));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && windyInteraction.classList.contains("is-active")) {
        setWindyActive(false);
        windyGuard.focus();
      }
    });
  }
  if (windyFrame && windyResetButton) {
    windyResetButton.addEventListener("click", () => {
      const mapUrl = windyFrame.getAttribute("src");
      if (!mapUrl) return;
      windyResetButton.disabled = true;
      windyResetButton.textContent = "Resetting…";
      windyFrame.addEventListener("load", () => {
        windyResetButton.disabled = false;
        windyResetButton.textContent = "× Close forecast";
      }, {once: true});
      windyFrame.setAttribute("src", mapUrl);
    });
  }

  function positionLunarEventMarker(markerId, eventAt, startAt, endAt) {
    const marker = document.getElementById(markerId);
    if (!marker) return;
    const eventMs = Date.parse(String(eventAt || ""));
    const startMs = Date.parse(String(startAt || ""));
    const endMs = Date.parse(String(endAt || ""));
    const available = Number.isFinite(eventMs)
      && Number.isFinite(startMs)
      && Number.isFinite(endMs)
      && endMs > startMs
      && eventMs >= startMs
      && eventMs <= endMs;
    marker.classList.toggle("is-unavailable", !available);
    if (!available) return;
    const percent = Math.max(0, Math.min(100, ((eventMs - startMs) / (endMs - startMs)) * 100));
    marker.style.setProperty("--timeline-left", `${percent.toFixed(2)}%`);
  }

  function renderLunarEventTimeline(moon) {
    const startAt = moon?.timeline_start_at || moon?.startAt || "";
    const sunsetAt = moon?.timeline_sunset_at || moon?.sunsetAt || "";
    const endAt = moon?.timeline_end_at || moon?.endAt || "";
    const moonriseAt = moon?.timeline_moonrise_at || moon?.moonriseAt || "";
    const moonsetAt = moon?.timeline_moonset_at || moon?.moonsetAt || "";
    const setText = (id, value) => {
      if (value == null) return;
      const element = document.getElementById(id);
      if (element) element.textContent = String(value || "—");
    };
    setText("sunriseTime", moon?.sunrise);
    setText("sunsetTime", moon?.sunset);
    setText("nextSunriseTime", moon?.next_sunrise);
    setText("lunarMoonriseTime", moon?.timeline_moonrise);
    setText("lunarMoonsetTime", moon?.timeline_moonset);
    positionLunarEventMarker("forecastSunsetMarker", sunsetAt, startAt, endAt);
    positionLunarEventMarker("forecastMoonriseMarker", moonriseAt, startAt, endAt);
    positionLunarEventMarker("forecastMoonsetMarker", moonsetAt, startAt, endAt);
  }

  async function refreshAstronomy() {
    if (document.hidden) return;
    try {
      const response = await fetch("/api/weather-forecast-app/astronomy", {cache: "no-store"});
      if (!response.ok) return;
      const moon = await response.json();
      const currentMoonDisk = document.getElementById("currentMoonDisk");
      if (currentMoonDisk) currentMoonDisk.dataset.moonAltitude = moon.moon_altitude == null ? "" : String(moon.moon_altitude);
      renderMoonDisk(currentMoonDisk, moon);
      updatePhaseStrip("previous", moon.previous_phases || []);
      updatePhaseStrip("upcoming", moon.upcoming_phases || []);
      document.getElementById("currentMoonName").textContent = moon.name;
      document.getElementById("currentMoonIllumination").textContent = `${moon.illumination}%`;
      document.getElementById("currentMoonAge").textContent = moon.age_days;
      updateMoonViewLabel(moon);
      renderLunarEventTimeline(moon);
      document.getElementById("mapSunriseTime").textContent = moon.sunrise_display || formatSolarTime(moon.sunrise);
      document.getElementById("mapSunsetTime").textContent = moon.sunset_display || formatSolarTime(moon.sunset);
      document.getElementById("solarNoonTime").textContent = moon.solar_noon_display || formatSolarTime(moon.solar_noon);
      document.getElementById("daylightDuration").textContent = moon.daylight_duration;
      document.getElementById("nextSeasonLabel").textContent = moon.next_season_label ?? "—";
      const nextSeasonDate = document.getElementById("nextSeasonDate");
      nextSeasonDate.textContent = moon.next_season_date ?? "—";
      nextSeasonDate.setAttribute("datetime", moon.next_season_at || "");
      const eclipseList = document.getElementById("nextEclipseList");
      eclipseList.replaceChildren();
      const eclipses = Array.isArray(moon.next_eclipses) ? moon.next_eclipses.slice(0, 3) : [];
      if (!eclipses.length) {
        const emptyItem = document.createElement("li");
        emptyItem.className = "eclipse-empty";
        emptyItem.textContent = "No visible eclipses for the next 12 months";
        eclipseList.append(emptyItem);
      } else {
        eclipses.forEach((eclipse) => {
          const item = document.createElement("li");
          const kind = document.createElement("strong");
          const date = document.createElement("time");
          kind.textContent = eclipse.kind || "Eclipse";
          date.textContent = eclipse.date || "—";
          date.setAttribute("datetime", eclipse.at || "");
          item.append(kind, date);
          eclipseList.append(item);
        });
      }
      document.getElementById("northPoleDaylight").textContent = moon.north_pole_daylight ?? "—";
      document.getElementById("southPoleDaylight").textContent = moon.south_pole_daylight ?? "—";
      document.getElementById("sunState").textContent = moon.sun_is_up ? "Sun above horizon" : "Sun below horizon";
      updateDaylightTrack(moon.daylight_progress);
      document.getElementById("lunarUpdated").textContent = `Updated ${moon.updated_at.slice(11, 16)} UTC`;
    } catch (_error) {
      return;
    }
  }

  renderLunarEventTimeline(document.getElementById("lunarEventTimeline")?.dataset || {});
  window.addEventListener("sensorius:moon-view-change", () => {
    updateMoonViewLabel({moon_altitude: document.getElementById("currentMoonDisk")?.dataset.moonAltitude});
  });
  window.setInterval(refreshAstronomy, 5 * 60 * 1000);
  document.addEventListener("visibilitychange", () => {
    refreshAstronomy();
    if (document.hidden) return;
    void refreshCurrentReadings();
    if (forecastHour !== Math.floor(Date.now() / (60 * 60 * 1000))) {
      window.clearTimeout(forecastTimer);
      void refreshHourlyForecast();
    }
  });
})();
