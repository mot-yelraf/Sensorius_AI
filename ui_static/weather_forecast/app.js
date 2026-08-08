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

  const moonSurfaceImage = new Image();
  const moonRenders = new Map();

  function renderMoonDisk(canvas, moon) {
    if (!canvas) return;
    moonRenders.set(canvas, moon);
    const context = canvas.getContext("2d");
    if (!context) return;

    const canonicalIllumination = [0, 15, 50, 85, 100, 85, 50, 15];
    const phaseIndex = Number(moon.index ?? canvas.dataset.phaseIndex);
    let illuminationPercent = Number(moon.illumination);
    if (!Number.isFinite(illuminationPercent)
      || (canvas.hasAttribute("data-phase-moon") && phaseIndex > 0 && illuminationPercent === 0)) {
      illuminationPercent = canonicalIllumination[phaseIndex] ?? 0;
    }
    const illumination = Math.max(0, Math.min(100, illuminationPercent)) / 100;
    const angle = Number(moon.bright_limb_angle || 0) * Math.PI / 180;
    const diskRotation = Number(moon.disk_rotation || 0) * Math.PI / 180;
    const rotationCosine = Math.cos(diskRotation);
    const rotationSine = Math.sin(diskRotation);
    const width = canvas.width;
    const height = canvas.height;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.455;
    const lightDepth = 2 * illumination - 1;
    const lightAcross = Math.sqrt(Math.max(0, 1 - lightDepth * lightDepth));
    const lightX = Math.sin(angle) * lightAcross;
    const lightY = Math.cos(angle) * lightAcross;
    const pixels = context.createImageData(width, height);
    let surfacePixels = null;
    if (moonSurfaceImage.complete && moonSurfaceImage.naturalWidth > 0) {
      const surfaceCanvas = document.createElement("canvas");
      surfaceCanvas.width = width;
      surfaceCanvas.height = height;
      const surfaceContext = surfaceCanvas.getContext("2d", {willReadFrequently: true});
      if (surfaceContext) {
        surfaceContext.drawImage(moonSurfaceImage, 0, 0, width, height);
        surfacePixels = surfaceContext.getImageData(0, 0, width, height).data;
      }
    }

    for (let pixelY = 0; pixelY < height; pixelY += 1) {
      for (let pixelX = 0; pixelX < width; pixelX += 1) {
        const x = (pixelX + 0.5 - centerX) / radius;
        const y = (centerY - pixelY - 0.5) / radius;
        const distanceSquared = x * x + y * y;
        if (distanceSquared > 1.025) continue;
        const z = Math.sqrt(Math.max(0, 1 - Math.min(1, distanceSquared)));
        const sunlight = x * lightX + y * lightY + z * lightDepth;
        const terminator = Math.max(0, Math.min(1, (sunlight + 0.018) / 0.036));
        const texture = 0.92 + 0.045 * Math.sin(pixelX * 0.31 + pixelY * 0.17)
          + 0.025 * Math.sin(pixelX * 0.08 - pixelY * 0.23);
        const index = (pixelY * width + pixelX) * 4;
        const textureX = rotationCosine * x - rotationSine * y;
        const textureY = rotationSine * x + rotationCosine * y;
        const sourceX = Math.max(0, Math.min(width - 1, Math.round(centerX + textureX * radius)));
        const sourceY = Math.max(0, Math.min(height - 1, Math.round(centerY - textureY * radius)));
        const sourceIndex = (sourceY * width + sourceX) * 4;
        const sourceRed = surfacePixels ? surfacePixels[sourceIndex] : Math.round(224 * texture);
        const sourceGreen = surfacePixels ? surfacePixels[sourceIndex + 1] : Math.round(211 * texture);
        const sourceBlue = surfacePixels ? surfacePixels[sourceIndex + 2] : Math.round(170 * texture);
        const brightness = 0.035 + terminator * (0.9 + z * 0.065);
        pixels.data[index] = Math.min(255, Math.round(sourceRed * brightness));
        pixels.data[index + 1] = Math.min(255, Math.round(sourceGreen * brightness));
        pixels.data[index + 2] = Math.min(255, Math.round(sourceBlue * brightness + (1 - terminator) * 5));
        pixels.data[index + 3] = distanceSquared <= 1 ? 255 : Math.round((1.025 - distanceSquared) / 0.025 * 255);
      }
    }
    context.putImageData(pixels, 0, 0);
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.strokeStyle = "rgba(255, 240, 198, 0.28)";
    context.lineWidth = 1.5;
    context.stroke();
    canvas.setAttribute("aria-label", `Observer-local view of the ${moon.name}, ${illuminationPercent} percent illuminated`);
    canvas.title = moon.representative_date
      ? `${moon.name} · local view near lunar transit on ${moon.representative_date}`
      : `${moon.name} · local view now`;
  }

  function pairedPhaseCycle(phases) {
    const paired = phases.map((phase) => ({...phase}));
    [[1, 7], [2, 6], [3, 5]].forEach(([waxingIndex, waningIndex]) => {
      const waxing = paired.find((phase) => Number(phase.index) === waxingIndex);
      const waning = paired.find((phase) => Number(phase.index) === waningIndex);
      if (!waxing || !waning) return;
      const waxingAngle = Number(waxing.bright_limb_angle);
      const waningAsWaxing = (Number(waning.bright_limb_angle) + 180) % 360;
      if (!Number.isFinite(waxingAngle) || !Number.isFinite(waningAsWaxing)) return;
      const waxingRadians = waxingAngle * Math.PI / 180;
      const waningRadians = waningAsWaxing * Math.PI / 180;
      const pairedAngle = (Math.atan2(
        Math.sin(waxingRadians) + Math.sin(waningRadians),
        Math.cos(waxingRadians) + Math.cos(waningRadians),
      ) * 180 / Math.PI + 360) % 360;
      waxing.bright_limb_angle = pairedAngle;
      waning.bright_limb_angle = (pairedAngle + 180) % 360;
    });
    return paired;
  }

  moonSurfaceImage.addEventListener("load", () => {
    moonRenders.forEach((moon, canvas) => renderMoonDisk(canvas, moon));
  });
  moonSurfaceImage.src = "/ui_static/weather_forecast/moon-surface.png?v=1";

  function updateDaylightTrack(progressValue) {
    const sunMarker = document.getElementById("daylightSun");
    if (!sunMarker) return;
    const progress = Math.max(0, Math.min(100, Number(progressValue) || 0));
    sunMarker.style.setProperty("--daylight-progress", `${5 + progress * 0.9}%`);
    sunMarker.style.setProperty("--sun-rise", String(Math.sin(Math.PI * progress / 100)));
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
  const initialPhaseCycle = Array.from(document.querySelectorAll("[data-phase-moon]")).map((canvas) => ({
    index: Number(canvas.dataset.phaseIndex),
    illumination: canvas.dataset.illumination,
    bright_limb_angle: canvas.dataset.brightLimbAngle,
    disk_rotation: canvas.dataset.diskRotation,
    name: canvas.getAttribute("aria-label")?.replace("Local view of the ", "") || "Moon phase",
  }));
  pairedPhaseCycle(initialPhaseCycle).forEach((phase) => {
    renderMoonDisk(document.querySelector(`[data-phase-index="${phase.index}"]`), phase);
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
      pairedPhaseCycle(moon.cycle || []).forEach((phase) => {
        renderMoonDisk(document.querySelector(`[data-phase-index="${phase.index}"]`), phase);
      });
      document.getElementById("currentMoonName").textContent = moon.name;
      document.getElementById("currentMoonIllumination").textContent = `${moon.illumination}%`;
      document.getElementById("currentMoonAge").textContent = moon.age_days;
      document.getElementById("moonLocalView").textContent = moon.moon_altitude == null
        ? "Observer-local orientation"
        : `Observer-local orientation · ${moon.moon_altitude}° altitude${moon.moon_altitude < 0 ? " (below horizon)" : ""}`;
      document.getElementById("sunriseTime").textContent = moon.sunrise;
      document.getElementById("sunsetTime").textContent = moon.sunset;
      document.getElementById("mapSunriseTime").textContent = moon.sunrise;
      document.getElementById("mapSunsetTime").textContent = moon.sunset;
      document.getElementById("solarNoonTime").textContent = moon.solar_noon;
      document.getElementById("daylightDuration").textContent = moon.daylight_duration;
      document.getElementById("daylightHours").textContent = moon.daylight_hours ?? "—";
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
