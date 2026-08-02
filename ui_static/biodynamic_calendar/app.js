const bootstrapElement = document.getElementById("bd-calendar-bootstrap");
const initialPlantings = JSON.parse(bootstrapElement?.textContent || "[]");
const state = {
  month: "",
  payload: null,
  rangePayload: null,
  plantings: Array.isArray(initialPlantings) ? initialPlantings : [],
  selectedDate: "",
  followCurrentMonth: true,
  selectionPinned: false,
  rangeCache: Object.create(null),
  summaryCache: Object.create(null),
  summaryRequests: Object.create(null),
  calendarRequestId: 0,
  rangeRequestId: 0,
  summaryRequestId: 0,
};
const STATUS_REFRESH_MS = 60 * 1000;
const SUN_REDRAW_MS = 60 * 1000;
const SUN_GRAPH_PAD = { top: 12, right: 18, bottom: 12, left: 24 };

function monthKeyFromDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function dateKeyFromDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function dateKeyInTimezone(d, timezoneName) {
  if (!timezoneName) return dateKeyFromDate(d);
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezoneName,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(d);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    if (values.year && values.month && values.day) {
      return `${values.year}-${values.month}-${values.day}`;
    }
  } catch (err) {
    // Fall through to the browser-local date if the timezone name is not supported.
  }
  return dateKeyFromDate(d);
}

function currentMonthKey() {
  const astro = (state.payload || {}).astro || {};
  return dateKeyInTimezone(new Date(), astro.tz || "").slice(0, 7);
}

function shiftMonth(monthKey, delta) {
  const [year, month] = String(monthKey).split("-").map(Number);
  const d = new Date(year, month - 1 + delta, 1);
  return monthKeyFromDate(d);
}

function clearPerformanceCaches({ ranges = true, summaries = true } = {}) {
  if (ranges) state.rangeCache = Object.create(null);
  if (summaries) {
    state.summaryCache = Object.create(null);
    state.summaryRequests = Object.create(null);
  }
}

function cacheKeyExists(cache, key) {
  return Object.prototype.hasOwnProperty.call(cache || {}, key);
}

function toMinutes(hhmm) {
  const m = String(hhmm || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  return Math.max(0, Math.min(1440, (parseInt(m[1], 10) * 60) + parseInt(m[2], 10)));
}

function textOnHex(hex) {
  const raw = String(hex || "").trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw)) return "#27313a";
  const toLinear = (pair) => {
    const channel = parseInt(pair, 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4);
  };
  const lum = (0.2126 * toLinear(raw.slice(0, 2))) + (0.7152 * toLinear(raw.slice(2, 4))) + (0.0722 * toLinear(raw.slice(4, 6)));
  const contrast = (a, b) => {
    const hi = Math.max(a, b);
    const lo = Math.min(a, b);
    return (hi + 0.05) / (lo + 0.05);
  };
  const darkLum = 0.028;
  return contrast(lum, darkLum) >= contrast(lum, 1) ? "#27313a" : "#fff";
}

function dayTextColor(day) {
  return textOnHex(day?.dominant_accent || day?.dominant_color || "#fff");
}

function dayBackground(day) {
  const fallback = String(day?.dominant_accent || "#fff");
  const segments = Array.isArray(day?.segments) ? day.segments : [];
  if (!segments.length) return fallback;
  const stops = [];
  const dividerStops = [];
  for (const seg of segments) {
    let startMin = toMinutes(seg?.start);
    let endMin = toMinutes(seg?.end);
    if (!Number.isFinite(startMin)) startMin = 0;
    if (!Number.isFinite(endMin)) endMin = 1440;
    if (endMin <= startMin) endMin = 1440;
    const color = String(seg?.accent || fallback);
    const startPct = Math.max(0, Math.min(100, (startMin / 1440) * 100));
    const endPct = Math.max(0, Math.min(100, (endMin / 1440) * 100));
    stops.push(`${color} ${startPct.toFixed(2)}%`, `${color} ${endPct.toFixed(2)}%`);
    if (startPct > 0 && startPct < 100) {
      const lineEnd = Math.min(100, startPct + 0.75);
      dividerStops.push(`transparent ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${startPct.toFixed(2)}%`, `rgba(39,49,58,.14) ${lineEnd.toFixed(2)}%`, `transparent ${lineEnd.toFixed(2)}%`);
    }
  }
  const base = `linear-gradient(90deg, ${stops.join(", ")})`;
  if (!dividerStops.length) return base;
  return `linear-gradient(90deg, ${dividerStops.join(", ")}), ${base}`;
}

