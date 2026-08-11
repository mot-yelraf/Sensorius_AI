(function () {
  const forecastDialog = document.getElementById("forecastDialog");
  document.querySelectorAll("[data-open-forecast]").forEach((button) => {
    button.addEventListener("click", () => forecastDialog?.showModal());
  });
  forecastDialog?.querySelectorAll("[data-close-forecast]").forEach((button) => {
    button.addEventListener("click", () => forecastDialog.close());
  });
  forecastDialog?.addEventListener("click", (event) => {
    if (event.target === forecastDialog) forecastDialog.close();
  });

  const dashboardReturn = document.getElementById("dashboardReturn");
  dashboardReturn?.addEventListener("click", () => dashboardReturn.classList.add("is-loading"));
  window.addEventListener("pageshow", () => dashboardReturn?.classList.remove("is-loading"));

  const renderMoonDisk = window.CaelusMoon?.renderMoonDisk || (() => {});

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
    sunMarker.style.setProperty("--daylight-progress", `${5 + progress * 0.9}%`);
    sunMarker.style.setProperty("--sun-rise", String(Math.sin(Math.PI * progress / 100)));
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

  async function refreshAstronomy() {
    if (document.hidden) return;
    try {
      const response = await fetch("/api/weather-forecast-app/astronomy", {cache: "no-store"});
      if (!response.ok) return;
      const moon = await response.json();
      renderMoonDisk(document.getElementById("currentMoonDisk"), moon);
      updatePhaseStrip("previous", moon.previous_phases || []);
      updatePhaseStrip("upcoming", moon.upcoming_phases || []);
      document.getElementById("currentMoonName").textContent = moon.name;
      document.getElementById("currentMoonIllumination").textContent = `${moon.illumination}%`;
      document.getElementById("currentMoonAge").textContent = moon.age_days;
      document.getElementById("moonLocalView").textContent = moon.moon_altitude == null
        ? "Observer-local orientation"
        : `Observer-local orientation · ${moon.moon_altitude}° altitude${moon.moon_altitude < 0 ? " (below horizon)" : ""}`;
      document.getElementById("sunriseTime").textContent = moon.sunrise;
      document.getElementById("sunsetTime").textContent = moon.sunset;
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

  window.setInterval(refreshAstronomy, 5 * 60 * 1000);
  document.addEventListener("visibilitychange", refreshAstronomy);
})();