function formatTime(value) {
  const m = String(value || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return "--";
  const hh = parseInt(m[1], 10);
  const mm = m[2];
  const suffix = hh < 12 ? "AM" : "PM";
  return `${(hh % 12) || 12}:${mm} ${suffix}`;
}

function formatIsoDate(value) {
  const d = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!d) return "--";
  return new Date(`${value}T00:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
}

function sunGraphAxisPercent(minute) {
  const canvas = document.getElementById("sunGraph");
  const rect = canvas?.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect?.width || canvas?.clientWidth || canvas?.width || 520));
  const innerW = Math.max(1, w - SUN_GRAPH_PAD.left - SUN_GRAPH_PAD.right);
  const clampedMinute = Math.max(0, Math.min(1440, Number(minute)));
  const x = SUN_GRAPH_PAD.left + ((clampedMinute / 1440) * innerW);
  return Math.max(0, Math.min(100, (x / w) * 100));
}

function setPositionEvent(markerId, statId, value, fallbackMinute) {
  const marker = document.getElementById(markerId);
  const stat = document.getElementById(statId);
  const minute = toMinutes(value);
  const hasTime = Number.isFinite(minute);
  const axisMinute = hasTime ? minute : fallbackMinute;
  const axisPct = sunGraphAxisPercent(axisMinute);
  if (stat) stat.textContent = formatTime(value);
  if (!marker) return;
  marker.style.setProperty("--x", `${axisPct.toFixed(2)}%`);
  marker.style.setProperty("--tx", axisPct < 9 ? "0%" : (axisPct > 91 ? "-100%" : "-50%"));
  marker.classList.toggle("is-unavailable", !hasTime);
  const label = marker.querySelector(".position-event-text > span")?.textContent || "Event";
  marker.setAttribute("aria-label", `${label} ${hasTime && stat ? stat.textContent : "unavailable"}`);
}

function updateSunMoonPositionTimes(astro) {
  const moonRiseForAxis = astro?.moon_rise_today || astro?.moon_rise || "";
  const moonSetForAxis = astro?.moon_set_today || astro?.moon_set || "";
  setPositionEvent("sunPositionRiseMarker", "sunriseStat", astro?.sunrise || "", 360);
  setPositionEvent("sunPositionNoonMarker", "sunNoonStat", astro?.sun_noon || "", 720);
  setPositionEvent("sunPositionSetMarker", "sunsetStat", astro?.sunset || "", 1080);
  setPositionEvent("moonPositionRiseMarker", "moonAxisRiseStat", moonRiseForAxis, 360);
  setPositionEvent("moonPositionSetMarker", "moonAxisSetStat", moonSetForAxis, 1080);
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch] || ch));
}

function loadingMarkup(label) {
  return `<span class="loading-inline" role="status" aria-live="polite" aria-label="${esc(label)}"><span class="loading-spinner" aria-hidden="true"></span></span>`;
}

function setCalendarBusy(busy) {
  const calendarEl = document.getElementById("calendar");
  const summaryLine = document.getElementById("summaryLine");
  if (calendarEl) {
    calendarEl.setAttribute("aria-busy", busy ? "true" : "false");
    calendarEl.classList.toggle("is-loading", busy);
    if (busy) calendarEl.innerHTML = `<div class="loading-block">${loadingMarkup("Loading calendar")}</div>`;
  }
  if (summaryLine && busy) {
    summaryLine.innerHTML = loadingMarkup("Loading calendar");
  }
}

function setRangeBusy(busy) {
  const rangeEl = document.getElementById("rangeCalendar");
  const rangeStatus = document.getElementById("rangeStatus");
  if (rangeEl) {
    rangeEl.setAttribute("aria-busy", busy ? "true" : "false");
    rangeEl.classList.toggle("is-loading", busy);
  }
  if (rangeStatus) {
    if (busy) rangeStatus.innerHTML = loadingMarkup("Loading planning range");
    else if (rangeStatus.querySelector(".loading-inline")) rangeStatus.textContent = "";
  }
}

function setSummaryBusy(busy) {
  const summaryEl = document.getElementById("dailySummary");
  if (!summaryEl) return;
  summaryEl.setAttribute("aria-busy", busy ? "true" : "false");
  summaryEl.classList.toggle("is-loading", busy);
  if (busy) summaryEl.innerHTML = `<span class="summary-loading">${loadingMarkup("Loading BD hints")}</span>`;
}

function dateToDayNumber(value) {
  const m = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return NaN;
  return Math.floor(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 86400000);
}

function plantingDisplayName(planting) {
  const name = String(planting?.name || "Unnamed planting").trim();
  const variety = String(planting?.variety || "").trim();
  return variety && !name.toLowerCase().includes(variety.toLowerCase()) ? `${name} (${variety})` : name;
}

function sortedPlantings() {
  return [...(Array.isArray(state.plantings) ? state.plantings : [])].sort((a, b) => {
    const dateCmp = String(a.start_date || "").localeCompare(String(b.start_date || ""));
    return dateCmp || plantingDisplayName(a).localeCompare(plantingDisplayName(b));
  });
}

function plantingMarkersForDate(dateIso) {
  const dayNum = dateToDayNumber(dateIso);
  const markers = { start: false, harvest: false, active: false, labels: [] };
  if (!Number.isFinite(dayNum)) return markers;
  for (const planting of sortedPlantings()) {
    const startNum = dateToDayNumber(planting.start_date);
    const harvestNum = dateToDayNumber(planting.expected_harvest_date);
    const name = plantingDisplayName(planting);
    if (Number.isFinite(startNum) && startNum === dayNum) {
      markers.start = true;
      markers.labels.push(`Start ${name}`);
    }
    if (Number.isFinite(harvestNum) && harvestNum === dayNum) {
      markers.harvest = true;
      markers.labels.push(`Harvest ${name}`);
    }
    if (Number.isFinite(startNum) && startNum <= dayNum && (!Number.isFinite(harvestNum) || dayNum <= harvestNum)) {
      markers.active = true;
    }
  }
  return markers;
}

function resetPlantingForm() {
  const form = document.getElementById("plantingForm");
  if (!form) return;
  form.reset();
  form.elements.id.value = "";
  if (state.selectedDate) form.elements.start_date.value = state.selectedDate;
}

function fillPlantingForm(planting) {
  const form = document.getElementById("plantingForm");
  if (!form || !planting) return;
  const editor = document.getElementById("plantingEditor");
  if (editor) {
    editor.open = true;
    const summary = editor.querySelector(":scope > summary");
    if (summary) summary.textContent = `Edit ${plantingDisplayName(planting)}`;
  }
  for (const key of ["id", "name", "variety", "plant_type", "plant_part", "start_method", "start_date", "expected_harvest_date", "days_to_maturity", "location", "attributes"]) {
    if (form.elements[key]) form.elements[key].value = planting[key] == null ? "" : String(planting[key]);
  }
  form.scrollIntoView({ block: "nearest" });
}

function renderPlantings() {
  const listEl = document.getElementById("plantingList");
  if (!listEl) return;
  const plantings = sortedPlantings();
  const selectedDayNumber = dateToDayNumber(state.selectedDate);
  const relevant = plantings.filter((planting) => {
    const start = dateToDayNumber(planting.start_date);
    const harvest = dateToDayNumber(planting.expected_harvest_date);
    return Number.isFinite(selectedDayNumber) && Number.isFinite(start) && start <= selectedDayNumber && (!Number.isFinite(harvest) || selectedDayNumber <= harvest);
  });
  const plantingItem = (planting) => {
    const harvest = planting.expected_harvest_date ? `Harvest ${planting.expected_harvest_date}` : "Harvest unset";
    const focus = planting.plant_part || "Auto";
    const method = planting.start_method === "transplant" ? "Transplant" : "Seed";
    return `
      <div class="planting-item">
        <div>
          <strong>${esc(plantingDisplayName(planting))}</strong>
          <span>${esc(method)} ${esc(planting.start_date || "--")} | ${esc(harvest)} | ${esc(focus)}</span>
        </div>
        <div class="planting-item-actions">
          <button type="button" data-edit-planting="${esc(planting.id)}">Edit</button>
          <button type="button" class="secondary-btn" data-delete-planting="${esc(planting.id)}">Delete</button>
        </div>
      </div>
    `;
  };
  const relevantHtml = relevant.length ? relevant.map(plantingItem).join("") : `<div class="empty-list">No active plantings for this day.</div>`;
  const listHtml = plantings.length ? plantings.map(plantingItem).join("") : `<div class="empty-list">No plantings configured.</div>`;
  listEl.innerHTML = `
    <div class="planting-saved">
      <div class="planting-subhead">Selected Day</div>
      <div class="planting-scroll">${relevantHtml}</div>
    </div>
    <div class="planting-saved">
      <div class="planting-subhead">All Saved Plantings</div>
      <div class="planting-scroll">
        ${listHtml}
      </div>
    </div>
  `;
}

function renderAstro(astro) {
  if (!astro || !astro.ok) {
    renderCosmicAttributes(null);
    return;
  }
  renderCosmicAttributes(astro.cosmic_attributes || null);
}

function formatCosmicDateTime(value) {
  const parsed = new Date(String(value || ""));
  if (Number.isNaN(parsed.getTime())) return "--";
  return parsed.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatDaylightMinutes(value) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  return `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, "0")}m`;
}

function renderCosmicAttributes(cosmic) {
  const moonTarget = document.getElementById("cosmicAttributes");
  const planetTarget = document.getElementById("planetaryAttributes");
  if (!moonTarget || !planetTarget) return;
  if (!cosmic || typeof cosmic !== "object" || !Object.keys(cosmic).length) {
    moonTarget.innerHTML = '<div class="cosmic-empty">Moon attributes unavailable.</div>';
    planetTarget.innerHTML = '<div class="cosmic-empty">Planetary information unavailable.</div>';
    return;
  }

  const aspects = Array.isArray(cosmic.planetary_aspects) ? cosmic.planetary_aspects : [];
  const aspectLines = aspects.length
    ? aspects.map((item) => `<div class="cosmic-line"><strong>${esc(item.bodies || "--")}</strong> ${esc(item.aspect || "")} · ${esc(item.orb_deg)}° orb</div>`).join("")
    : '<div class="cosmic-line">No major aspect within 3°.</div>';
  const zodiac = Array.isArray(cosmic.planet_zodiac) ? cosmic.planet_zodiac : [];
  const zodiacLines = zodiac.length
    ? zodiac.map((item) => `<div class="cosmic-line"><strong>${esc(item.body || "--")}</strong><span>${esc(item.sign || "--")}</span></div>`).join("")
    : '<div class="cosmic-line">Planet zodiac information unavailable.</div>';

  const direction = cosmic.moon_direction_window || {};
  const distance = cosmic.moon_distance || {};
  const distanceEvents = Array.isArray(distance.events) ? distance.events : [];
  const distanceEventLines = distanceEvents.map((item) => `<div class="cosmic-line">${esc(item.kind || "Event")} ${formatCosmicDateTime(item.at)} · ${Number(item.distance_km || 0).toLocaleString()} km</div>`).join("");

  const eclipses = Array.isArray(cosmic.eclipses) ? cosmic.eclipses : [];
  const eclipseLines = eclipses.length
    ? eclipses.map((item) => `<div class="cosmic-line"><strong>${esc(item.kind || "Eclipse")}</strong> · ${formatCosmicDateTime(item.at)}</div>`).join("")
    : '<div class="cosmic-line">No lunar eclipse in the next year.</div>';

  moonTarget.innerHTML = `
    <section class="cosmic-group">
      <h3>Moon Direction Window</h3>
      <div class="cosmic-line"><strong>${esc(String(direction.direction || "--").replace(/^./, (letter) => letter.toUpperCase()))}</strong></div>
      <div class="cosmic-line">${formatCosmicDateTime(direction.start)} to ${formatCosmicDateTime(direction.end)}</div>
    </section>
    <section class="cosmic-group">
      <h3>Moon Distance / Declination</h3>
      <div class="cosmic-line"><strong>${Number(distance.km || 0).toLocaleString()} km</strong> · ${esc(distance.trend || "--")}</div>
      <div class="cosmic-line">Declination ${Number(distance.declination_deg || 0).toFixed(1)}°</div>
      ${distanceEventLines}
    </section>
    <section class="cosmic-group">
      <h3>Eclipses</h3>
      ${eclipseLines}
    </section>
  `;

  planetTarget.innerHTML = `
    <section class="cosmic-group">
      <h3>Current Major Aspects</h3>
      ${aspectLines}
    </section>
    <section class="cosmic-group">
      <h3>Planet Zodiac</h3>
      <div class="planet-zodiac-list">${zodiacLines}</div>
    </section>
  `;
}

function getMoonViewMode() {
  const refBtn = document.getElementById("moonViewReference");
  return refBtn && refBtn.classList.contains("active") ? "reference" : "local";
}

function setMoonViewMode(mode) {
  const localBtn = document.getElementById("moonViewLocal");
  const refBtn = document.getElementById("moonViewReference");
  const isReference = mode === "reference";
  if (localBtn) {
    localBtn.classList.toggle("active", !isReference);
    localBtn.setAttribute("aria-pressed", !isReference ? "true" : "false");
  }
  if (refBtn) {
    refBtn.classList.toggle("active", isReference);
    refBtn.setAttribute("aria-pressed", isReference ? "true" : "false");
  }
}

function currentMinutesForAstro(astro) {
  const baseMinutes = Number(astro?.current_minutes);
  const timestampMs = Date.parse(String(astro?.timestamp || ""));
  if (Number.isFinite(baseMinutes) && Number.isFinite(timestampMs)) {
    const elapsedMinutes = (Date.now() - timestampMs) / 60000;
    const wrapped = (baseMinutes + elapsedMinutes) % 1440;
    return wrapped < 0 ? wrapped + 1440 : wrapped;
  }
  if (Number.isFinite(baseMinutes)) {
    return Math.max(0, Math.min(1440, baseMinutes));
  }
  const now = new Date();
  return (now.getHours() * 60) + now.getMinutes() + (now.getSeconds() / 60);
}

function drawSunGraph(astro) {
  const canvas = document.getElementById("sunGraph");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width || canvas.clientWidth || canvas.width));
  const h = Math.max(1, Math.round(rect.height || canvas.clientHeight || canvas.height));
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const bufferW = Math.round(w * dpr);
  const bufferH = Math.round(h * dpr);
  if (canvas.width !== bufferW || canvas.height !== bufferH) {
    canvas.width = bufferW;
    canvas.height = bufferH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!astro || !astro.ok || !Array.isArray(astro.sun_points) || !astro.sun_points.length) {
    ctx.fillStyle = "#fff8e8";
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "#7b7a73";
    ctx.font = "600 16px Avenir Next";
    ctx.fillText("Sun/Moon data unavailable", 24, h / 2);
    return;
  }

  const pad = SUN_GRAPH_PAD;
  const innerW = w - pad.left - pad.right;
  const pointMinute = (point) => {
    const raw = Number(point?.m);
    return Number.isFinite(raw) ? Math.max(0, Math.min(1440, raw)) : toMinutes(point?.t);
  };
  const pointList = (items) => (Array.isArray(items) ? items : [])
    .map((point) => ({ m: pointMinute(point), e: Number(point?.e) }))
    .filter((point) => Number.isFinite(point.m) && Number.isFinite(point.e))
    .sort((a, b) => a.m - b.m);
  const sunPoints = pointList(astro.sun_points);
  const moonPoints = pointList(astro.moon_points);
  const allElevs = sunPoints.concat(moonPoints).map((point) => point.e).filter((value) => Number.isFinite(value));
  const maxElev = Math.max(20, ...allElevs.filter((value) => value > 0));
  const minElev = Math.min(-18, ...allElevs.filter((value) => value < 0));
  const graphTop = pad.top + 2;
  const graphBottom = h - pad.bottom - 2;
  const graphH = Math.max(1, graphBottom - graphTop);
  const elevMax = maxElev + 1;
  const elevMin = minElev - 1;
  const elevRange = Math.max(1, elevMax - elevMin);
  const sinusoidalScale = (ratio) => 0.5 - (0.5 * Math.cos(Math.PI * Math.max(0, Math.min(1, ratio))));
  const xForMin = (minute) => pad.left + ((Math.max(0, Math.min(1440, minute)) / 1440) * innerW);
  const yForElev = (elevation) => {
    if (!Number.isFinite(elevation)) return graphBottom;
    const clamped = Math.max(elevMin, Math.min(elevMax, elevation));
    return graphBottom - (sinusoidalScale((clamped - elevMin) / elevRange) * graphH);
  };
  const yBase = yForElev(0);
  ctx.fillStyle = "#dff1ff";
  ctx.fillRect(0, 0, w, Math.max(1, yBase));
  ctx.fillStyle = "#071322";
  ctx.fillRect(0, yBase, w, Math.max(1, h - yBase));
  ctx.strokeStyle = "rgba(39,49,58,0.22)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, yBase);
  ctx.lineTo(w - pad.right, yBase);
  ctx.stroke();
  const interpElev = (points, minute) => {
    if (!Array.isArray(points) || !points.length || !Number.isFinite(minute)) return NaN;
    if (minute <= points[0].m) return points[0].e;
    for (let idx = 1; idx < points.length; idx++) {
      if (minute <= points[idx].m) {
        const prev = points[idx - 1];
        const next = points[idx];
        const span = Math.max(1, next.m - prev.m);
        const ratio = Math.max(0, Math.min(1, (minute - prev.m) / span));
        return prev.e + ((next.e - prev.e) * ratio);
      }
    }
    return points[points.length - 1].e;
  };
  const drawPath = (points, color, width, dash = []) => {
    if (!Array.isArray(points) || points.length < 2) return false;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.setLineDash(dash);
    const xy = points.map((point) => ({ x: xForMin(point.m), y: yForElev(point.e) }));
    ctx.beginPath();
    ctx.moveTo(xy[0].x, xy[0].y);
    for (let idx = 0; idx < xy.length - 1; idx++) {
      const p0 = xy[Math.max(0, idx - 1)];
      const p1 = xy[idx];
      const p2 = xy[idx + 1];
      const p3 = xy[Math.min(xy.length - 1, idx + 2)];
      const cp1x = p1.x + ((p2.x - p0.x) / 6);
      const cp1y = p1.y + ((p2.y - p0.y) / 6);
      const cp2x = p2.x - ((p3.x - p1.x) / 6);
      const cp2y = p2.y - ((p3.y - p1.y) / 6);
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
    ctx.stroke();
    ctx.restore();
    return true;
  };

  drawPath(sunPoints, "#f3d34a", 3);
  drawPath(moonPoints, "#69bdf2", 2.25);

  ctx.save();
  ctx.font = "700 12px Avenir Next, sans-serif";
  ctx.textBaseline = "top";
  ctx.fillStyle = "#8d7115";
  ctx.fillText("Sun", pad.left + 8, pad.top + 6);
  ctx.fillStyle = "#2e78a9";
  ctx.fillText("Moon", pad.left + 50, pad.top + 6);
  ctx.restore();

  const currentMinutes = currentMinutesForAstro(astro);
  const currentX = xForMin(currentMinutes);
  const currentSunElev = interpElev(sunPoints, currentMinutes);
  if (Number.isFinite(currentSunElev)) {
    const currentY = yForElev(currentSunElev);
    const glow = ctx.createRadialGradient(currentX, currentY, 0, currentX, currentY, 16);
    glow.addColorStop(0, "rgba(255, 220, 116, 0.72)");
    glow.addColorStop(1, "rgba(255, 220, 116, 0)");
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(currentX, currentY, 16, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#ffd34d";
    ctx.strokeStyle = "#db7f1f";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(currentX, currentY, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
  const currentMoonElev = interpElev(moonPoints, currentMinutes);
  if (Number.isFinite(currentMoonElev)) {
    const currentMoonY = yForElev(currentMoonElev);
    ctx.fillStyle = "#fff8df";
    ctx.strokeStyle = "#d1b94c";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(currentX, currentMoonY, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }

  ctx.textAlign = "start";
  const moonDec = Number(astro.moon_declination);
  const source = String(astro.moon_position_source || "").trim();
  if (Number.isFinite(moonDec)) {
    canvas.title = `Moon declination ${moonDec.toFixed(1)} deg${source ? ` (${source})` : ""}`;
  } else {
    canvas.removeAttribute("title");
  }
}

function sunMoon29Label(day) {
  const direct = String(day?.label || "").trim();
  if (direct) return direct;
  const raw = String(day?.date || "").trim();
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return raw || "--";
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[Math.max(0, Math.min(11, parseInt(m[2], 10) - 1))]}${parseInt(m[3], 10)}`;
}

function drawSunMoon29Day(astro) {
  const canvas = document.getElementById("sunMoon29Canvas");
  const meta = document.getElementById("sunMoon29Meta");
  if (!canvas || !meta) return;
  const ctx = canvas.getContext("2d");
  const cw = canvas.width;
  const ch = canvas.height;
  ctx.clearRect(0, 0, cw, ch);
  const days = Array.isArray(astro?.position_29d) ? astro.position_29d : [];
  const pad = { left: 34, right: 14, top: 12, bottom: 14 };
  const graphW = Math.max(1, cw - pad.left - pad.right);
  const contentH = Math.max(1, ch - pad.top - pad.bottom);
  const graphH = Math.max(84, Math.round(contentH * 0.68));
  const labelY = pad.top + graphH + 8;
  const moonY = Math.min(ch - pad.bottom - 10, labelY + 28);
  ctx.fillStyle = "#fff9d6";
  ctx.fillRect(0, 0, cw, ch);

  const fromSeries = (items, dayIdx) => (Array.isArray(items) ? items : []).map((point) => {
    const minute = Number(Array.isArray(point) ? point[0] : point?.m);
    const elevation = Number(Array.isArray(point) ? point[1] : point?.e);
    if (!Number.isFinite(minute) || !Number.isFinite(elevation)) return null;
    return { x: (dayIdx * 1440) + Math.max(0, Math.min(1440, minute)), e: elevation };
  }).filter(Boolean);
  const sunPoints = [];
  const moonPoints = [];
  days.forEach((day, dayIdx) => {
    sunPoints.push(...fromSeries(day?.sun, dayIdx));
    moonPoints.push(...fromSeries(day?.moon, dayIdx));
  });
  const allElevs = sunPoints.concat(moonPoints).map((point) => point.e).filter((value) => Number.isFinite(value));
  if (!days.length || !allElevs.length) {
    meta.textContent = "29 day position data unavailable";
    ctx.fillStyle = "#7b7a73";
    ctx.font = "700 18px Avenir Next, sans-serif";
    ctx.fillText("29 day position data unavailable", pad.left, ch / 2);
    return;
  }
  const totalMin = Math.max(1440, days.length * 1440);
  const maxElev = Math.max(20, ...allElevs.filter((value) => value > 0));
  const minElev = Math.min(-18, ...allElevs.filter((value) => value < 0));
  const graphTop = pad.top + 2;
  const graphBottom = pad.top + graphH - 2;
  const graphPlotH = Math.max(1, graphBottom - graphTop);
  const elevMax = maxElev + 1;
  const elevMin = minElev - 1;
  const elevRange = Math.max(1, elevMax - elevMin);
  const sinusoidalScale = (ratio) => 0.5 - (0.5 * Math.cos(Math.PI * Math.max(0, Math.min(1, ratio))));
  const xForMin = (minute) => pad.left + ((Math.max(0, Math.min(totalMin, minute)) / totalMin) * graphW);
  const yForElev = (elevation) => {
    if (!Number.isFinite(elevation)) return graphBottom;
    const clamped = Math.max(elevMin, Math.min(elevMax, elevation));
    return graphBottom - (sinusoidalScale((clamped - elevMin) / elevRange) * graphPlotH);
  };
  const yBase = yForElev(0);
  ctx.fillStyle = "#dff1ff";
  ctx.fillRect(0, pad.top, cw, Math.max(1, yBase - pad.top));
  ctx.fillStyle = "#071322";
  ctx.fillRect(0, yBase, cw, Math.max(1, (pad.top + graphH) - yBase));
  ctx.strokeStyle = "#8fa4b3";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, yBase);
  ctx.lineTo(cw - pad.right, yBase);
  ctx.stroke();
  const drawTinyMoonPhase = (day, cx, cy) => {
    const rawPhase = Number(day?.moon_phase_value);
    if (!Number.isFinite(rawPhase)) return;
    const phase = ((rawPhase % 28) + 28) % 28;
    const phaseAngle = (2 * Math.PI * phase) / 28;
    const limbStrength = Math.abs(Math.sin(phaseAngle));
    const sx = limbStrength;
    const sz = -Math.cos(phaseAngle);
    const r = 7;
    const size = 18;
    const x = size / 2;
    const y = size / 2;
    const rawVisibleAngle = Number(day?.moon_visible_angle);
    const lat = Number(astro?.lat);
    const rotationDeg = Number.isFinite(rawVisibleAngle) ? rawVisibleAngle : (Number.isFinite(lat) && lat < 0 ? -60 : 60);
    const phaseCanvas = document.createElement("canvas");
    phaseCanvas.width = size;
    phaseCanvas.height = size;
    const phaseCtx = phaseCanvas.getContext("2d");
    const image = phaseCtx.createImageData(size, size);
    const pix = image.data;
    for (let py = 0; py < size; py++) {
      for (let px = 0; px < size; px++) {
        const dx = (px + 0.5 - x) / r;
        const dy = (py + 0.5 - y) / r;
        const rr = (dx * dx) + (dy * dy);
        const off = ((py * size) + px) * 4;
        if (rr > 1) {
          pix[off + 3] = 0;
          continue;
        }
        const dz = Math.sqrt(Math.max(0, 1 - rr));
        const dot = (dx * sx) + (dz * sz);
        const edge = Math.max(-1, Math.min(1, dot / 0.06));
        const blend = (edge + 1) * 0.5;
        const litMix = Math.pow(blend, 0.82);
        const rim = Math.pow(Math.max(0, dz), 0.65);
        const darkR = 74, darkG = 78, darkB = 86;
        const litR = 244, litG = 242, litB = 234;
        pix[off + 0] = Math.round(Math.max(0, Math.min(255, darkR + ((litR - darkR) * litMix) + (rim * 8))));
        pix[off + 1] = Math.round(Math.max(0, Math.min(255, darkG + ((litG - darkG) * litMix) + (rim * 7))));
        pix[off + 2] = Math.round(Math.max(0, Math.min(255, darkB + ((litB - darkB) * litMix) + (rim * 5))));
        pix[off + 3] = 255;
      }
    }
    phaseCtx.putImageData(image, 0, 0);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate((rotationDeg * Math.PI) / 180);
    ctx.drawImage(phaseCanvas, -x, -y);
    ctx.restore();
    ctx.strokeStyle = "#58524a";
    ctx.lineWidth = 0.75;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  };

  ctx.save();
  ctx.strokeStyle = "rgba(39, 49, 58, 0.22)";
  ctx.fillStyle = "#27313a";
  ctx.font = "10px Avenir Next, sans-serif";
  ctx.textBaseline = "top";
  for (let idx = 0; idx <= days.length; idx++) {
    const x = xForMin(idx * 1440);
    ctx.beginPath();
    ctx.moveTo(x, pad.top);
    ctx.lineTo(x, ch - pad.bottom);
    ctx.stroke();
    if (idx > 0 && idx < days.length && idx % 2 === 0) {
      ctx.textAlign = "center";
      ctx.fillText(sunMoon29Label(days[idx]), x, labelY);
    }
  }
  ctx.restore();

  days.forEach((day, dayIdx) => drawTinyMoonPhase(day, xForMin((dayIdx * 1440) + 720), moonY));
  const drawPath = (points, color, width) => {
    if (!Array.isArray(points) || points.length < 2) return;
    const sorted = points.slice().sort((a, b) => a.x - b.x);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(xForMin(sorted[0].x), yForElev(sorted[0].e));
    const xy = sorted.map((point) => ({ x: xForMin(point.x), y: yForElev(point.e) }));
    for (let idx = 0; idx < xy.length - 1; idx++) {
      const p0 = xy[Math.max(0, idx - 1)];
      const p1 = xy[idx];
      const p2 = xy[idx + 1];
      const p3 = xy[Math.min(xy.length - 1, idx + 2)];
      const cp1x = p1.x + ((p2.x - p0.x) / 6);
      const cp1y = p1.y + ((p2.y - p0.y) / 6);
      const cp2x = p2.x - ((p3.x - p1.x) / 6);
      const cp2y = p2.y - ((p3.y - p1.y) / 6);
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
    ctx.stroke();
    ctx.restore();
  };
  drawPath(sunPoints, "#f3d34a", 1.8);
  drawPath(moonPoints, "#69bdf2", 1.55);
  ctx.save();
  ctx.font = "700 12px Avenir Next, sans-serif";
  ctx.textBaseline = "top";
  ctx.textAlign = "right";
  ctx.fillStyle = "#8d7115";
  ctx.fillText("Sun", cw - pad.right - 52, pad.top + 5);
  ctx.fillStyle = "#2e78a9";
  ctx.fillText("Moon", cw - pad.right - 6, pad.top + 5);
  ctx.restore();
  meta.textContent = `${sunMoon29Label(days[0])} - ${sunMoon29Label(days[days.length - 1])}`;
}

function sunMoon29IsOpen() {
  return document.getElementById("sunMoon29Overlay")?.classList.contains("open") === true;
}

function openSunMoon29Day() {
  const overlay = document.getElementById("sunMoon29Overlay");
  const card = document.getElementById("sunMoon29Card");
  if (!overlay) return;
  overlay.classList.add("open");
  overlay.setAttribute("aria-hidden", "false");
  drawSunMoon29Day((state.payload || {}).astro || null);
  try {
    if (card) card.focus({ preventScroll: true });
  } catch (err) {
    if (card) card.focus();
  }
}

function isSunMoon29Trigger(target) {
  if (!(target instanceof Element)) return false;
  if (target.closest("[data-moon-view]")) return false;
  return Boolean(target.closest("#sunMoonPositionPanel") || target.closest("#moonPhasePanel"));
}

function closeSunMoon29Day() {
  const overlay = document.getElementById("sunMoon29Overlay");
  if (!overlay) return;
  overlay.classList.remove("open");
  overlay.setAttribute("aria-hidden", "true");
}

function drawMoonPhase(astro) {
  const canvas = document.getElementById("moonPhaseCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (!astro || !astro.ok || typeof astro.moon_phase_value !== "number") {
    ctx.fillStyle = "#7b7a73";
    ctx.font = "600 14px Avenir Next";
    ctx.fillText("Moon data unavailable", 14, h / 2);
    return;
  }

  const phase = ((astro.moon_phase_value % 28) + 28) % 28;
  const phaseAngle = (2 * Math.PI * phase) / 28;
  const illum = 0.5 * (1 - Math.cos(phaseAngle));
  const lat = Number(astro.lat || 0);
  const hemisphereFlip = lat < 0 ? -1 : 1;
  const rawVisibleAngle = astro ? astro.moon_visible_angle : null;
  const visibleAngle = typeof rawVisibleAngle === "number" && Number.isFinite(rawVisibleAngle) ? rawVisibleAngle : NaN;
  const rawReferenceAngle = astro ? astro.moon_reference_angle : null;
  const referenceAngle = typeof rawReferenceAngle === "number" && Number.isFinite(rawReferenceAngle) ? rawReferenceAngle : NaN;
  const isReferenceMode = getMoonViewMode() === "reference";
  const useVisibleAngle = !isReferenceMode && Number.isFinite(visibleAngle);
  const useReferenceAngle = isReferenceMode && Number.isFinite(referenceAngle);
  const limbStrength = Math.abs(Math.sin(phaseAngle));
  const sourceAngleDeg = useVisibleAngle ? visibleAngle : (isReferenceMode ? 0 : (hemisphereFlip < 0 ? 60 : -60));
  const rotationDeg = sourceAngleDeg;
  const sx = limbStrength;
  const sz = -Math.cos(phaseAngle);
  const r = Math.min(w, h) / 2 - 1;
  const cx = w / 2;
  const cy = h / 2;
  const phaseCanvas = document.createElement("canvas");
  phaseCanvas.width = w;
  phaseCanvas.height = h;
  const phaseCtx = phaseCanvas.getContext("2d");
  const image = phaseCtx.createImageData(w, h);
  const pix = image.data;

  for (let py = 0; py < h; py++) {
    for (let px = 0; px < w; px++) {
      const dx = (px + 0.5 - cx) / r;
      const dy = (py + 0.5 - cy) / r;
      const rr = dx * dx + dy * dy;
      const off = (py * w + px) * 4;
      if (rr > 1) {
        pix[off + 3] = 0;
        continue;
      }
      const dz = Math.sqrt(Math.max(0, 1 - rr));
      const dot = (dx * sx) + (dz * sz);
      const edge = Math.max(-1, Math.min(1, dot / 0.06));
      const blend = (edge + 1) * 0.5;
      const litMix = Math.pow(blend, 0.82);
      const rim = Math.pow(Math.max(0, dz), 0.65);
      const darkR = 74, darkG = 78, darkB = 86;
      const litR = 244, litG = 242, litB = 234;
      const bodyR = darkR + ((litR - darkR) * litMix);
      const bodyG = darkG + ((litG - darkG) * litMix);
      const bodyB = darkB + ((litB - darkB) * litMix);
      const rimBoost = 0.12 + (0.10 * rim);
      pix[off + 0] = Math.round(Math.max(0, Math.min(255, bodyR + (litMix * 10) + (rimBoost * 18))));
      pix[off + 1] = Math.round(Math.max(0, Math.min(255, bodyG + (litMix * 9) + (rimBoost * 16))));
      pix[off + 2] = Math.round(Math.max(0, Math.min(255, bodyB + (litMix * 7) + (rimBoost * 10))));
      pix[off + 3] = 255;
    }
  }
  phaseCtx.putImageData(image, 0, 0);

  const maria = [
    {x:-0.28,y:-0.24,r:0.22,a:0.15},
    {x:0.06,y:-0.1,r:0.17,a:0.12},
    {x:-0.12,y:0.18,r:0.2,a:0.11},
    {x:0.26,y:0.12,r:0.12,a:0.1},
    {x:0.18,y:-0.34,r:0.1,a:0.1},
  ];
  phaseCtx.save();
  phaseCtx.translate(cx, cy);
  phaseCtx.beginPath();
  phaseCtx.arc(0, 0, r, 0, Math.PI * 2);
  phaseCtx.clip();
  for (const m of maria) {
    phaseCtx.fillStyle = `rgba(88, 92, 100, ${m.a})`;
    phaseCtx.beginPath();
    phaseCtx.arc(m.x * r, m.y * r, m.r * r, 0, Math.PI * 2);
    phaseCtx.fill();
  }
  phaseCtx.restore();

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate((rotationDeg * Math.PI) / 180);
  ctx.drawImage(phaseCanvas, -cx, -cy);
  ctx.restore();
  ctx.strokeStyle = "#58524a";
  ctx.lineWidth = 1.15;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.stroke();

  if (!Number.isFinite(Number(astro.moon_lit_pct))) {
    document.getElementById("moonLitStat").textContent = `${Math.round(illum * 100)}%`;
  }
  document.getElementById("moonPhaseLabel").textContent = `${astro.moon_phase_label || "Moon"} | ${isReferenceMode ? "Reference diagram" : "Local sky view"}`;
}

async function loadCalendar(monthKey = "", preferredDate = "", options = {}) {
  const requestId = ++state.calendarRequestId;
  const requestMonth = String(monthKey || "").trim();
  if (options.showLoading !== false) setCalendarBusy(true);
  try {
    const resp = await fetch(requestMonth ? `/api/biodynamic-calendar-app/calendar?month=${encodeURIComponent(requestMonth)}` : "/api/biodynamic-calendar-app/calendar");
    const payload = await resp.json();
    if (requestId !== state.calendarRequestId) return;
    const previousMonth = state.month;
    state.payload = payload;
    if (Array.isArray(payload.plantings)) state.plantings = payload.plantings;
    state.month = monthKeyForPayload(payload) || requestMonth || currentMonthKey();
    if (preferredDate) state.selectedDate = preferredDate;
    render({
      preserveNoteDraft: options.preserveNoteDraft === true,
      refreshSummary: options.refreshSummary !== false,
    });
    const shouldRefreshRange = options.refreshRange !== false;
    if (shouldRefreshRange || (state.followCurrentMonth && previousMonth && previousMonth !== state.month)) {
      void loadCalendarRange(state.month);
    }
  } catch (err) {
    if (requestId !== state.calendarRequestId) return;
    state.payload = { ok: false, reason: "Calendar unavailable.", calendar: [] };
    render({ refreshSummary: false });
  } finally {
    if (requestId === state.calendarRequestId) setCalendarBusy(false);
  }
}

async function fetchDailySummary(dayIso) {
  if (!dayIso) {
    return "";
  }
  if (cacheKeyExists(state.summaryCache, dayIso)) {
    return state.summaryCache[dayIso];
  }
  if (cacheKeyExists(state.summaryRequests, dayIso)) {
    return state.summaryRequests[dayIso];
  }
  const request = fetch(`/api/biodynamic-calendar-app/daily-summary?day=${encodeURIComponent(dayIso)}`)
    .then((resp) => resp.json().then((payload) => ({ resp, payload })))
    .then(({ resp, payload }) => {
      const summary = resp.ok && payload && payload.ok ? String(payload.summary || "") : "Daily summary unavailable.";
      state.summaryCache[dayIso] = summary;
      return summary;
    })
    .catch(() => "Daily summary unavailable.")
    .finally(() => {
      delete state.summaryRequests[dayIso];
    });
  state.summaryRequests[dayIso] = request;
  return request;
}

async function loadDailySummary(dayIso) {
  const summaryEl = document.getElementById("dailySummary");
  if (!dayIso) {
    summaryEl.textContent = "";
    setSummaryBusy(false);
    return;
  }
  if (cacheKeyExists(state.summaryCache, dayIso)) {
    setSummaryBusy(false);
    renderDailyGuidance(summaryEl, state.summaryCache[dayIso]);
    return;
  }
  const requestId = ++state.summaryRequestId;
  setSummaryBusy(true);
  const summary = await fetchDailySummary(dayIso);
  if (requestId !== state.summaryRequestId || state.selectedDate !== dayIso) return;
  renderDailyGuidance(summaryEl, summary);
  setSummaryBusy(false);
}

function renderDailyGuidance(container, summary) {
  const lines = String(summary || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const actions = [];
  const warnings = [];
  const plants = [];
  const technical = [];
  for (const line of lines) {
    if (/^(Suggestion|Timing|Observation):/i.test(line)) actions.push(line.replace(/^[^:]+:\s*/, ""));
    else if (/^(Caution|Plant Condition):/i.test(line)) warnings.push(line.replace(/^[^:]+:\s*/, ""));
    else if (/^Plant (Plan|Attributes):/i.test(line)) plants.push(line.replace(/^[^:]+:\s*/, ""));
    else if (line !== "Biodynamic Hints") technical.push(line);
  }
  const group = (title, items, className = "") => items.length ? `<section class="guidance-group ${className}"><h3>${esc(title)}</h3><ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></section>` : "";
  const astralDetails = technical.map((line) => {
    if (["Selected Day", "Biodynamic Influences", "Astral Notes"].includes(line)) {
      return `<strong class="astral-section-title">${esc(line)}</strong>`;
    }
    return `<span>${esc(line)}</span>`;
  }).join("");
  container.innerHTML = [
    group("Best actions", actions),
    group("Cautions", warnings, "warning"),
    group("Plant guidance", plants, "plants"),
    technical.length ? `<details class="technical-details"><summary>Astral details</summary><div class="astral-details-body">${astralDetails}</div></details>` : "",
  ].join("") || `<div class="empty-list">Daily guidance unavailable.</div>`;
}

function renderSelectedFacts(day) {
  const factsEl = document.getElementById("selectedFacts");
  if (!factsEl) return;
  if (!day) {
    factsEl.innerHTML = "";
    return;
  }
  const events = Array.isArray(day.lunar_events) ? day.lunar_events : [];
  const eventText = events.length
    ? events.map((ev) => `${ev.label || ev.type || "Event"} ${ev.start || "--"}-${ev.end || "--"}`).join(" | ")
    : "None";
  const flags = [
    day.lunar_node ? "Lunar node" : "",
    day.perigee ? "Perigee" : "",
    day.apogee ? "Apogee" : "",
  ].filter(Boolean).join(" | ") || "None";
  const segments = (Array.isArray(day.segments) ? day.segments : []).map((seg) => {
    const label = seg.off_label || seg.sign || "--";
    const part = seg.plant_part && seg.plant_part !== "Rest" ? ` / ${seg.plant_part}` : "";
    return `<li><span>${esc(seg.start || "--")} to ${esc(seg.end || "--")}</span><strong>${esc(label)}${esc(part)}</strong></li>`;
  }).join("");
  factsEl.innerHTML = `
    <div><span>Zodiac Moon</span><strong>${esc(day.dominant_sign || "--")}</strong></div>
    <div><span>Element / Part</span><strong>${esc(day.dominant_element || "--")} / ${esc(day.dominant_plant_part || "--")}</strong></div>
    <div><span>Moon Direction</span><strong>${esc(day.moon_direction || "--")}</strong></div>
    <div><span>Flags</span><strong>${esc(flags)}</strong></div>
    <div class="wide"><span>Lunar Events</span><strong>${esc(eventText)}</strong></div>
    <ul class="wide segment-list">${segments}</ul>
  `;
}

async function selectDay(dayIso, options = {}) {
  state.selectedDate = dayIso;
  const payload = state.payload || {};
  const days = Array.isArray(payload.calendar) ? payload.calendar : [];
  const notes = payload.notes || {};
  const day = days.find((row) => row && row.date === dayIso);
  document.querySelectorAll(".bio-day").forEach((el) => {
    el.classList.toggle("selected", el.dataset.date === dayIso);
  });
  document.querySelectorAll(".mini-day").forEach((el) => {
    el.classList.toggle("selected", el.dataset.date === dayIso);
  });
  document.getElementById("selectedDate").textContent = day ? new Date(`${day.date}T00:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: "numeric" }) : "Select a day";
  document.getElementById("selectedMeta").textContent = day ? `${day.dominant_sign || "--"} / ${day.dominant_plant_part || "--"}` : "";
  renderSelectedFacts(day);
  const noteInput = document.getElementById("noteInput");
  const preserveDraft = options.preserveNoteDraft === true && document.activeElement === noteInput && noteInput.dataset.date === dayIso;
  if (!preserveDraft) {
    noteInput.value = day ? String(notes[day.date] || "") : "";
  }
  noteInput.dataset.date = day ? day.date : "";
  renderPlantings();
  if (options.refreshSummary !== false) {
    await loadDailySummary(dayIso);
  }
}

function monthKeyForPayload(monthPayload) {
  const days = Array.isArray(monthPayload?.calendar) ? monthPayload.calendar : [];
  const inMonth = days.find((day) => day && day.in_month && day.date);
  return inMonth ? String(inMonth.date).slice(0, 7) : "";
}

function cachedRangePayloads() {
  return [state.rangePayload, ...Object.values(state.rangeCache || {})].filter(Boolean);
}

function cachedMonthPayload(monthKey) {
  const target = String(monthKey || "");
  if (!target) return null;
  for (const payload of cachedRangePayloads()) {
    const months = Array.isArray(payload?.months) ? payload.months : [];
    const match = months.find((monthPayload) => monthKeyForPayload(monthPayload) === target);
    if (match) return match;
  }
  return null;
}

function promoteCachedMonth(monthKey, preferredDate = "", options = {}) {
  const cachedMonth = cachedMonthPayload(monthKey);
  if (!cachedMonth) return false;
  const currentPayload = state.payload || {};
  const currentRange = state.rangePayload || {};
  const plantings = Array.isArray(state.plantings)
    ? state.plantings
    : (Array.isArray(currentPayload.plantings) ? currentPayload.plantings : currentRange.plantings);
  state.payload = {
    ...cachedMonth,
    astro: currentPayload.astro || null,
    notes: currentRange.notes || currentPayload.notes || {},
    plantings,
    location: currentRange.location || currentPayload.location || {},
  };
  state.plantings = Array.isArray(plantings) ? plantings : [];
  state.month = monthKey;
  state.selectedDate = preferredDate || "";
  render({
    preserveNoteDraft: options.preserveNoteDraft === true,
    refreshSummary: options.refreshSummary !== false,
  });
  if (options.refreshRange !== false) {
    void loadCalendarRange(monthKey);
  }
  return true;
}

async function loadCalendarRange(monthKey, options = {}) {
  const rangeStatus = document.getElementById("rangeStatus");
  if (rangeStatus) rangeStatus.textContent = "";
  const requestedMonth = monthKey || monthKeyFromDate(new Date());
  const cached = !options.force && state.rangeCache[requestedMonth];
  if (cached) {
    setRangeBusy(false);
    state.rangePayload = cached;
    renderRange();
    return;
  }
  const requestId = ++state.rangeRequestId;
  setRangeBusy(true);
  try {
    const resp = await fetch(`/api/biodynamic-calendar-app/calendar-range?start=${encodeURIComponent(requestedMonth)}&months=13`);
    const payload = await resp.json();
    if (requestId !== state.rangeRequestId) return;
    if (state.month !== requestedMonth) return;
    state.rangePayload = payload;
    if (payload && payload.ok && !payload.warming) {
      state.rangeCache[requestedMonth] = payload;
    }
    renderRange();
    if (payload && payload.warming) {
      window.setTimeout(() => {
        if (state.month === requestedMonth) void loadCalendarRange(requestedMonth, { force: true });
      }, 5000);
    }
  } catch (err) {
    if (requestId !== state.rangeRequestId) return;
    if (rangeStatus) rangeStatus.textContent = "Planning range unavailable.";
  } finally {
    if (requestId === state.rangeRequestId) setRangeBusy(false);
  }
}

function renderRange() {
  const rangeEl = document.getElementById("rangeCalendar");
  const rangeStatus = document.getElementById("rangeStatus");
  const payload = state.rangePayload || {};
  const months = Array.isArray(payload.months) ? payload.months : [];
  if (!rangeEl) return;
  if (!payload.ok) {
    rangeEl.innerHTML = "";
    if (rangeStatus) rangeStatus.textContent = payload.reason || "";
    return;
  }
  if (rangeStatus) rangeStatus.textContent = "";
  const futureMonths = months.slice(1, 13);
  rangeEl.innerHTML = futureMonths.map((monthPayload) => {
    const monthKey = monthKeyForPayload(monthPayload);
    const weekdays = (Array.isArray(monthPayload.weekday_labels) ? monthPayload.weekday_labels : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]).map((label) => `<div class="mini-weekday">${esc(String(label).slice(0, 1))}</div>`).join("");
    const days = Array.isArray(monthPayload.calendar) ? monthPayload.calendar : [];
    const grid = days.map((day) => {
      const classes = ["mini-day"];
      const plantingMarkers = plantingMarkersForDate(day.date);
      if (!day.in_month) classes.push("out");
      if (day.is_today) classes.push("today");
      if (plantingMarkers.start) classes.push("plant-start");
      if (plantingMarkers.harvest) classes.push("plant-harvest");
      if (state.selectedDate === day.date) classes.push("selected");
      const style = `background:${dayBackground(day)};border-color:${esc(day.dominant_color || "#d7d0bf")};color:${dayTextColor(day)};`;
      const markerTitle = plantingMarkers.labels.length ? ` title="${esc(plantingMarkers.labels.join(" | "))}"` : "";
      return `<button type="button" class="${classes.join(" ")}" data-date="${esc(day.date)}" data-month="${esc(monthKey)}" style="${style}"${markerTitle}>${esc(day.day)}</button>`;
    }).join("");
    return `<section class="range-month"><h3>${esc(monthPayload.month_label || monthKey || "--")}</h3><div class="mini-calendar">${weekdays}${grid}</div></section>`;
  }).join("");
  rangeEl.querySelectorAll(".mini-day").forEach((btn) => {
    btn.addEventListener("click", () => {
      const month = btn.getAttribute("data-month") || state.month;
      const dateIso = btn.getAttribute("data-date") || "";
      state.followCurrentMonth = month === currentMonthKey();
      state.selectionPinned = true;
      if (!promoteCachedMonth(month, dateIso)) {
        void loadCalendar(month, dateIso);
      }
    });
  });
}

function render(options = {}) {
  const payload = state.payload || {};
  const calendarEl = document.getElementById("calendar");
  const monthLabel = document.getElementById("monthLabel");
  const summaryLine = document.getElementById("summaryLine");
  const weekdays = Array.isArray(payload.weekday_labels) ? payload.weekday_labels : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const days = Array.isArray(payload.calendar) ? payload.calendar : [];
  monthLabel.textContent = payload.month_label || state.month || "--";
  const headerDate = document.getElementById("headerDate");
  const headerLocation = document.getElementById("headerLocation");
  const astro = payload.astro || {};
  if (headerDate) {
    const dateOptions = { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" };
    if (astro.tz) dateOptions.timeZone = astro.tz;
    headerDate.textContent = new Date().toLocaleDateString([], dateOptions);
  }
  if (headerLocation) {
    const lat = Number(astro.latitude ?? astro.lat);
    const lon = Number(astro.longitude ?? astro.lon);
    headerLocation.textContent = Number.isFinite(lat) && Number.isFinite(lon) ? `${lat.toFixed(3)}, ${lon.toFixed(3)}` : (astro.tz || "Location unavailable");
  }
  renderAstro(payload.astro || null);
  if (!payload.ok) {
    summaryLine.textContent = payload.reason === "config_missing" ? "Set latitude, longitude, and timezone to load the calendar." : (payload.reason || "Calendar unavailable.");
    calendarEl.innerHTML = "";
    selectDay("", { refreshSummary: false });
    return;
  }
  const cur = payload.current || {};
  summaryLine.textContent = cur.sign ? `${cur.sign} Moon | ${cur.element || "--"} / ${cur.plant_part || "--"} | ${cur.window_start_hm || "--"} to ${cur.window_end_hm || "--"}` : "Unavailable";
  let html = weekdays.map((label) => `<div class="weekday">${label}</div>`).join("");
  html += days.map((day) => {
    const classes = ["bio-day"];
    const plantingMarkers = plantingMarkersForDate(day.date);
    if (!day.in_month) classes.push("out");
    if (day.is_today) classes.push("today");
    if ((payload.notes || {})[day.date]) classes.push("noted");
    if (plantingMarkers.start) classes.push("plant-start");
    if (plantingMarkers.harvest) classes.push("plant-harvest");
    if (plantingMarkers.active) classes.push("plant-active");
    if (state.selectedDate === day.date) classes.push("selected");
    const partLabel = String(day.dominant_plant_part || "").trim() || "--";
    const style = `background:${dayBackground(day)};border-color:${day.dominant_color || "#d7d0bf"};color:${dayTextColor(day)};`;
    const markerTitle = plantingMarkers.labels.length ? ` | ${plantingMarkers.labels.join(" | ")}` : "";
    return `<button type="button" class="${classes.join(" ")}" data-date="${esc(day.date)}" style="${style}" title="${esc(day.date || "")} ${esc(day.dominant_sign || "")} ${esc(day.dominant_plant_part || "")}${esc(markerTitle)}"><span class="day-number">${esc(day.day)}</span><span class="day-meta">${esc(partLabel)}</span></button>`;
  }).join("");
  calendarEl.innerHTML = html;
  calendarEl.querySelectorAll(".bio-day").forEach((btn) => btn.addEventListener("click", () => {
    state.selectionPinned = true;
    void selectDay(btn.dataset.date || "");
  }));
  const defaultDay = days.find((d) => d && d.is_today && d.in_month) || days.find((d) => d && d.in_month) || null;
  if (!state.selectedDate || !days.some((d) => d && d.date === state.selectedDate)) {
    state.selectedDate = defaultDay ? defaultDay.date : "";
  }
  void selectDay(state.selectedDate, {
    preserveNoteDraft: options.preserveNoteDraft === true,
    refreshSummary: options.refreshSummary !== false,
  });
}

function printMonthDays(payload) {
  return (Array.isArray(payload?.calendar) ? payload.calendar : [])
    .filter((day) => day && day.in_month && day.date);
}

function formatPrintDate(value, options = {}) {
  const m = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return value || "--";
  const date = new Date(`${value}T00:00:00`);
  return date.toLocaleDateString([], {
    weekday: options.weekday || "short",
    month: "short",
    day: "numeric",
    year: options.year || undefined,
  });
}

function fallbackHintForDay(day) {
  const segments = Array.isArray(day?.segments) ? day.segments : [];
  const segmentText = segments.map((seg) => {
    const part = seg.plant_part && seg.plant_part !== "Rest" ? ` / ${seg.plant_part}` : "";
    return `${seg.start || "--"} to ${seg.end || "--"}: ${seg.off_label || seg.sign || "--"}${part}`;
  }).join("\n");
  const flags = [
    day?.lunar_node ? "Lunar node" : "",
    day?.perigee ? "Perigee" : "",
    day?.apogee ? "Apogee" : "",
  ].filter(Boolean).join(", ");
  const lines = [
    `${day?.dominant_sign || "--"} Moon | ${day?.dominant_element || "--"} / ${day?.dominant_plant_part || "--"}`,
    day?.moon_direction ? `Moon direction: ${day.moon_direction}` : "",
    flags ? `Flags: ${flags}` : "",
    segmentText,
  ].filter(Boolean);
  return lines.join("\n");
}

function compactPrintHint(summary, day) {
  const text = String(summary || "").replace(/^Biodynamic Hints\s*/i, "").trim();
  return text || fallbackHintForDay(day);
}

async function monthlyPrintHints(payload) {
  const days = printMonthDays(payload);
  return Promise.all(days.map(async (day) => {
    try {
      const summary = await fetchDailySummary(day.date);
      return { day, summary: compactPrintHint(summary, day) };
    } catch (err) {
      // Fall back to calendar payload details when the summary endpoint is unavailable.
    }
    return { day, summary: fallbackHintForDay(day) };
  }));
}

function immediatePrintHints(payload) {
  return printMonthDays(payload).map((day) => ({
    day,
    summary: cacheKeyExists(state.summaryCache, day.date)
      ? compactPrintHint(state.summaryCache[day.date], day)
      : fallbackHintForDay(day),
  }));
}

function buildPrintCalendar(payload) {
  const weekdays = Array.isArray(payload?.weekday_labels) ? payload.weekday_labels : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const days = Array.isArray(payload?.calendar) ? payload.calendar : [];
  const weekdayHtml = weekdays.map((label) => `<div class="print-weekday">${esc(label)}</div>`).join("");
  const dayHtml = days.map((day) => {
    const classes = ["print-day"];
    if (!day.in_month) classes.push("out");
    if (day.is_today) classes.push("today");
    if (state.selectedDate === day.date) classes.push("selected");
    const style = `background:${dayBackground(day)};border-color:${esc(day.dominant_color || "#d7d0bf")};color:${dayTextColor(day)};`;
    const partLabel = String(day.dominant_plant_part || "").trim() || "--";
    return `
      <div class="${classes.join(" ")}" style="${style}">
        <span class="print-day-number">${esc(day.day)}</span>
        <span class="print-day-meta">${esc(partLabel)}</span>
      </div>
    `;
  }).join("");
  return `<div class="print-calendar-grid">${weekdayHtml}${dayHtml}</div>`;
}

function monthBounds(days) {
  const nums = days.map((day) => dateToDayNumber(day.date)).filter((num) => Number.isFinite(num));
  return {
    start: nums.length ? Math.min(...nums) : NaN,
    end: nums.length ? Math.max(...nums) : NaN,
  };
}

function plantingRelevantToMonth(planting, bounds) {
  if (!Number.isFinite(bounds.start) || !Number.isFinite(bounds.end)) return true;
  const startNum = dateToDayNumber(planting?.start_date);
  const harvestNum = dateToDayNumber(planting?.expected_harvest_date);
  if (!Number.isFinite(startNum) && !Number.isFinite(harvestNum)) return true;
  const effectiveStart = Number.isFinite(startNum) ? startNum : harvestNum;
  const effectiveEnd = Number.isFinite(harvestNum) ? harvestNum : startNum;
  return effectiveStart <= bounds.end && effectiveEnd >= bounds.start;
}

function buildPrintPlantings(days) {
  const bounds = monthBounds(days);
  const plantings = sortedPlantings().filter((planting) => plantingRelevantToMonth(planting, bounds));
  if (!plantings.length) return `<p class="print-empty">No plantings overlap this month.</p>`;
  return `
    <div class="print-planting-list">
      ${plantings.map((planting) => {
        const details = [
          planting.start_method ? `Start: ${planting.start_method}` : "",
          planting.start_date ? `Start date: ${planting.start_date}` : "",
          planting.expected_harvest_date ? `Harvest: ${planting.expected_harvest_date}` : "",
          planting.days_to_maturity ? `Days to maturity: ${planting.days_to_maturity}` : "",
          planting.location ? `Location: ${planting.location}` : "",
          planting.plant_part ? `Focus: ${planting.plant_part}` : "",
          planting.plant_type ? `Type: ${planting.plant_type}` : "",
        ].filter(Boolean);
        const attributes = String(planting.attributes || "").trim();
        return `
          <article class="print-planting">
            <h3>${esc(plantingDisplayName(planting))}</h3>
            <p>${details.map(esc).join(" | ") || "No planting details configured."}</p>
            ${attributes ? `<p>${esc(attributes)}</p>` : ""}
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function printNotesForMonth(days) {
  const notes = { ...((state.payload || {}).notes || {}) };
  const noteInput = document.getElementById("noteInput");
  if (noteInput?.dataset?.date) {
    notes[noteInput.dataset.date] = noteInput.value;
  }
  return days
    .map((day) => ({ day, note: String(notes[day.date] || "").trim() }))
    .filter((row) => row.note);
}

function buildPrintNotes(days) {
  const notes = printNotesForMonth(days);
  if (!notes.length) return `<p class="print-empty">No notes for this month.</p>`;
  return `
    <div class="print-note-list">
      ${notes.map(({ day, note }) => `
        <article class="print-note">
          <h3>${esc(formatPrintDate(day.date, { year: "numeric" }))}</h3>
          <p>${esc(note)}</p>
        </article>
      `).join("")}
    </div>
  `;
}

function buildPrintHints(hints) {
  if (!hints.length) return `<p class="print-empty">No BD hints available for this month.</p>`;
  return `
    <div class="print-hint-list">
      ${hints.map(({ day, summary }) => `
        <article class="print-hint">
          <h3>${esc(formatPrintDate(day.date))}</h3>
          <pre>${esc(summary)}</pre>
        </article>
      `).join("")}
    </div>
  `;
}

function buildPrintReport(payload, hints) {
  const days = printMonthDays(payload);
  const selectedMonth = payload?.month_label || state.month || "Selected Month";
  const generatedAt = new Date().toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  return `
    <header class="print-report-head">
      <h1>${esc(selectedMonth)} Biodynamic Calendar</h1>
      <p>${esc(payload?.current?.sign || "--")} Moon | ${esc(payload?.current?.element || "--")} / ${esc(payload?.current?.plant_part || "--")} | Printed ${esc(generatedAt)}</p>
    </header>
    <section class="print-section print-calendar-section">
      <h2>Current Month Calendar</h2>
      ${buildPrintCalendar(payload)}
    </section>
    <section class="print-section print-hints-section">
      <h2>BD Hints for ${esc(selectedMonth)}</h2>
      ${buildPrintHints(hints)}
    </section>
    <section class="print-section print-plantings-section">
      <h2>Plantings</h2>
      ${buildPrintPlantings(days)}
    </section>
    <section class="print-section print-notes-section">
      <h2>Your Notes</h2>
      ${buildPrintNotes(days)}
    </section>
  `;
}

function stageCurrentMonthReport() {
  const payload = state.payload || {};
  if (!payload.ok) return false;
  const report = document.getElementById("printReport");
  const printButton = document.getElementById("printBtn");
  try {
    const hints = immediatePrintHints(payload);
    report.innerHTML = buildPrintReport(payload, hints);
    const title = `${payload.month_label || state.month || "Selected Month"} Biodynamic Calendar`;
    const key = `bd-calendar-report-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    window.localStorage.setItem(key, JSON.stringify({ title, html: report.innerHTML }));
    printButton.href = `/calendar/report?key=${encodeURIComponent(key)}`;
  } catch (err) {
    return false;
  }
  // Enrich the cache after staging so a future report can include full daily guidance.
  void monthlyPrintHints(payload);
  return true;
}

function applyConfigToForm(config) {
  const form = document.getElementById("configForm");
  if (!form || !config) return;
  if (Number.isFinite(Number(config.latitude))) {
    form.elements.latitude.value = Number(config.latitude).toFixed(5);
  }
  if (Number.isFinite(Number(config.longitude))) {
    form.elements.longitude.value = Number(config.longitude).toFixed(5);
  }
  if (config.timezone_name) {
    form.elements.timezone_name.value = String(config.timezone_name);
  }
}

function setLocationBusy(activeButton, busy) {
  const buttons = [
    document.getElementById("saveLocationBtn"),
    document.getElementById("resetLocationBtn"),
  ].filter(Boolean);
  buttons.forEach((button) => {
    button.disabled = busy;
    button.classList.toggle("is-loading", busy && button === activeButton);
    button.setAttribute("aria-busy", busy && button === activeButton ? "true" : "false");
  });
}

document.getElementById("configForm")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const saveButton = document.getElementById("saveLocationBtn");
  const status = document.getElementById("status");
  setLocationBusy(saveButton, true);
  status.textContent = "Saving location.";
  try {
    const fd = new FormData(ev.currentTarget);
    const body = Object.fromEntries(fd.entries());
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await resp.json();
    if (!resp.ok) {
      status.textContent = "Failed to save location.";
      return;
    }
    applyConfigToForm(payload.config || null);
    status.textContent = "Location saved.";
    clearPerformanceCaches();
    state.followCurrentMonth = true;
    state.selectionPinned = false;
    state.selectedDate = "";
    await loadCalendar("");
  } catch (err) {
    status.textContent = "Failed to save location.";
  } finally {
    setLocationBusy(null, false);
  }
});

document.getElementById("resetLocationBtn")?.addEventListener("click", async () => {
  const resetButton = document.getElementById("resetLocationBtn");
  const status = document.getElementById("status");
  setLocationBusy(resetButton, true);
  status.textContent = "Resetting location.";
  try {
    const resp = await fetch("/api/config/reset", { method: "POST" });
    const payload = await resp.json();
    if (!resp.ok || !payload.ok) {
      status.textContent = "Location reset failed.";
      return;
    }
    applyConfigToForm(payload.config || null);
    status.textContent = "Location reset.";
    clearPerformanceCaches();
    state.followCurrentMonth = true;
    state.selectionPinned = false;
    state.selectedDate = "";
    await loadCalendar("");
  } catch (err) {
    status.textContent = "Location reset failed.";
  } finally {
    setLocationBusy(null, false);
  }
});

document.getElementById("plantingForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const status = document.getElementById("plantingStatus");
  const body = Object.fromEntries(new FormData(form).entries());
  status.textContent = "Saving planting.";
  try {
    const resp = await fetch("/api/biodynamic-calendar-app/planting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await resp.json();
    if (!resp.ok || !payload.ok) {
      status.textContent = payload.reason || "Failed to save planting.";
      return;
    }
    state.plantings = Array.isArray(payload.plantings) ? payload.plantings : state.plantings;
    clearPerformanceCaches({ ranges: false, summaries: true });
    resetPlantingForm();
    const editor = document.getElementById("plantingEditor");
    if (editor) {
      editor.open = false;
      const summary = editor.querySelector(":scope > summary");
      if (summary) summary.textContent = "Add planting";
    }
    renderPlantings();
    status.textContent = "Planting saved.";
    await loadCalendar(state.month || "", state.selectedDate, { refreshRange: true });
  } catch (err) {
    status.textContent = "Failed to save planting.";
  }
});

document.getElementById("clearPlantingBtn").addEventListener("click", () => {
  resetPlantingForm();
  const editor = document.getElementById("plantingEditor");
  if (editor) {
    editor.open = false;
    const summary = editor.querySelector(":scope > summary");
    if (summary) summary.textContent = "Add planting";
  }
  document.getElementById("plantingStatus").textContent = "";
});

document.getElementById("plantingList").addEventListener("click", async (ev) => {
  const target = ev.target instanceof Element ? ev.target : null;
  const editBtn = target ? target.closest("[data-edit-planting]") : null;
  const deleteBtn = target ? target.closest("[data-delete-planting]") : null;
  if (editBtn) {
    const id = editBtn.getAttribute("data-edit-planting") || "";
    const planting = sortedPlantings().find((row) => String(row.id || "") === id);
    fillPlantingForm(planting);
    return;
  }
  if (!deleteBtn) return;
  const id = deleteBtn.getAttribute("data-delete-planting") || "";
  if (!id) return;
  const status = document.getElementById("plantingStatus");
  status.textContent = "Deleting planting.";
  try {
    const resp = await fetch(`/api/biodynamic-calendar-app/planting/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await resp.json();
    if (!resp.ok || !payload.ok) {
      status.textContent = "Failed to delete planting.";
      return;
    }
    state.plantings = Array.isArray(payload.plantings) ? payload.plantings : [];
    clearPerformanceCaches({ ranges: false, summaries: true });
    resetPlantingForm();
    renderPlantings();
    status.textContent = payload.deleted ? "Planting deleted." : "Planting was already removed.";
    await loadCalendar(state.month || "", state.selectedDate, { refreshRange: true });
  } catch (err) {
    status.textContent = "Failed to delete planting.";
  }
});

document.getElementById("saveNoteBtn").addEventListener("click", async () => {
  if (!state.selectedDate) return;
  state.selectionPinned = true;
  await fetch("/api/biodynamic-calendar-app/note", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ date: state.selectedDate, note: document.getElementById("noteInput").value }),
  });
  await loadCalendar(state.month, state.selectedDate, { refreshRange: false });
});

document.getElementById("printBtn").addEventListener("click", (ev) => {
  if (!stageCurrentMonthReport()) ev.preventDefault();
});

function navigateCalendarMonth(delta) {
  const targetMonth = shiftMonth(state.month || currentMonthKey(), delta);
  state.followCurrentMonth = targetMonth === currentMonthKey();
  state.selectionPinned = false;
  state.selectedDate = "";
  if (!promoteCachedMonth(targetMonth)) {
    void loadCalendar(targetMonth);
  }
}

document.getElementById("prevBtn").addEventListener("click", () => {
  navigateCalendarMonth(-1);
});
document.getElementById("nextBtn").addEventListener("click", () => {
  navigateCalendarMonth(1);
});

const dashboardReturn = document.getElementById("dashboardReturn");
if (dashboardReturn) {
  dashboardReturn.addEventListener("click", (ev) => {
    ev.preventDefault();
    if (dashboardReturn.classList.contains("is-loading")) return;
    dashboardReturn.classList.add("is-loading");
    dashboardReturn.setAttribute("aria-busy", "true");
    const destination = dashboardReturn.href;
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => window.location.assign(destination));
    });
  });
  window.addEventListener("pageshow", () => {
    dashboardReturn.classList.remove("is-loading");
    dashboardReturn.removeAttribute("aria-busy");
  });
}

async function refreshCurrentStatus() {
  const nextMonth = state.followCurrentMonth ? currentMonthKey() : (state.month || currentMonthKey());
  const monthChanged = Boolean(state.month && nextMonth && state.month !== nextMonth);
  const preferredDate = state.selectionPinned ? state.selectedDate : "";
  if (!state.selectionPinned) state.selectedDate = "";
  await loadCalendar(nextMonth, preferredDate, {
    refreshRange: monthChanged,
    preserveNoteDraft: true,
    refreshSummary: false,
    showLoading: false,
  });
}

setInterval(() => { void refreshCurrentStatus(); }, STATUS_REFRESH_MS);

loadCalendar("");
