export function initializeDashboard(config, { pressureGaugeConfig, startRainGauges }) {
function displaySensorId(sid) {
  const label = String(sid || '').toUpperCase();
  const match = label.match(/^(ECOWITT|ACURITE)-([A-Z0-9]+)$/);
  return match ? `${match[1]}-${match[2].slice(-6)}` : label;
}
let pressureAltitude = config.pressureAltitude;
let pressureSensorContext = config.pressureSensorContext;
let stepCount = 0;
const gaugeConfig = config.gaugeConfig;
const currentValues = config.currentValues;
const sensorStats = config.sensorStats;
const expectedGaugeMap = config.expectedGaugeMap;
const expectedDisplayStyleMap = config.expectedDisplayStyleMap;
const dashboardMetricSet = config.dashboardMetricSet;
const metricCanvasWidth = config.metricCanvasWidth;
const metricCanvasHeight = config.metricCanvasHeight;
const astroData = config.astroData;
const biodynamicData = config.biodynamicData;
const weatherForecastProvider = config.weatherForecastProvider;
const weatherForecastEnabled = config.weatherForecastEnabled;
window.__weatherForecastEnabled = weatherForecastEnabled;
window.__weatherForecastProvider = weatherForecastProvider;
const isPiPlatform = config.isPiPlatform;
const lastTimestamps = {};
let lastSensorTimestampChangeMs = Date.now();
const sensorTimestampStaleMs = 75000;
const dashboardFetchTimeoutMs = 12000;
const dashboardExtrasRefreshMs = 60000;
const dashboardExtrasWarmRetryMs = 5000;
const micrographFetchTimeoutMs = 20000;
const micrographInflightStaleMs = 30000;
function fetchWithTimeout(url, options = {}, timeoutMs = dashboardFetchTimeoutMs, consumeResponse = null) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((resolve, reject) => { timer = window.setTimeout(() => { controller.abort(); reject(new Error('Request timed out')); }, timeoutMs); });
  const opts = Object.assign({}, options, { signal: controller.signal });
  const response = fetch(url, opts).then(res => consumeResponse ? consumeResponse(res) : res);
  return Promise.race([response, timeout]).finally(() => window.clearTimeout(timer));
}
const metricTileStyleMap = config.metricTileStyleMap || {};
const displayStyle = config.displayStyle;
window.displayStyle = displayStyle;
function toSafe(s) { return (s || '').replace(/[^A-Za-z0-9_-]/g, '_'); }
if (window.location.search.includes('refresh=true')) {
  window.history.replaceState(null, '', window.location.pathname);
  window.location.reload(true);
} else if (new URLSearchParams(window.location.search).has('dashboard_return')) {
  window.history.replaceState(null, '', window.location.pathname);
}
function closeMenu() {
  const menu = document.getElementById('menu');
  if (menu) menu.style.display = 'none';
}
let __lastBiodynamicMinuteKey = '';
let __lastAstronomyDateKey = '';
function astronomyLocalDateKey(now) {
  const timezoneName = String((window.caelusMoonPopupData && window.caelusMoonPopupData.timezone) || (astroData && (astroData.timezone || astroData.tz)) || '').trim();
  try { return new Intl.DateTimeFormat('en-CA', { timeZone: timezoneName || undefined, year:'numeric', month:'2-digit', day:'2-digit' }).format(now); }
  catch (_) { return `${now.getFullYear()}-${now.getMonth()+1}-${now.getDate()}`; }
}
function updateAstronomyDates(data){
  const timezoneName = String((data && (data.timezone || data.tz)) || (window.__weatherForecastPayload && window.__weatherForecastPayload.location && window.__weatherForecastPayload.location.timezone) || (biodynamicData && biodynamicData.tz) || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const date = new Intl.DateTimeFormat('en-US', { timeZone:timezoneName, weekday:'long', month:'long', day:'2-digit', year:'numeric' }).format(new Date());
  for (const id of ['moonDateLine', 'sunDateLine', 'forecastDateLine']) {
    const el = document.getElementById(id);
    if (el) el.textContent = date;
  }
}
function updateLocalTime() {
  const now = new Date();
  const ts = document.getElementById('update_time');
  if (ts) {
    const formatted = now.toLocaleString('en-CA', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' }).replace(',', '');
    const weekday = now.toLocaleDateString('en-CA', { weekday:'short' });
    ts.textContent = `${weekday} ${formatted}`;
  }
  const biodynamicMinuteKey = `${now.getFullYear()}-${now.getMonth()+1}-${now.getDate()}-${now.getHours()}-${now.getMinutes()}`;
  if (biodynamicMinuteKey !== __lastBiodynamicMinuteKey) {
    __lastBiodynamicMinuteKey = biodynamicMinuteKey;
    updateAstronomyDates(astroData);
    if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);
    const astronomyDateKey = astronomyLocalDateKey(now);
    if (!__lastAstronomyDateKey) {
      __lastAstronomyDateKey = astronomyDateKey;
    } else if (astronomyDateKey !== __lastAstronomyDateKey) {
      const previousDateKey = __lastAstronomyDateKey;
      __lastAstronomyDateKey = astronomyDateKey;
      if (typeof refreshCaelusAstronomy === 'function') refreshCaelusAstronomy().catch((error) => { if (__lastAstronomyDateKey === astronomyDateKey) __lastAstronomyDateKey = previousDateKey; console.error('Failed to refresh eclipse information at local midnight', error); });
    }
  }
}
function makeSmoothSkyYMapper(yBase, topY, bottomY, maxElev, minElev){
  const rawTop = Number(topY);
  const rawBottom = Number(bottomY);
  const top = Number.isFinite(rawTop) ? Math.min(yBase - 1, rawTop) : yBase - 1;
  const bottom = Number.isFinite(rawBottom) ? Math.max(yBase + 1, rawBottom) : yBase + 1;
  const abovePx = Math.max(1, yBase - top);
  const belowPx = Math.max(1, bottom - yBase);
  const maxE = Math.max(1, Math.abs(Number(maxElev) || 1));
  const minE = Math.max(1, Math.abs(Number(minElev) || 1));
  const posScale = abovePx / maxE;
  const negScale = belowPx / minE;
  const bandLimit = Math.max(1, Math.min(maxE, minE));
  const band = Math.max(0.5, Math.min(bandLimit, 12, Math.max(5, bandLimit * 0.35)));
  const hermite = (t, y0, m0, y1, m1, span) => {
    const tt = Math.max(0, Math.min(1, t));
    const t2 = tt * tt;
    const t3 = t2 * tt;
    return ((2 * t3 - 3 * t2 + 1) * y0) + ((t3 - 2 * t2 + tt) * span * m0) + ((-2 * t3 + 3 * t2) * y1) + ((t3 - t2) * span * m1);
  };
  const midSlope = -((posScale + negScale) / 2);
  return (e) => {
    const elev = Number(e);
    if (!Number.isFinite(elev)) return yBase;
    if (elev >= band) return yBase - (Math.min(elev, maxE) * posScale);
    if (elev <= -band) return yBase - (Math.max(elev, -minE) * negScale);
    if (elev < 0) {
      const t = (elev + band) / band;
      return hermite(t, yBase + (band * negScale), -negScale, yBase, midSlope, band);
    }
    return hermite(elev / band, yBase, midSlope, yBase - (band * posScale), -posScale, band);
  };
}
function pickMoonEventRaw(sameDay, nearest){
  const dayVal = typeof sameDay === 'string' ? sameDay.trim() : '';
  if (dayVal) return dayVal;
  return typeof nearest === 'string' ? nearest.trim() : '';
}
function drawSunPath(data){
  updateAstronomyDates(data);
  const c = document.getElementById('sunPathCanvas');
  const meta = document.getElementById('sunMeta');
  const riseEl = document.getElementById('sunTimeRise');
  const noonEl = document.getElementById('sunTimeNoon');
  const setEl = document.getElementById('sunTimeSet');
  const moonMeta = document.getElementById('moonPositionMeta');
  const moonRiseEl = document.getElementById('moonTimeRise');
  const moonSetEl = document.getElementById('moonTimeSet');
  if (!c || !meta || !riseEl || !noonEl || !setEl || !moonMeta || !moonRiseEl || !moonSetEl) return;
  if (typeof setAstroCardsLoading === 'function') setAstroCardsLoading(isDashboardWarmingPayload(data));
  const ctx = c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height);
  const toMin = (hhmm) => {
    if (typeof hhmm === 'number' && Number.isFinite(hhmm)) return Math.max(0, Math.min(1440, hhmm));
    const m = String(hhmm || '').match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return null;
    const hh = parseInt(m[1], 10);
    const mm = parseInt(m[2], 10);
    if (hh === 24 && mm === 0) return 1440;
    return Math.max(0, Math.min(1440, (hh * 60) + mm));
  };
  const fmtSun = (hhmm) => {
    const m = String(hhmm || '').match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return '--';
    const hh = parseInt(m[1], 10);
    const mm = m[2];
    const ap = hh < 12 ? 'A' : 'P';
    const h12 = (hh % 12) || 12;
    return `${h12}:${mm}${ap}`;
  };
  const placeLabel = (el, minutes, fallback) => {
    const raw = Number.isFinite(minutes) ? minutes : fallback;
    const p = Number.isFinite(raw) ? (raw / 1440) : 0.5;
    const clamped = Math.max(0.06, Math.min(0.94, p));
    el.style.left = `${(clamped * 100).toFixed(2)}%`;
  };
  const pointMinute = (p) => {
    const raw = Number(p && p.m);
    if (Number.isFinite(raw)) return Math.max(0, Math.min(1440, raw));
    return toMin(p && p.t);
  };
  const pointList = (items) => {
    if (!Array.isArray(items)) return [];
    return items.map((p) => ({m: pointMinute(p), e: Number(p && p.e)})).filter((p) => Number.isFinite(p.m) && Number.isFinite(p.e)).sort((a,b) => a.m - b.m);
  };
  const interpPointValue = (pts, minutes, key) => {
    if (!Array.isArray(pts) || !pts.length || !Number.isFinite(minutes)) return NaN;
    const first = Number(pts[0] && pts[0][key]);
    if (minutes <= pts[0].m) return Number.isFinite(first) ? first : NaN;
    for (let i = 1; i < pts.length; i++) {
      if (minutes <= pts[i].m) {
        const a = pts[i-1], b = pts[i];
        const av = Number(a && a[key]);
        const bv = Number(b && b[key]);
        if (!Number.isFinite(av) || !Number.isFinite(bv)) return NaN;
        const span = Math.max(1, b.m - a.m);
        const f = (minutes - a.m) / span;
        return av + ((bv - av) * f);
      }
    }
    const last = Number(pts[pts.length - 1] && pts[pts.length - 1][key]);
    return Number.isFinite(last) ? last : NaN;
  };
  const interpElev = (pts, minutes) => interpPointValue(pts, minutes, 'e');
  const interpY = (pts, minutes) => interpPointValue(pts, minutes, 'y');
  const sr = toMin(data && data.sunrise);
  const ss = toMin(data && data.sunset);
  const nn = toMin(data && data.sun_noon);
  const mrRaw = pickMoonEventRaw(data && data.moon_rise_today, data && data.moon_rise);
  const msRaw = pickMoonEventRaw(data && data.moon_set_today, data && data.moon_set);
  const mr = toMin(mrRaw);
  const ms = toMin(msRaw);
  riseEl.textContent = fmtSun(data && data.sunrise);
  noonEl.textContent = fmtSun(data && data.sun_noon);
  setEl.textContent = fmtSun(data && data.sunset);
  moonRiseEl.textContent = fmtSun(mrRaw);
  moonSetEl.textContent = fmtSun(msRaw);
  placeLabel(riseEl, sr, 360);
  placeLabel(noonEl, Number.isFinite(nn) ? nn : 720, 720);
  placeLabel(setEl, ss, 1080);
  placeLabel(moonRiseEl, mr, 390);
  placeLabel(moonSetEl, ms, 1050);
  const padX = 8, padY = 2;
  const w = c.width - padX*2, h = c.height - padY*2;
  const yBase = padY + (h * 0.54);
  ctx.fillStyle = '#dff1ff';
  ctx.fillRect(padX, padY, w, yBase - padY);
  ctx.fillStyle = '#000000';
  ctx.fillRect(padX, yBase, w, (c.height - padY) - yBase);
  ctx.strokeStyle = '#8fa4b3';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padX, yBase);
  ctx.lineTo(c.width - padX, yBase);
  ctx.stroke();
  const sunPoints = pointList(data && data.sun_points);
  const moonPoints = pointList(data && data.moon_points);
  const allElevs = sunPoints.concat(moonPoints).map((p) => p.e).filter((v) => Number.isFinite(v));
  const maxElev = Math.max(20, ...allElevs.filter((v) => v > 0));
  const minElev = Math.min(-18, ...allElevs.filter((v) => v < 0));
  const abovePx = Math.max(1, yBase - padY - 2);
  const belowPx = Math.max(1, (c.height - padY) - yBase - 2);
  const yForElev = makeSmoothSkyYMapper(yBase, padY + 2, c.height - padY - 2, maxElev, minElev);
  const xForMin = (m) => padX + ((Math.max(0, Math.min(1440, m)) / 1440) * w);
  const curveHermite = (t, y0, s0, y1, s1, span) => {
    const tt = Math.max(0, Math.min(1, t));
    const t2 = tt * tt;
    const t3 = t2 * tt;
    return ((2 * t3 - 3 * t2 + 1) * y0) + ((t3 - 2 * t2 + tt) * span * s0) + ((-2 * t3 + 3 * t2) * y1) + ((t3 - t2) * span * s1);
  };
  const buildSunCurveKeys = () => {
    if (!data || !data.ok || !Number.isFinite(sr) || !Number.isFinite(ss) || sr >= ss) return [];
    const peakMin = Number.isFinite(nn) ? Math.max(sr + 1, Math.min(ss - 1, nn)) : ((sr + ss) / 2);
    const prevSunset = ss - 1440;
    const nextSunrise = sr + 1440;
    const prevTrough = prevSunset + ((sr - prevSunset) / 2);
    const nextTrough = ss + ((nextSunrise - ss) / 2);
    const topY = yBase - abovePx;
    const bottomY = yBase + belowPx;
    const riseSlope = -Math.min((abovePx * Math.PI) / (2 * Math.max(1, peakMin - sr)), (belowPx * Math.PI) / (2 * Math.max(1, sr - prevTrough)));
    const setSlope = Math.min((abovePx * Math.PI) / (2 * Math.max(1, ss - peakMin)), (belowPx * Math.PI) / (2 * Math.max(1, nextTrough - ss)));
    return [
      {m: prevSunset, y: yBase, s: setSlope},
      {m: prevTrough, y: bottomY, s: 0},
      {m: sr, y: yBase, s: riseSlope},
      {m: peakMin, y: topY, s: 0},
      {m: ss, y: yBase, s: setSlope},
      {m: nextTrough, y: bottomY, s: 0},
      {m: nextSunrise, y: yBase, s: riseSlope},
    ];
  };
  const sunKeys = buildSunCurveKeys();
  const sunYForMin = (m) => {
    if (!sunKeys.length) return yBase;
    const minuteRaw = Number(m);
    const minute = Number.isFinite(minuteRaw) ? Math.max(0, Math.min(1440, minuteRaw)) : 0;
    if (minute <= sunKeys[0].m) return sunKeys[0].y;
    for (let i = 0; i < sunKeys.length - 1; i++) {
      const a = sunKeys[i];
      const b = sunKeys[i + 1];
      if (minute <= b.m) {
        const span = Math.max(1, b.m - a.m);
        return curveHermite((minute - a.m) / span, a.y, a.s, b.y, b.s, span);
      }
    }
    return sunKeys[sunKeys.length - 1].y;
  };
  const buildSunDisplayPoints = () => {
    if (!sunKeys.length) return [];
    const built = [];
    for (let m = 0; m <= 1440; m += 5) {
      built.push({m, y: sunYForMin(m)});
    }
    return built;
  };
  const smoothSampledElevationPath = (pts, stepMin) => {
    const sorted = Array.isArray(pts) ? pts.filter((p) => Number.isFinite(p.m) && Number.isFinite(p.e)).sort((a,b) => a.m - b.m) : [];
    if (sorted.length < 2) return [];
    const step = Math.max(1, Number(stepMin) || 5);
    const yVals = sorted.map((p) => yForElev(p.e));
    const slopeAt = (idx) => {
      if (idx > 0 && idx < sorted.length - 1) {
        const dyPrev = yVals[idx] - yVals[idx - 1];
        const dyNext = yVals[idx + 1] - yVals[idx];
        if ((dyPrev < 0 && dyNext > 0) || (dyPrev > 0 && dyNext < 0)) return 0;
      }
      const prev = sorted[Math.max(0, idx - 1)];
      const next = sorted[Math.min(sorted.length - 1, idx + 1)];
      const span = Math.max(1, next.m - prev.m);
      return ((yVals[Math.min(sorted.length - 1, idx + 1)] - yVals[Math.max(0, idx - 1)]) / span) * 0.65;
    };
    const slopes = sorted.map((_p, idx) => slopeAt(idx));
    const built = [];
    for (let i = 0; i < sorted.length - 1; i++) {
      const a = sorted[i];
      const b = sorted[i + 1];
      const span = Math.max(1, b.m - a.m);
      const start = i === 0 ? a.m : Math.ceil(a.m / step) * step;
      const end = b.m;
      for (let m = start; m < end; m += step) {
        const t = (m - a.m) / span;
        built.push({m, y: curveHermite(t, yVals[i], slopes[i], yVals[i + 1], slopes[i + 1], span)});
      }
    }
    const last = sorted[sorted.length - 1];
    built.push({m: last.m, y: yVals[yVals.length - 1]});
    return built;
  };
  const softenMoonLowerExtrema = (pts) => {
    const sorted = (Array.isArray(pts) ? pts : []).map((p) => ({m: Number(p && p.m), y: Number(p && p.y)})).filter((p) => Number.isFinite(p.m) && Number.isFinite(p.y)).sort((a,b) => a.m - b.m);
    if (sorted.length < 5) return sorted;
    const rounded = sorted.map((p) => ({m: p.m, y: p.y}));
    const lastIdx = sorted.length - 1;
    const lowerExtrema = [];
    for (let i = 2; i < lastIdx - 1; i++) {
      const y = sorted[i].y;
      if (y <= yBase + 3) continue;
      const leftY = sorted[i - 1].y;
      const rightY = sorted[i + 1].y;
      if (y >= leftY && y >= rightY && (y > leftY || y > rightY)) {
        const prevIdx = lowerExtrema.length ? lowerExtrema[lowerExtrema.length - 1] : -1;
        if (prevIdx >= 0 && sorted[i].m - sorted[prevIdx].m < 32) {
          if (y > sorted[prevIdx].y) lowerExtrema[lowerExtrema.length - 1] = i;
        } else {
          lowerExtrema.push(i);
        }
      }
    }
    lowerExtrema.forEach((idx) => {
      const apex = sorted[idx];
      let leftHorizon = null;
      let rightHorizon = null;
      for (let j = idx; j >= 0; j--) {
        if (sorted[j].y <= yBase + 1.5) { leftHorizon = sorted[j].m; break; }
      }
      for (let j = idx; j <= lastIdx; j++) {
        if (sorted[j].y <= yBase + 1.5) { rightHorizon = sorted[j].m; break; }
      }
      const leftRoom = Number.isFinite(leftHorizon) ? apex.m - leftHorizon : 90;
      const rightRoom = Number.isFinite(rightHorizon) ? rightHorizon - apex.m : 90;
      const halfWindow = Math.max(48, Math.min(420, leftRoom * 0.88, rightRoom * 0.88));
      const leftM = Math.max(sorted[0].m, apex.m - halfWindow);
      const rightM = Math.min(sorted[lastIdx].m, apex.m + halfWindow);
      if (apex.m - leftM < 18 || rightM - apex.m < 18) return;
      const leftY = interpY(sorted, leftM);
      const rightY = interpY(sorted, rightM);
      if (!Number.isFinite(leftY) || !Number.isFinite(rightY)) return;
      rounded.forEach((p) => {
        if (p.m < leftM || p.m > rightM) return;
        if (p.m <= apex.m) {
          const span = Math.max(1, apex.m - leftM);
          const eased = Math.sin(((p.m - leftM) / span) * Math.PI * 0.5);
          p.y = leftY + ((apex.y - leftY) * eased);
        } else {
          const span = Math.max(1, rightM - apex.m);
          const eased = Math.cos(((p.m - apex.m) / span) * Math.PI * 0.5);
          p.y = rightY + ((apex.y - rightY) * eased);
        }
      });
    });
    return rounded;
  };
  const buildMoonRiseSetDisplayPoints = () => {
    if (!Number.isFinite(mr) || !Number.isFinite(ms) || mr === ms) return [];
    const riseMin = mr;
    const setMin = ms > riseMin ? ms : ms + 1440;
    const visibleMinutes = setMin - riseMin;
    if (visibleMinutes <= 0 || visibleMinutes >= 1440) return [];
    const amplitude = Math.min(abovePx * 0.94, belowPx * 0.9);
    const yAt = (minute) => {
      const wave = Math.sin(Math.PI * (minute - riseMin) / visibleMinutes);
      return yBase - (amplitude * wave);
    };
    const built = [];
    for (let m = 0; m <= 1440; m += 2) {
      built.push({m, y: yAt(m)});
    }
    return built;
  };
  const buildMoonDisplayPoints = () => {
    const riseSet = buildMoonRiseSetDisplayPoints();
    if (riseSet.length >= 2) return riseSet;
    const sampled = smoothSampledElevationPath(moonPoints, 2);
    if (sampled.length >= 2) return sampled;
    return moonPoints;
  };
  const drawPath = (pts, color, width, dash) => {
    if (!Array.isArray(pts) || pts.length < 2) return false;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.setLineDash(Array.isArray(dash) ? dash : []);
    const xy = pts.map((p) => {
      const yRaw = Number(p && p.y);
      const eRaw = Number(p && p.e);
      const y = Number.isFinite(yRaw) ? yRaw : yForElev(eRaw);
      return {x: xForMin(p && p.m), y};
    }).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
    if (xy.length < 2) { ctx.restore(); return false; }
    ctx.beginPath();
    ctx.moveTo(xy[0].x, xy[0].y);
    const tangentAt = (idx) => {
      const cur = xy[idx];
      const prev = xy[Math.max(0, idx - 1)];
      const next = xy[Math.min(xy.length - 1, idx + 1)];
      let ty = (next.y - prev.y) * 0.38;
      if (idx > 0 && idx < xy.length - 1) {
        const dyPrev = cur.y - prev.y;
        const dyNext = next.y - cur.y;
        if ((dyPrev < 0 && dyNext > 0) || (dyPrev > 0 && dyNext < 0)) ty = 0;
      }
      return {x: (next.x - prev.x) * 0.38, y: ty};
    };
    const tangents = xy.map((_p, idx) => tangentAt(idx));
    for (let i = 0; i < xy.length - 1; i++) {
      const p0 = xy[i];
      const p1 = xy[i + 1];
      const t0 = tangents[i];
      const t1 = tangents[i + 1];
      ctx.bezierCurveTo(
        p0.x + (t0.x / 3), p0.y + (t0.y / 3),
        p1.x - (t1.x / 3), p1.y - (t1.y / 3),
        p1.x, p1.y
      );
    }
    ctx.stroke();
    ctx.restore();
    return true;
  };
  const sunDisplayPoints = buildSunDisplayPoints();
  const moonDisplayPoints = buildMoonDisplayPoints();
  const drewSun = drawPath(sunDisplayPoints, '#f3d34a', 2.1, []);
  if (!drewSun && data && data.ok && Number.isFinite(sr) && Number.isFinite(ss) && sr < ss){
    ctx.strokeStyle = '#f3d34a';
    ctx.lineWidth = 2.1;
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (let m = 0; m <= 1440; m += 10){
      const x = xForMin(m);
      const y = sunYForMin(m);
      if (m === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  drawPath(moonDisplayPoints, '#69bdf2', 1.65, []);
  const now = new Date();
  const curMin = (now.getHours() * 60) + now.getMinutes();
  const xNow = xForMin(curMin);
  const sunYNow = interpY(sunDisplayPoints, curMin);
  const yNow = Number.isFinite(sunYNow) ? sunYNow : ((Number.isFinite(sr) && Number.isFinite(ss) && sr < ss) ? sunYForMin(curMin) : yBase);
  ctx.fillStyle = '#ffff00';
  ctx.beginPath();
  ctx.arc(xNow, yNow, 3.9, 0, Math.PI*2);
  ctx.fill();
  ctx.strokeStyle = '#ff8c00';
  ctx.lineWidth = 1;
  ctx.stroke();
  const moonYNow = interpY(moonDisplayPoints, curMin);
  if (Number.isFinite(moonYNow)){
    const yMoonNow = moonYNow;
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,.35)';
    ctx.shadowBlur = 2;
    ctx.shadowOffsetY = 0.6;
    ctx.fillStyle = '#fff8df';
    ctx.beginPath();
    ctx.arc(xNow, yMoonNow, 4.2, 0, Math.PI*2);
    ctx.fill();
    ctx.shadowColor = 'transparent';
    ctx.strokeStyle = '#2e78a9';
    ctx.lineWidth = 1.35;
    ctx.stroke();
    ctx.fillStyle = '#d9edff';
    ctx.beginPath();
    ctx.arc(xNow - 1.1, yMoonNow - 1.1, 1.1, 0, Math.PI*2);
    ctx.fill();
    ctx.restore();
  }
  const moonDec = Number(data && data.moon_declination);
  const source = String((data && data.moon_position_source) || '').trim();
  if (Number.isFinite(moonDec)) c.title = `Moon declination ${moonDec.toFixed(1)} deg${source ? ` (${source})` : ''}`; else c.removeAttribute('title');
}

function sunMoon29IsOpen(){
  const row = document.querySelector('.dash-top-row');
  return !!(row && row.classList.contains('sun-moon-expanded'));
}

function sunMoon29Label(day){
  const direct = String((day && day.label) || '').trim();
  if (direct) return direct;
  const raw = String((day && day.date) || '').trim();
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return raw || '--';
  const mons = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${mons[Math.max(0, Math.min(11, parseInt(m[2], 10) - 1))]}${parseInt(m[3], 10)}`;
}

function drawSunMoon29Day(data){
  const c = document.getElementById('sunMoon29Canvas');
  const meta = document.getElementById('sunMoon29Meta');
  if (!c || !meta) return;
  if (typeof setAstroCardsLoading === 'function') setAstroCardsLoading(isDashboardWarmingPayload(data));
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  const days = Array.isArray(data && data.position_29d) ? data.position_29d : [];
  const padL = 30, padR = 12, padT = 8, padB = 8;
  const graphW = Math.max(1, c.width - padL - padR);
  const contentH = Math.max(1, c.height - padT - padB);
  const graphH = Math.max(48, Math.round(contentH * 0.64));
  const labelY = padT + graphH + 7;
  const moonY = Math.min(c.height - padB - 7, labelY + 17);
  const yBase = padT + (graphH * 0.56);
  ctx.fillStyle = '#fff9d6';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.fillStyle = '#dff1ff';
  ctx.fillRect(padL, padT, graphW, Math.max(1, yBase - padT));
  ctx.fillStyle = '#000000';
  ctx.fillRect(padL, yBase, graphW, Math.max(1, (padT + graphH) - yBase));
  ctx.strokeStyle = '#8fa4b3';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, yBase);
  ctx.lineTo(c.width - padR, yBase);
  ctx.stroke();
  const fromSeries = (items, dayIdx) => {
    if (!Array.isArray(items)) return [];
    return items.map((p) => {
      const minuteRaw = Array.isArray(p) ? p[0] : (p && p.m);
      const elevRaw = Array.isArray(p) ? p[1] : (p && p.e);
      const minute = Number(minuteRaw);
      const elev = Number(elevRaw);
      if (!Number.isFinite(minute) || !Number.isFinite(elev)) return null;
      return { x: (dayIdx * 1440) + Math.max(0, Math.min(1440, minute)), e: elev };
    }).filter(Boolean);
  };
  const sunPoints = [];
  const moonPoints = [];
  days.forEach((day, dayIdx) => {
    sunPoints.push(...fromSeries(day && day.sun, dayIdx));
    moonPoints.push(...fromSeries(day && day.moon, dayIdx));
  });
  const allElevs = sunPoints.concat(moonPoints).map((p) => p.e).filter((v) => Number.isFinite(v));
  if (!days.length || !allElevs.length) {
    meta.textContent = '29 day position data unavailable';
    return;
  }
  const totalMin = Math.max(1440, days.length * 1440);
  const maxElev = Math.max(20, ...allElevs.filter((v) => v > 0));
  const minElev = Math.min(-18, ...allElevs.filter((v) => v < 0));
  const xForMin = (m) => padL + ((Math.max(0, Math.min(totalMin, m)) / totalMin) * graphW);
  const yForElev = makeSmoothSkyYMapper(yBase, padT + 2, padT + graphH - 2, maxElev, minElev);
  const drawTinyMoonPhase = (day, cx, cy) => {
    const rawPhase = Number(day && day.moon_phase_value);
    if (!Number.isFinite(rawPhase)) return;
    const phase = ((rawPhase % 28) + 28) % 28;
    const moonPhaseCardSize = 88;
    const r = (moonPhaseCardSize * 0.15) / 2;
    const size = Math.max(12, Math.ceil((r * 2) + 4));
    const x = size / 2;
    const y = size / 2;
    const phaseAngle = (2 * Math.PI * phase) / 28;
    const limbStrength = Math.abs(Math.sin(phaseAngle));
    const sx = limbStrength;
    const sz = -Math.cos(phaseAngle);
    const lat = Number(data && data.lat);
    const fallbackAngle = Number.isFinite(lat) && lat < 0 ? -60 : 60;
    const rawVisibleAngle = Number(day && day.moon_visible_angle);
    const rotationDeg = Number.isFinite(rawVisibleAngle) ? rawVisibleAngle : fallbackAngle;
    const phaseCanvas = document.createElement('canvas');
    phaseCanvas.width = size;
    phaseCanvas.height = size;
    const phaseCtx = phaseCanvas.getContext('2d');
    const image = phaseCtx.createImageData(size, size);
    const pix = image.data;
    for (let py = 0; py < size; py++) {
      for (let px = 0; px < size; px++) {
        const dx = (px + 0.5 - x) / r;
        const dy = (py + 0.5 - y) / r;
        const rr = dx * dx + dy * dy;
        const off = (py * size + px) * 4;
        if (rr > 1) { pix[off + 3] = 0; continue; }
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
    ctx.strokeStyle = '#58524a';
    ctx.lineWidth = 0.75;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  };
  ctx.save();
  ctx.strokeStyle = 'rgba(39, 49, 58, 0.22)';
  ctx.fillStyle = '#27313a';
  ctx.font = '9px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.textBaseline = 'top';
  for (let i = 0; i <= days.length; i++) {
    const x = xForMin(i * 1440);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, c.height - padB);
    ctx.stroke();
    if (i > 0 && i < days.length) {
      const label = sunMoon29Label(days[i]);
      ctx.textAlign = i === 0 ? 'left' : 'center';
      ctx.fillText(label, i === 0 ? x + 2 : x, labelY);
    }
  }
  ctx.restore();
  days.forEach((day, dayIdx) => {
    drawTinyMoonPhase(day, xForMin((dayIdx * 1440) + 720), moonY);
  });
  const drawPath = (pts, color, width) => {
    if (!Array.isArray(pts) || pts.length < 2) return;
    const sorted = pts.slice().sort((a, b) => a.x - b.x);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();
    const xy = sorted.map((p) => ({x: xForMin(p.x), y: yForElev(p.e)})).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
    if (xy.length < 2) { ctx.restore(); return; }
    ctx.moveTo(xy[0].x, xy[0].y);
    for (let i = 1; i < xy.length - 1; i++) {
      const p = xy[i];
      const next = xy[i + 1];
      const midX = (p.x + next.x) / 2;
      const midY = (p.y + next.y) / 2;
      ctx.quadraticCurveTo(p.x, p.y, midX, midY);
    }
    const last = xy[xy.length - 1];
    const prev = xy[xy.length - 2];
    ctx.quadraticCurveTo(prev.x, prev.y, last.x, last.y);
    ctx.stroke();
    ctx.restore();
  };
  drawPath(sunPoints, '#f3d34a', 1.55);
  drawPath(moonPoints, '#69bdf2', 1.35);
  ctx.save();
  ctx.font = '10px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.textBaseline = 'top';
  ctx.textAlign = 'right';
  ctx.fillStyle = '#8d7115';
  ctx.fillText('Sun', c.width - padR - 42, padT + 4);
  ctx.fillStyle = '#2e78a9';
  ctx.fillText('Moon', c.width - padR - 4, padT + 4);
  ctx.restore();
  const firstLabel = sunMoon29Label(days[0]);
  const lastLabel = sunMoon29Label(days[days.length - 1]);
  meta.textContent = `${firstLabel} - ${lastLabel}`;
}

function openSunMoon29Day(){
  const row = document.querySelector('.dash-top-row');
  const overlay = document.getElementById('sunMoon29Overlay');
  if (!row || !overlay) return;
  row.classList.add('sun-moon-expanded');
  overlay.setAttribute('aria-hidden', 'false');
  drawSunMoon29Day(astroData);
  const card = document.getElementById('sunMoon29Card');
  try { if (card) card.focus({ preventScroll: true }); } catch (_) { if (card) card.focus(); }
}

function closeSunMoon29Day(){
  const row = document.querySelector('.dash-top-row');
  const overlay = document.getElementById('sunMoon29Overlay');
  if (row) row.classList.remove('sun-moon-expanded');
  if (overlay) overlay.setAttribute('aria-hidden', 'true');
}

window.openSunMoon29Day = openSunMoon29Day;
window.closeSunMoon29Day = closeSunMoon29Day;

function caelusMoonIsOpen(){
  const overlay = document.getElementById('caelusMoonOverlay');
  return !!(overlay && overlay.classList.contains('is-open'));
}
function setCaelusMoonText(id, value){
  const element = document.getElementById(id);
  if (element) element.textContent = value == null || value === '' ? '--' : String(value);
}
function formatCaelusEclipse(event){
  if (!event || typeof event !== 'object') return '';
  const kind = String(event.kind || 'Eclipse');
  const date = String(event.date || '');
  const starts = String(event.starts || event.time || '');
  const ends = String(event.ends || '');
  const timeRange = starts && ends ? `${starts}–${ends}` : starts;
  return [kind, date, timeRange].filter(Boolean).join(' · ');
}
function renderDashboardEclipse(moon){
  const line = document.getElementById('moonEclipse24h');
  const event = moon && moon.eclipse_next_24h;
  if (!line) return;
  line.hidden = !event;
  if (event) setCaelusMoonText('moonEclipse24hText', formatCaelusEclipse(event));
}
function renderUpcomingEclipse(moon){
  const eclipses = moon && Array.isArray(moon.next_eclipses) ? moon.next_eclipses : [];
  setCaelusMoonText('caelusUpcomingEclipseText', eclipses.length ? formatCaelusEclipse(eclipses[0]) : 'No visible eclipse in the next 12 months');
}
function positionCaelusTimelineMarker(markerId, eventAt, startAt, endAt){
  const marker = document.getElementById(markerId);
  if (!marker) return;
  const eventMs = Date.parse(String(eventAt || ''));
  const startMs = Date.parse(String(startAt || ''));
  const endMs = Date.parse(String(endAt || ''));
  const available = Number.isFinite(eventMs) && Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs && eventMs >= startMs && eventMs <= endMs;
  marker.classList.toggle('is-unavailable', !available);
  if (!available) return;
  const percent = Math.max(0, Math.min(100, ((eventMs - startMs) / (endMs - startMs)) * 100));
  marker.style.setProperty('--timeline-left', `${percent.toFixed(2)}%`);
}
function renderCaelusPhaseSide(containerId, title, phases){
  const container = document.getElementById(containerId);
  if (!container) return;
  container.replaceChildren();
  const heading = document.createElement('p');
  heading.className = 'caelus-lunar-side-title';
  heading.textContent = title;
  container.appendChild(heading);
  (Array.isArray(phases) ? phases.slice(0, 4) : []).forEach((phase) => {
    const step = document.createElement('div');
    step.className = 'caelus-lunar-step';
    const canvas = document.createElement('canvas');
    canvas.width = 96;
    canvas.height = 96;
    canvas.dataset.phaseMoon = '';
    canvas.dataset.phaseIndex = String(phase.index == null ? '' : phase.index);
    const name = document.createElement('small');
    name.textContent = phase.name || 'Moon phase';
    const date = document.createElement('time');
    date.dateTime = phase.representative_date || '';
    date.textContent = phase.date_label || phase.representative_date || '--';
    step.append(canvas, name, date);
    container.appendChild(step);
    if (window.CaelusMoon) window.CaelusMoon.renderMoonDisk(canvas, phase);
  });
}
function renderCaelusMoonPopup(moon){
  if (!moon || typeof moon !== 'object') return;
  window.caelusMoonPopupData = moon;
  renderCaelusPhaseSide('caelusPreviousPhases', 'Previous phases', moon.previous_phases);
  renderCaelusPhaseSide('caelusUpcomingPhases', 'Upcoming phases', moon.upcoming_phases);
  const currentCanvas = document.getElementById('caelusCurrentMoonDisk');
  if (currentCanvas && window.CaelusMoon) window.CaelusMoon.renderMoonDisk(currentCanvas, moon);
  setCaelusMoonText('caelusCurrentMoonName', moon.name || 'Moon');
  setCaelusMoonText('caelusCurrentMoonIllumination', moon.illumination == null ? '--' : `${moon.illumination}%`);
  setCaelusMoonText('caelusCurrentMoonAge', moon.age_days);
  updateCaelusMoonViewLabel(moon);
  renderDashboardEclipse(moon);
  renderUpcomingEclipse(moon);
  setCaelusMoonText('caelusMoonSunrise', moon.sunrise);
  setCaelusMoonText('caelusMoonSunset', moon.sunset);
  setCaelusMoonText('caelusNextSunrise', moon.next_sunrise);
  setCaelusMoonText('caelusMoonrise', moon.timeline_moonrise);
  setCaelusMoonText('caelusMoonset', moon.timeline_moonset);
  positionCaelusTimelineMarker('caelusSunsetMarker', moon.timeline_sunset_at, moon.timeline_start_at, moon.timeline_end_at);
  positionCaelusTimelineMarker('caelusMoonriseMarker', moon.timeline_moonrise_at, moon.timeline_start_at, moon.timeline_end_at);
  positionCaelusTimelineMarker('caelusMoonsetMarker', moon.timeline_moonset_at, moon.timeline_start_at, moon.timeline_end_at);
  const updated = String(moon.updated_at || '');
  setCaelusMoonText('caelusMoonUpdated', updated.length >= 16 ? `Updated ${updated.slice(11, 16)} UTC` : 'Updated now');
  setCaelusMoonText('caelusMoonStatus', 'Observer-local phase timeline');
}
let caelusAstronomyPromise = null;
function loadCaelusAstronomy(){
  if (!caelusAstronomyPromise) {
    caelusAstronomyPromise = fetch('/api/weather-forecast-app/astronomy', {cache:'no-store'}).then((response) => { if (!response.ok) throw new Error(`astronomy_${response.status}`); return response.json(); }).catch((error) => { caelusAstronomyPromise = null; throw error; });
  }
  return caelusAstronomyPromise;
}
async function refreshCaelusAstronomy(){
  caelusAstronomyPromise = null;
  const moon = await loadCaelusAstronomy();
  renderDashboardEclipse(moon);
  renderUpcomingEclipse(moon);
  if (caelusMoonIsOpen()) renderCaelusMoonPopup(moon);
  return moon;
}
async function openCaelusMoonPhases(){
  const overlay = document.getElementById('caelusMoonOverlay');
  const dialog = document.getElementById('caelusMoonDialog');
  if (!overlay || !dialog) return;
  overlay.classList.add('is-open');
  overlay.setAttribute('aria-hidden', 'false');
  setCaelusMoonText('caelusMoonStatus', 'Loading observer-local Moon phases…');
  try { if (dialog) dialog.focus({preventScroll:true}); } catch (_) { dialog.focus(); }
  try {
    renderCaelusMoonPopup(await loadCaelusAstronomy());
  } catch (error) {
    console.error('Failed to load Caelus Moon phases', error);
    setCaelusMoonText('caelusMoonStatus', 'Moon phase timeline unavailable');
  }
}
function closeCaelusMoonPhases(){
  const overlay = document.getElementById('caelusMoonOverlay');
  if (overlay) { overlay.classList.remove('is-open'); overlay.setAttribute('aria-hidden', 'true'); }
  const moonButton = document.getElementById('moonCalendarBtn');
  try { if (moonButton) moonButton.focus({preventScroll:true}); } catch (_) { if (moonButton) moonButton.focus(); }
}
window.openCaelusMoonPhases = openCaelusMoonPhases;
window.closeCaelusMoonPhases = closeCaelusMoonPhases;
document.addEventListener('DOMContentLoaded', function(){ refreshCaelusAstronomy().catch((error) => console.error('Failed to load eclipse information', error)); });

document.addEventListener('click', function(ev){
  const target = ev.target instanceof Element ? ev.target : null;
  if (!target) return;
  if (target.closest('#caelusMoonClose') || target.id === 'caelusMoonOverlay') {
    ev.preventDefault();
    closeCaelusMoonPhases();
    return;
  }
  const closeCard = target.closest('#sunMoon29Card');
  if (closeCard) {
    ev.preventDefault();
    ev.stopPropagation();
    closeSunMoon29Day();
    return;
  }
  if (target.closest('#moonCalendarBtn')) {
    ev.preventDefault();
    openCaelusMoonPhases();
    return;
  }
  if (target.closest('.at-a-glance-order-controls')) return;
  if (target.closest('#sunBox')) {
    ev.preventDefault();
    openSunMoon29Day();
  }
});

document.addEventListener('keydown', function(ev){
  const target = ev.target instanceof Element ? ev.target : null;
  if (ev.key === 'Escape' && caelusMoonIsOpen()) {
    ev.preventDefault();
    closeCaelusMoonPhases();
    return;
  }
  if (ev.key === 'Escape' && sunMoon29IsOpen()) {
    ev.preventDefault();
    closeSunMoon29Day();
    return;
  }
  if (!target || (ev.key !== 'Enter' && ev.key !== ' ')) return;
  if (target.closest('#sunMoon29Card')) {
    ev.preventDefault();
    closeSunMoon29Day();
    return;
  }
  if (target.closest('.at-a-glance-order-controls')) return;
  if (target.closest('#sunBox')) {
    ev.preventDefault();
    openSunMoon29Day();
  }
});
function getMoonViewMode(){
  if (window.CaelusMoon && typeof window.CaelusMoon.getViewMode === 'function') return window.CaelusMoon.getViewMode();
  try { return window.localStorage.getItem('sensorius.moonViewMode') === 'reference' ? 'reference' : 'local'; } catch (_) {}
  return 'local';
}

function updateCaelusMoonViewLabel(moon){
  if (getMoonViewMode() === 'reference'){ setCaelusMoonText('caelusMoonLocalView', 'Reference orientation · lunar north up'); return; }
  const altitude = Number(moon && moon.moon_altitude);
  setCaelusMoonText('caelusMoonLocalView', Number.isFinite(altitude) ? `Observer-local orientation · ${altitude}° altitude${altitude < 0 ? ' (below horizon)' : ''}` : 'Observer-local orientation');
}

function setMoonViewMode(mode){
  const isReference = mode === 'reference';
  document.querySelectorAll('[data-moon-view]').forEach((button) => {
    const active = (button.getAttribute('data-moon-view') === 'reference') === isReference;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  if (window.CaelusMoon && typeof window.CaelusMoon.setViewMode === 'function') window.CaelusMoon.setViewMode(isReference ? 'reference' : 'local');
  else { try { window.localStorage.setItem('sensorius.moonViewMode', isReference ? 'reference' : 'local'); } catch (_) {} }
  updateCaelusMoonViewLabel(window.caelusMoonPopupData);
}

const dashboardMoonSurfaceImage = new Image();
dashboardMoonSurfaceImage.addEventListener('load', function(){ drawMoonPhase(astroData); });
dashboardMoonSurfaceImage.src = '/ui_static/weather_forecast/moon-surface.png?v=1';

function drawMoonPhase(data){
  updateAstronomyDates(data);
  const c = document.getElementById('moonPhaseCanvas');
  const meta = document.getElementById('moonMeta');
  const riseEl = document.getElementById('moonRiseTime');
  const setEl = document.getElementById('moonSetTime');
  const litEl = document.getElementById('moonLitPct');
  const nextPhaseLabelEl = document.getElementById('moonNextPhaseLabel');
  const nextPhaseDateEl = document.getElementById('moonNextPhaseDate');
  if (!c || !meta || !riseEl || !setEl || !litEl || !nextPhaseLabelEl || !nextPhaseDateEl) return;
  if (typeof setAstroCardsLoading === 'function') setAstroCardsLoading(isDashboardWarmingPayload(data));
  const ctx = c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height);
  const fmtRaw = (v) => (typeof v === 'string' && v.trim() ? v.trim() : '--');
  const fmtMoonTime = (v) => {
    const s = fmtRaw(v);
    const m = s.match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return s;
    const hh = parseInt(m[1], 10);
    const mm = m[2];
    const ap = hh < 12 ? 'A' : 'P';
    const h12 = (hh % 12) || 12;
    return `${h12}:${mm}${ap}`;
  };
  const fmtMoonDate = (v) => {
    const s = fmtRaw(v);
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return s;
    const yy = m[1].slice(-2);
    const mon = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][Math.max(0, Math.min(11, parseInt(m[2],10)-1))];
    return `${m[3]}${mon}${yy}`;
  };
  const setMoonNextPhaseLabel = (rawLabel) => {
    const label = fmtRaw(rawLabel) === '--' ? 'Next Phase' : fmtRaw(rawLabel);
    nextPhaseLabelEl.replaceChildren();
    const namedFullMoon = label !== 'New Moon' && /\sMoon$/.test(label);
    if (!namedFullMoon){
      nextPhaseLabelEl.textContent = label;
      return;
    }
    const descriptor = label.replace(/\s+Moon$/, '');
    nextPhaseLabelEl.append(document.createTextNode(descriptor));
    nextPhaseLabelEl.append(document.createElement('br'));
    nextPhaseLabelEl.append(document.createTextNode('Moon'));
  };
  if (!data || !data.ok || typeof data.moon_phase_value !== 'number'){
    meta.textContent = 'Moon data unavailable';
    riseEl.textContent = '--';
    setEl.textContent = '--';
    litEl.textContent = '--';
    nextPhaseLabelEl.textContent = 'Next Phase';
    nextPhaseDateEl.textContent = '--';
    return;
  }
  const w = c.width, h = c.height;
  const r = Math.min(w, h) / 2 - 1;
  const x = w / 2, y = h / 2;
  const phase = ((data.moon_phase_value % 28) + 28) % 28;
  const illum = 0.5 * (1 - Math.cos((2*Math.PI*phase)/28));
  const lat = Number(data.lat || 0);
  const hemisphereFlip = lat < 0 ? -1 : 1;
  const phaseAngle = (2 * Math.PI * phase) / 28;
  const rawVisibleAngle = data ? data.moon_visible_angle : null;
  const visibleAngle = (typeof rawVisibleAngle === 'number' && Number.isFinite(rawVisibleAngle)) ? rawVisibleAngle : NaN;
  const hasVisibleAngle = Number.isFinite(visibleAngle);
  const rawReferenceAngle = data ? data.moon_reference_angle : null;
  const referenceAngle = (typeof rawReferenceAngle === 'number' && Number.isFinite(rawReferenceAngle)) ? rawReferenceAngle : NaN;
  const hasReferenceAngle = Number.isFinite(referenceAngle);
  const moonViewMode = getMoonViewMode();
  const isReferenceMode = moonViewMode === 'reference';
  const useVisibleAngle = !isReferenceMode && hasVisibleAngle;
  const useReferenceAngle = isReferenceMode && hasReferenceAngle;
  const limbStrength = Math.abs(Math.sin(phaseAngle));
  const sourceAngleDeg = useVisibleAngle ? visibleAngle : (isReferenceMode ? 0 : (hemisphereFlip < 0 ? -60 : 60));
  const rotationDeg = sourceAngleDeg;
  if (useVisibleAngle) c.setAttribute('data-visible-angle', visibleAngle.toFixed(2)); else c.removeAttribute('data-visible-angle');
  if (useReferenceAngle) c.setAttribute('data-reference-angle', referenceAngle.toFixed(2)); else c.removeAttribute('data-reference-angle');
  const moonPhaseRiseRaw = pickMoonEventRaw(data && data.moon_rise_today, data && data.moon_rise);
  const moonPhaseSetRaw = pickMoonEventRaw(data && data.moon_set_today, data && data.moon_set);
  const sx = limbStrength;
  const sz = -Math.cos(phaseAngle);
  const phaseCanvas = document.createElement('canvas');
  phaseCanvas.width = w; phaseCanvas.height = h;
  const phaseCtx = phaseCanvas.getContext('2d');
  const image = phaseCtx.createImageData(w, h);
  const pix = image.data;
  let surfacePixels = null;
  if (dashboardMoonSurfaceImage.complete && dashboardMoonSurfaceImage.naturalWidth > 0){
    const surfaceCanvas = document.createElement('canvas');
    surfaceCanvas.width = w; surfaceCanvas.height = h;
    const surfaceCtx = surfaceCanvas.getContext('2d', {willReadFrequently:true});
    if (surfaceCtx){
      surfaceCtx.drawImage(dashboardMoonSurfaceImage, 0, 0, w, h);
      surfacePixels = surfaceCtx.getImageData(0, 0, w, h).data;
    }
  }
  for (let py = 0; py < h; py++) {
    for (let px = 0; px < w; px++) {
      const dx = (px + 0.5 - x) / r;
      const dy = (py + 0.5 - y) / r;
      const rr = dx*dx + dy*dy;
      const off = (py * w + px) * 4;
      if (rr > 1) { pix[off+3] = 0; continue; }
      const dz = Math.sqrt(Math.max(0, 1 - rr));
      const dot = (dx * sx) + (dz * sz);
      const edge = Math.max(-1, Math.min(1, dot / 0.06));
      const blend = (edge + 1) * 0.5;
      const litMix = Math.pow(blend, 0.82);
      const rim = Math.pow(Math.max(0, dz), 0.65);
      if (surfacePixels){
        const textureRadius = Math.min(w, h) * 0.44;
        const sourceX = Math.max(0, Math.min(w - 1, Math.round(x + dx * textureRadius)));
        const sourceY = Math.max(0, Math.min(h - 1, Math.round(y + dy * textureRadius)));
        const sourceOff = (sourceY * w + sourceX) * 4;
        const brightness = 0.035 + litMix * (0.9 + dz * 0.065);
        pix[off+0] = Math.min(255, Math.round(surfacePixels[sourceOff] * brightness));
        pix[off+1] = Math.min(255, Math.round(surfacePixels[sourceOff+1] * brightness));
        pix[off+2] = Math.min(255, Math.round(surfacePixels[sourceOff+2] * brightness + (1 - litMix) * 5));
      } else {
        const darkR = 74, darkG = 78, darkB = 86;
        const litR = 244, litG = 242, litB = 234;
        const bodyR = darkR + ((litR - darkR) * litMix);
        const bodyG = darkG + ((litG - darkG) * litMix);
        const bodyB = darkB + ((litB - darkB) * litMix);
        const rimBoost = 0.12 + (0.10 * rim);
        pix[off+0] = Math.round(Math.max(0, Math.min(255, bodyR + (litMix * 10) + (rimBoost * 18))));
        pix[off+1] = Math.round(Math.max(0, Math.min(255, bodyG + (litMix * 9) + (rimBoost * 16))));
        pix[off+2] = Math.round(Math.max(0, Math.min(255, bodyB + (litMix * 7) + (rimBoost * 10))));
      }
      const edgeDistance = (1 - Math.sqrt(rr)) * r;
      pix[off+3] = Math.round(255 * Math.max(0, Math.min(1, edgeDistance / 1.35)));
    }
  }
  phaseCtx.putImageData(image, 0, 0);
  const maria = [{x:-0.28,y:-0.24,r:0.22,a:0.15},{x:0.06,y:-0.1,r:0.17,a:0.12},{x:-0.12,y:0.18,r:0.2,a:0.11},{x:0.26,y:0.12,r:0.12,a:0.1},{x:0.18,y:-0.34,r:0.1,a:0.1}];
  phaseCtx.save();
  phaseCtx.translate(x, y);
  phaseCtx.beginPath();
  phaseCtx.arc(0, 0, r, 0, Math.PI * 2);
  phaseCtx.clip();
  for (const m of (surfacePixels ? [] : maria)) {
    phaseCtx.fillStyle = `rgba(88, 92, 100, ${m.a})`;
    phaseCtx.beginPath();
    phaseCtx.arc(m.x * r, m.y * r, m.r * r, 0, Math.PI * 2);
    phaseCtx.fill();
  }
  phaseCtx.restore();
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate((rotationDeg * Math.PI) / 180);
  ctx.drawImage(phaseCanvas, -x, -y);
  ctx.restore();
  const litPct = Number.isFinite(Number(data.moon_lit_pct)) ? `${Math.round(Number(data.moon_lit_pct))}%` : `${(illum*100).toFixed(0)}%`;
  meta.textContent = `${data.moon_phase_label || 'Moon'} • ${isReferenceMode ? 'Reference diagram' : 'Local sky view'}`;
  riseEl.textContent = fmtMoonTime(moonPhaseRiseRaw);
  setEl.textContent = fmtMoonTime(moonPhaseSetRaw);
  litEl.textContent = litPct;
  setMoonNextPhaseLabel(data.moon_next_phase_label);
  nextPhaseDateEl.textContent = fmtMoonDate(data.moon_next_phase_date);
}

document.addEventListener('click', function(ev){
  const btn = ev.target instanceof Element ? ev.target.closest('[data-moon-view]') : null;
  if (!btn) return;
  const mode = btn.getAttribute('data-moon-view') === 'reference' ? 'reference' : 'local';
  setMoonViewMode(mode);
  if (typeof astroData !== 'undefined') drawMoonPhase(astroData);
});
window.addEventListener('sensorius:moon-view-change', function(ev){
  const mode = ev.detail && ev.detail.mode === 'reference' ? 'reference' : 'local';
  document.querySelectorAll('[data-moon-view]').forEach((button) => { const active = button.getAttribute('data-moon-view') === mode; button.classList.toggle('active', active); button.setAttribute('aria-pressed', active ? 'true' : 'false'); });
  updateCaelusMoonViewLabel(window.caelusMoonPopupData);
  if (typeof astroData !== 'undefined') drawMoonPhase(astroData);
});
document.addEventListener('DOMContentLoaded', function(){ setMoonViewMode(getMoonViewMode()); });

function forecastEsc(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => {
    if (ch === '&') return '&amp;';
    if (ch === '<') return '&lt;';
    if (ch === '>') return '&gt;';
    if (ch === '"') return '&quot;';
    return '&#39;';
  });
}
function forecastProviderLabel(data){
  const provider = String((data && data.provider) || '').trim();
  if (provider === 'met_no') return 'MET Norway';
  if (provider === 'open_meteo') return 'Open-Meteo';
  if (provider === 'us') return 'US';
  if (provider === 'none') return 'None';
  return provider || '';
}
function forecastUnavailableText(data){
  const reason = String((data && data.reason) || '').trim();
  if (reason === 'provider_disabled') return 'Weather forecast disabled.';
  if (reason === 'location_unavailable') return 'Set Astral location to enable forecast.';
  if (reason === 'provider_unavailable') return 'Forecast provider unavailable.';
  if (reason === 'forecast_failed') return 'Forecast unavailable.';
  return reason ? `Forecast unavailable: ${reason}` : 'Forecast unavailable.';
}
function setForecastText(id, value){
  const el = document.getElementById(id);
  if (el) el.textContent = value || '--';
}
function forecastCurrentTemperature(data){
  const readings = (data && data.current_readings && typeof data.current_readings === 'object') ? data.current_readings : null;
  if (!readings || !readings.ok || !readings.sensor_id) return '';
  const metric = String((data && data.unit_system) || '').toLowerCase() === 'metric';
  const rawValue = metric ? readings.temperature_c : readings.temperature_f;
  if (rawValue === null || rawValue === undefined || rawValue === '') return '';
  const value = Number(rawValue);
  return Number.isFinite(value) ? `${value.toFixed(1)}${metric ? '°C' : '°F'}` : '';
}
function forecastCurrentHumidity(data){
  const readings = (data && data.current_readings && typeof data.current_readings === 'object') ? data.current_readings : null;
  if (!readings || !readings.ok || !readings.sensor_id) return '';
  if (readings.humidity === null || readings.humidity === undefined || readings.humidity === '') return '';
  const value = Number(readings.humidity);
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : '';
}
function forecastReadingAndRange(currentValue, forecastRange){
  const range = String(forecastRange || '').trim() || '--';
  return currentValue ? `${currentValue} : ${range}` : range;
}
function forecastWindParts(value){
  const text = String(value || '').trim();
  if (!text || text === '--') return { description:'--', speeds:'' };
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length >= 2) return { description:lines[0].replace(/,\s*$/, ''), speeds:lines.slice(1).join(' ') };
  const m = text.match(/^(.*?)(?:,\s*)?(~?\d+(?:[-–]\d+)?\s*m\/s\s*\/\s*.+)$/i);
  if (m) return { description:String(m[1] || '').trim().replace(/,\s*$/, '') || '--', speeds:String(m[2] || '').trim() };
  const idx = text.search(/~?\d+(?:[-–]\d+)?\s*m\/s/i);
  if (idx > 0) return { description:text.slice(0, idx).trim().replace(/,\s*$/, '') || '--', speeds:text.slice(idx).trim() };
  return { description:text.replace(/,\s*$/, ''), speeds:'' };
}
function forecastWindHtml(value){
  const parts = forecastWindParts(value);
  const desc = forecastEsc(parts.description || '--');
  const speeds = String(parts.speeds || '').trim();
  return speeds ? `<span class='forecast-wind-line'>${desc}</span><span class='forecast-wind-line'>${forecastEsc(speeds)}</span>` : `<span class='forecast-wind-line'>${desc}</span>`;
}
function setForecastWind(value){
  const el = document.getElementById('forecastWind');
  if (el) el.innerHTML = forecastWindHtml(value || '--');
}
function forecastPrecipitationKind(data, cur){
  const rows = Array.isArray(data && data.hourly) ? data.hourly : [];
  const text = [cur && (cur.overall || cur.forecast), ...rows.map((row) => row && row.symbol)].join(' ').toLowerCase();
  return /snow|sleet/.test(text) ? 'Snow' : 'Rain';
}
function setForecastPrecipitation(data, cur){
  const label = document.getElementById('forecastPrecipLabel');
  const rawChance = cur && cur.precip_probability;
  const chance = Number(rawChance);
  if (label) label.textContent = forecastPrecipitationKind(data, cur);
  setForecastText('forecastPrecipChance', rawChance !== null && rawChance !== undefined && rawChance !== '' && Number.isFinite(chance) ? `${Math.round(Math.max(0, Math.min(100, chance)))}% chance` : '--');
}
function forecastClockMinutes(value){
  const match = String(value || '').match(/(?:T|^)(\d{1,2}):(\d{2})/);
  if (!match) return NaN;
  return (Number(match[1]) * 60) + Number(match[2]);
}
function forecastStationMinutes(data){
  const timezoneName = String((data && data.location && data.location.timezone) || '').trim();
  if (!timezoneName) return NaN;
  try {
    const parts = new Intl.DateTimeFormat('en-US', {timeZone:timezoneName,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date());
    const hour = Number((parts.find((part) => part.type === 'hour') || {}).value);
    const minute = Number((parts.find((part) => part.type === 'minute') || {}).value);
    return Number.isFinite(hour) && Number.isFinite(minute) ? ((hour % 24) * 60) + minute : NaN;
  } catch (_) {
    return NaN;
  }
}
function forecastMinuteInWindow(minutes, start, end){
  return start <= end ? minutes >= start && minutes < end : minutes >= start || minutes < end;
}
function forecastSceneFor(data){
  const rows = Array.isArray(data && data.hourly) ? data.hourly.slice(0, 3) : [];
  if (!rows.length) return 'pending';
  const symbols = rows.map((row) => String((row && row.symbol) || '').toLowerCase());
  const symbolText = symbols.join(' ');
  const precipitation = rows.reduce((total, row) => total + (Number(row && row.precip_mm) || 0), 0);
  const cloudValues = rows.map((row) => Number(row && row.cloud)).filter(Number.isFinite);
  const averageCloud = cloudValues.length ? cloudValues.reduce((total, value) => total + value, 0) / cloudValues.length : 0;
  let condition = 'clear';
  if (precipitation > 0.05 || /rain|shower|drizzle|thunder|sleet|snow/.test(symbolText)) condition = 'rain';
  else if (averageCloud >= 45 || /cloud|overcast|fog|mist/.test(symbolText)) condition = 'cloudy';
  const middle = rows[Math.min(1, rows.length - 1)] || {};
  const middleSymbol = String(middle.symbol || '').toLowerCase();
  const middleLocalMinutes = forecastClockMinutes(middle.local_time);
  const fallbackMinutes = Number(middle.local_hour) * 60;
  const stationMinutes = forecastStationMinutes(data);
  const minutes = Number.isFinite(stationMinutes) ? stationMinutes : (Number.isFinite(middleLocalMinutes) ? middleLocalMinutes : fallbackMinutes);
  const sunrise = forecastClockMinutes(astroData && astroData.sunrise);
  const sunset = forecastClockMinutes(astroData && astroData.sunset);
  let isNight;
  if (Number.isFinite(sunrise) && Number.isFinite(sunset) && Number.isFinite(minutes)) {
    const daybreak = (sunrise - 30 + 1440) % 1440;
    const dusk = (sunset + 30) % 1440;
    isNight = !forecastMinuteInWindow(minutes, daybreak, dusk);
  } else if (/_night\b|\bnight\b/.test(middleSymbol)) isNight = true;
  else if (/_day\b|\bday\b/.test(middleSymbol)) isNight = false;
  else isNight = !(Number.isFinite(minutes) && minutes >= 7 * 60 && minutes < 19 * 60);
  return `${condition}-${isNight ? 'night' : 'day'}`;
}
function applyForecastScene(data){
  const card = document.getElementById('weatherForecastBox');
  if (!card) return;
  Array.from(card.classList).filter((name) => name.startsWith('forecast-scene-')).forEach((name) => card.classList.remove(name));
  const scene = forecastSceneFor(data);
  card.classList.add(`forecast-scene-${scene}`);
  card.dataset.forecastScene = scene;
}
window.setInterval(function(){ if (window.__weatherForecastPayload) applyForecastScene(window.__weatherForecastPayload); }, 60000);
function renderWeatherForecast(data){
  window.__weatherForecastPayload = data || null;
  updateAstronomyDates(astroData);
  applyForecastScene(data);
  const statusEl = document.getElementById('forecastStatus');
  const cur = (data && data.current_24h && typeof data.current_24h === 'object') ? data.current_24h : null;
  if (!data || !data.ok || !cur) {
    if (statusEl) statusEl.textContent = 'Unavailable';
    setForecastText('forecastOverall', forecastUnavailableText(data));
    setForecastText('forecastTempRange', '--');
    setForecastWind('--');
    setForecastText('forecastRhRange', '--');
    setForecastPrecipitation(data, null);
    if (typeof bioRefreshOpenSummary === 'function') bioRefreshOpenSummary();
    return;
  }
  const provider = forecastProviderLabel(data);
  const cacheText = data.stale ? 'Cached' : 'Live';
  if (statusEl) statusEl.textContent = provider ? `${cacheText} - ${provider}` : cacheText;
  setForecastText('forecastOverall', cur.synopsis || cur.overall || cur.forecast || '--');
  setForecastText('forecastTempRange', forecastReadingAndRange(forecastCurrentTemperature(data), cur.temp_range));
  setForecastWind(cur.wind || '--');
  setForecastText('forecastRhRange', forecastReadingAndRange(forecastCurrentHumidity(data), cur.rh_range));
  setForecastPrecipitation(data, cur);
  if (typeof bioRefreshOpenSummary === 'function') bioRefreshOpenSummary();
}
function setWeatherForecastCardLoading(isLoading){
  if (typeof setDashboardCardLoading === 'function') setDashboardCardLoading('weatherForecastBox', isLoading);
}
async function loadWeatherForecast(forceRefresh, backgroundRefresh){
  const isBackgroundRefresh = !!backgroundRefresh;
  if (!isBackgroundRefresh) setWeatherForecastCardLoading(true);
  try {
    const url = forceRefresh ? '/api/weather-forecast?days=1&force_refresh=true' : '/api/weather-forecast?days=1';
    const resp = await fetch(url, { cache:'no-store' });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(`forecast_${resp.status}`);
    renderWeatherForecast(payload);
  } catch (e) {
    console.error('Failed to load weather forecast', e);
    if (!isBackgroundRefresh) renderWeatherForecast({ ok:false, reason:'forecast_failed' });
  } finally {
    if (!isBackgroundRefresh) setWeatherForecastCardLoading(false);
  }
}
window.loadWeatherForecast = loadWeatherForecast;
function setForecastButtonLoading(isLoading){
  const btn = document.getElementById('forecastFiveDayBtn');
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle('loading', !!isLoading);
  btn.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}
function forecastMetaText(data){
  if (!data || !data.ok) return forecastUnavailableText(data);
  const provider = forecastProviderLabel(data) || 'forecast provider';
  const loc = (data.location && typeof data.location === 'object') ? data.location : {};
  const lat = Number(loc.latitude);
  const lon = Number(loc.longitude);
  const coord = (Number.isFinite(lat) && Number.isFinite(lon)) ? `${lat.toFixed(4)}, ${lon.toFixed(4)}` : '';
  const stale = data.stale ? 'Cached forecast' : 'Forecast';
  return `${stale} from ${provider}${coord ? ` - ${coord}` : ''}`;
}
function renderForecastDayCard(day){
  const label = forecastEsc(day && (day.label || day.date) || 'Day');
  const summary = forecastEsc(day && (day.forecast || day.overall) || '--');
  const temp = forecastEsc(day && day.temp_range || '--');
  const wind = forecastWindHtml(day && day.wind || '--');
  const rh = forecastEsc(day && day.rh_range || '--');
  return `<section class='forecast-day'><div class='forecast-day-label'>${label}</div><div class='forecast-day-summary'>${summary}</div><dl class='forecast-day-grid'><dt>Temp</dt><dd>${temp}</dd><dt>RH</dt><dd>${rh}</dd><dt>Wind</dt><dd class='forecast-wind-value'>${wind}</dd></dl></section>`;
}
window.openWeatherForecastModal = function(){
  setForecastButtonLoading(true);
  window.location.assign('/weather-forecast');
};

function drawBiodynamic(data){
  const signEl = document.getElementById('bioCurrentSign');
  const elementEl = document.getElementById('bioCurrentElement');
  const openBtn = document.getElementById('bioOpenBtn');
  const boxEl = document.getElementById('bioBox');
  const panelEl = document.getElementById('bioCurrentPanel');
  const dateEl = document.getElementById('bioDateLine');
  const windowEl = document.getElementById('bioWindow');
  const daylightEl = document.getElementById('bioDaylightLine');
  const upcomingEl = document.getElementById('bioUpcoming');
  if (!signEl || !elementEl || !openBtn || !boxEl || !panelEl || !dateEl || !windowEl || !daylightEl || !upcomingEl) return;
  const titleEl = boxEl.querySelector('.astro-title');
  boxEl.style.setProperty('--at-a-glance-arrow-color', '#27313a');
  const biodynamicWarming = isDashboardWarmingPayload(data);
  if (typeof setBiodynamicCardLoading === 'function') setBiodynamicCardLoading(biodynamicWarming);
  const tzName = String((data && data.tz) || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const fmtParts = (iso) => {
    if (!iso) return null;
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return null;
    const dtf = new Intl.DateTimeFormat('en-CA', {
      timeZone: tzName,
      weekday: 'long',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: 'numeric',
      minute: '2-digit',
      hour12: false,
    });
    const parts = dtf.formatToParts(d);
    const out = {};
    for (const part of parts) {
      if (part.type !== 'literal') out[part.type] = part.value;
    }
    return out;
  };
  const fmtIsoHm = (iso) => {
    const parts = fmtParts(iso);
    if (!parts || !parts.hour || !parts.minute) return '--';
    let hour24 = parseInt(parts.hour, 10);
    if (!Number.isFinite(hour24)) return '--';
    if (hour24 >= 24) hour24 = 0;
    const hour12 = ((hour24 + 11) % 12) + 1;
    const suffix = hour24 >= 12 ? 'PM' : 'AM';
    return `${hour12}:${parts.minute} ${suffix}`;
  };
  const fmtHm = (hm) => {
    const m = String(hm || '').match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return '--';
    let hour24 = parseInt(m[1], 10);
    if (!Number.isFinite(hour24)) return '--';
    if (hour24 >= 24) hour24 = 0;
    const hour12 = ((hour24 + 11) % 12) + 1;
    const suffix = hour24 >= 12 ? 'PM' : 'AM';
    return `${hour12}:${m[2]} ${suffix}`;
  };
  const fmtIsoDate = (iso) => {
    const parts = fmtParts(iso);
    if (!parts || !parts.year || !parts.month || !parts.day) return '--';
    const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    const monthName = months[Math.max(0, Math.min(11, parseInt(parts.month, 10) - 1))] || '--';
    return `${parts.weekday || '--'}, ${monthName} ${parts.day}, ${parts.year}`;
  };
  const isoDayKey = (iso) => {
    const parts = fmtParts(iso);
    if (!parts || !parts.year || !parts.month || !parts.day) return '';
    return `${parts.year}-${parts.month}-${parts.day}`;
  };
  const findBiodynamicDay = (payload, currentIso) => {
    const dayKey = isoDayKey(currentIso);
    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];
    return days.find((d) => d && d.date === dayKey) || null;
  };
  const toMinutes = (hhmm) => {
    const m = String(hhmm || '').match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return null;
    const out = (parseInt(m[1], 10) * 60) + parseInt(m[2], 10);
    return Math.max(0, Math.min(1440, out));
  };
  const bioNowParts = () => {
    const d = new Date();
    const parts = fmtParts(d.toISOString());
    if (!parts || !parts.year || !parts.month || !parts.day || !parts.hour || !parts.minute) return null;
    let hour24 = parseInt(parts.hour, 10);
    const minute = parseInt(parts.minute, 10);
    if (!Number.isFinite(hour24) || !Number.isFinite(minute)) return null;
    if (hour24 >= 24) hour24 = 0;
    return { iso: d.toISOString(), dayKey: `${parts.year}-${parts.month}-${parts.day}`, minuteOfDay: Math.max(0, Math.min(1439, (hour24 * 60) + minute)) };
  };
  const lightenHex = (hex, factor) => {
    const m = String(hex || '').trim().match(/^#?([0-9a-f]{6})$/i);
    if (!m) return String(hex || '#fffdf6');
    const raw = m[1];
    const mix = (v) => {
      const n = parseInt(v, 16);
      return Math.max(0, Math.min(255, Math.round(n + ((255 - n) * factor))));
    };
    const r = mix(raw.slice(0, 2));
    const g = mix(raw.slice(2, 4));
    const b = mix(raw.slice(4, 6));
    return `#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`;
  };
  const textOnHex = (hex) => {
    const m = String(hex || '').trim().match(/^#?([0-9a-f]{6})$/i);
    if (!m) return '#27313a';
    const raw = m[1];
    const toLinear = (pair) => {
      const c = parseInt(pair, 16) / 255;
      return c <= 0.03928 ? (c / 12.92) : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    const r = toLinear(raw.slice(0, 2));
    const g = toLinear(raw.slice(2, 4));
    const b = toLinear(raw.slice(4, 6));
    const luminance = (0.2126 * r) + (0.7152 * g) + (0.0722 * b);
    const contrast = (a, b) => {
      const hi = Math.max(a, b);
      const lo = Math.min(a, b);
      return (hi + 0.05) / (lo + 0.05);
    };
    const darkText = '#27313a';
    const darkRaw = darkText.slice(1);
    const darkLuminance = (0.2126 * toLinear(darkRaw.slice(0, 2))) + (0.7152 * toLinear(darkRaw.slice(2, 4))) + (0.0722 * toLinear(darkRaw.slice(4, 6)));
    return contrast(luminance, darkLuminance) >= contrast(luminance, 1) ? darkText : '#fff';
  };
  const biodynamicActionColors = (item) => {
    const isFruit = String((item && item.plant_part) || '').trim().toLowerCase() === 'fruit';
    const background = isFruit ? '#4c3a7f' : String((item && item.color) || '').trim();
    return { background, text: isFruit ? '#fff' : textOnHex(background) };
  };
  window.biodynamicActionColors = biodynamicActionColors;
  const biodynamicIsLightRest = (item) => {
    const sign = String((item && item.sign) || '').trim().toLowerCase();
    const element = String((item && item.element) || '').trim().toLowerCase();
    const part = String((item && item.plant_part) || '').trim().toLowerCase();
    const kind = String((item && item.kind) || '').trim().toLowerCase();
    return sign === 'rest' || element === 'pause' || part === 'rest' || kind === 'off';
  };
  const biodynamicTextColor = (item) => {
    if (biodynamicIsLightRest(item)) return '#27313a';
    const bgColor = String((item && (item.accent || item.dominant_accent || item.color)) || '');
    return bgColor ? textOnHex(bgColor) : '#fff';
  };
  const clearBiodynamicTextContrast = (els) => {
    els.forEach((el) => {
      if (!el) return;
      el.style.color = '';
      el.style.textShadow = '';
      el.style.backgroundImage = '';
      el.style.backgroundSize = '';
      el.style.backgroundClip = '';
      el.style.webkitBackgroundClip = '';
      el.style.webkitTextFillColor = '';
    });
  };
  const applyBiodynamicTextContrast = (els, fallbackColor, gradient) => {
    els.forEach((el) => {
      if (!el) return;
      el.style.color = fallbackColor;
      el.style.textShadow = 'none';
      if (!gradient) {
        el.style.backgroundImage = '';
        el.style.backgroundSize = '';
        el.style.backgroundClip = '';
        el.style.webkitBackgroundClip = '';
        el.style.webkitTextFillColor = '';
        return;
      }
      el.style.backgroundImage = gradient;
      el.style.backgroundSize = '100% 100%';
      el.style.backgroundClip = 'text';
      el.style.webkitBackgroundClip = 'text';
      el.style.webkitTextFillColor = 'transparent';
    });
  };
  const buildRollingTextGradient = (payload, currentIso) => {
    if (!currentIso) return null;
    const dayKey = isoDayKey(currentIso);
    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];
    const today = days.find((d) => d && d.date === dayKey);
    const segments = Array.isArray(today && today.segments) ? today.segments : [];
    if (!segments.length) return null;
    const stops = [];
    for (const seg of segments) {
      let startMin = toMinutes(seg && seg.start);
      let endMin = toMinutes(seg && seg.end);
      if (!Number.isFinite(startMin)) startMin = 0;
      if (!Number.isFinite(endMin)) endMin = 1440;
      if (endMin <= startMin) endMin = 1440;
      const startPct = ((startMin / 1440) * 100).toFixed(2);
      const endPct = ((endMin / 1440) * 100).toFixed(2);
      const textColor = biodynamicTextColor(seg);
      stops.push(`${textColor} ${startPct}%`, `${textColor} ${endPct}%`);
    }
    return stops.length ? `linear-gradient(90deg, ${stops.join(', ')})` : null;
  };
  const buildRollingGradient = (data, currentIso) => {
    if (!currentIso) return '#ffffe0';
    const dayKey = isoDayKey(currentIso);
    const days = Array.isArray(data && data.calendar) ? data.calendar : [];
    const today = days.find((d) => d && d.date === dayKey);
    const segments = Array.isArray(today && today.segments) ? today.segments : [];
    const fallback = String((today && today.dominant_accent) || '#ffffe0');
    if (!segments.length) { boxEl.style.setProperty('--at-a-glance-arrow-color', textOnHex(fallback)); return fallback; }
    const slices = [];
    const pushSlice = (startMin, endMin, accent) => {
      if (!Number.isFinite(startMin) || !Number.isFinite(endMin) || endMin <= startMin) return;
      slices.push({ start: startMin, end: endMin, color: String(accent || fallback) });
    };
    for (const seg of segments) {
      let startMin = toMinutes(seg && seg.start);
      let endMin = toMinutes(seg && seg.end);
      if (!Number.isFinite(startMin)) startMin = 0;
      if (!Number.isFinite(endMin)) endMin = 1440;
      if (endMin <= startMin) endMin = 1440;
      const accent = String((seg && (seg.accent || lightenHex(seg.color || '#d8d8d8', 0.78))) || fallback);
      pushSlice(startMin, endMin, accent);
    }
    if (!slices.length) return '#ffffe0';
    const stops = [];
    for (const seg of slices) {
      const color = String(seg.color || '#fffdf6');
      const startPct = ((seg.start / 1440) * 100).toFixed(2);
      const endPct = ((seg.end / 1440) * 100).toFixed(2);
      stops.push(`${color} ${startPct}%`, `${color} ${endPct}%`);
    }
    boxEl.style.setProperty('--at-a-glance-arrow-color', textOnHex(slices[slices.length - 1].color));
    return `linear-gradient(90deg, ${stops.join(', ')})`;
  };
  const buildBiodynamicWindowText = (payload, currentIso, current) => {
    const dayKey = isoDayKey(currentIso);
    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];
    const today = days.find((d) => d && d.date === dayKey);
    const segments = Array.isArray(today && today.segments) ? today.segments : [];
    const rows = segments.map((seg) => {
      const sign = String((seg && seg.sign) || '').trim() || '--';
      return `${sign} Moon: ${fmtHm(seg && seg.start)} to ${fmtHm(seg && seg.end)}`;
    }).filter((line) => line && line !== '-- Moon: -- to --');
    const sign = String((current && current.sign) || '').trim() || '--';
    return rows.length ? rows.join('\n') : `${sign} Moon: ${fmtIsoHm(current && current.window_start)} to ${fmtIsoHm(current && current.window_end)}`;
  };
  const findActiveBiodynamicSegment = (payload, fallback) => {
    const nowParts = bioNowParts();
    if (!nowParts) return Object.assign({}, fallback || {});
    const days = Array.isArray(payload && payload.calendar) ? payload.calendar : [];
    const today = days.find((d) => d && d.date === nowParts.dayKey);
    const segments = Array.isArray(today && today.segments) ? today.segments : [];
    for (const seg of segments) {
      let startMin = toMinutes(seg && seg.start);
      let endMin = toMinutes(seg && seg.end);
      if (!Number.isFinite(startMin)) startMin = 0;
      if (!Number.isFinite(endMin)) endMin = 1440;
      if (endMin <= startMin) endMin = 1440;
      if (nowParts.minuteOfDay >= startMin && nowParts.minuteOfDay < endMin) {
        return Object.assign({}, fallback || {}, seg || {}, { timestamp: nowParts.iso, window_start_hm: String((seg && seg.start) || ''), window_end_hm: String((seg && seg.end) || '') });
      }
    }
    return Object.assign({}, fallback || {}, { timestamp: nowParts.iso });
  };
  const buildUnavailableMessage = (payload) => {
    const reason = String((payload && payload.reason) || '').trim();
    const eph = (payload && payload.ephemeris && typeof payload.ephemeris === 'object') ? payload.ephemeris : {};
    if (reason === 'location_unavailable') return 'Set Astral latitude/longitude/timezone to enable biodynamics.';
    if (reason === 'skyfield_not_installed') return 'Skyfield is not installed on this Sensorius host.';
    if (reason === 'warming') return '';
    if (reason.startsWith('ephemeris_download_failed:')) return `Automatic ephemeris download failed. Next retry in ${eph.retry_after_sec || 0}s.`;
    if (reason.startsWith('ephemeris_download_deferred:')) return `Waiting to retry ephemeris download in ${eph.retry_after_sec || 0}s.`;
    return reason ? `Status: ${reason}` : 'No biodynamic data available.';
  };
  const fallbackCurrent = (data && data.current && typeof data.current === 'object') ? data.current : {};
  const cur = findActiveBiodynamicSegment(data || {}, fallbackCurrent);
  if (!data || !data.ok || !cur.sign) {
    signEl.textContent = biodynamicWarming ? 'Loading...' : 'Biodynamics unavailable';
    elementEl.textContent = biodynamicWarming ? 'Moon sign' : 'Automatic ephemeris install is enabled.';
    openBtn.style.background = '#fff7d6';
    openBtn.style.borderColor = '#c6bb8f';
    openBtn.style.color = '#27313a';
    boxEl.style.background = '#ffffe0';
    panelEl.style.background = 'transparent';
    clearBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, daylightEl, upcomingEl]);
    dateEl.textContent = '';
    windowEl.textContent = buildUnavailableMessage(data);
    daylightEl.textContent = '';
    upcomingEl.textContent = '';
    return;
  }
  try { if (cur.timestamp && !document.getElementById('biodynamicCalendarModal')) window.__bioModalState.month = bioMonthKeyFromDate(new Date(cur.timestamp)); } catch (_) {}
  signEl.textContent = `${cur.sign || '--'} Moon`;
  elementEl.textContent = `${cur.element || '--'} / ${cur.plant_part || '--'}`;
  const actionColors = biodynamicActionColors(cur);
  openBtn.style.background = actionColors.background;
  openBtn.style.borderColor = '#27313a';
  openBtn.style.color = actionColors.text;
  boxEl.style.background = buildRollingGradient(data, cur.timestamp);
  panelEl.style.background = 'transparent';
  panelEl.style.borderColor = 'transparent';
  const mainTextColor = biodynamicTextColor(cur);
  const mainTextGradient = buildRollingTextGradient(data, cur.timestamp);
  applyBiodynamicTextContrast([titleEl, signEl, elementEl, dateEl, windowEl, daylightEl, upcomingEl], mainTextColor, mainTextGradient);
  dateEl.textContent = fmtIsoDate(cur.timestamp);
  windowEl.textContent = buildBiodynamicWindowText(data, cur.timestamp, cur);
  daylightEl.textContent = '';
  upcomingEl.textContent = '';
}

window.__bioModalState = window.__bioModalState || { month: '', data: null, selectedDate: '' };
window.__bioMonthCache = window.__bioMonthCache || {};
window.__bioMonthInflight = window.__bioMonthInflight || {};
window.__bioMonthRequestSeq = window.__bioMonthRequestSeq || 0;
window.__bioSummaryRefresh = window.__bioSummaryRefresh || { date: '', attempts: 0, timer: null };
window.__bioNoteDraft = '';
function bioEsc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch] || ch)); }
function bioTodayIso(){ const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function bioMonthKeyFromDate(d){ return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; }
function bioShiftMonth(monthKey, delta){ const m = String(monthKey || '').match(/^(\d{4})-(\d{2})$/); if (!m) return bioMonthKeyFromDate(new Date()); const d = new Date(parseInt(m[1],10), parseInt(m[2],10)-1 + delta, 1); return bioMonthKeyFromDate(d); }
function bioFmtDateLabel(iso){ const d = new Date(`${iso}T00:00:00`); if (!Number.isFinite(d.getTime())) return iso || '--'; return d.toLocaleDateString([], { weekday:'short', month:'short', day:'numeric', year:'numeric' }); }
function bioTextToHtml(s){ return bioEsc(s).replace(/\n/g, '<br>'); }
function bioForecastSummaryText(){
  const data = window.__weatherForecastPayload || null;
  const cur = (data && data.current_24h && typeof data.current_24h === 'object') ? data.current_24h : null;
  const lines = ['24hr Forecast'];
  if (!data || !data.ok || !cur) {
    lines.push(forecastUnavailableText(data));
    return lines.join('\n');
  }
  const overall = String(cur.overall || cur.forecast || '').trim();
  const temp = String(cur.temp_range || '').trim();
  const rh = String(cur.rh_range || '').trim();
  const wind = String(cur.wind || '').trim().replace(/\s*\n\s*/g, ' | ');
  if (overall && overall !== '--') lines.push(`Overall: ${overall}`);
  if (temp && temp !== '--') lines.push(`Temperature: ${temp}`);
  if (rh && rh !== '--') lines.push(`Relative Humidity: ${rh}`);
  if (wind && wind !== '--') lines.push(`Wind: ${wind}`);
  if (Number.isFinite(Number(cur.precip_mm))) lines.push(`Precipitation: ${Number(cur.precip_mm).toFixed(2)} mm`);
  return lines.join('\n');
}
function bioSummaryWithForecast(dateIso, summaryText){
  const parts = [];
  const body = String(summaryText || '').trim();
  if (body) parts.push(body);
  if (window.__weatherForecastEnabled !== false && dateIso && dateIso === bioTodayIso()) parts.push(bioForecastSummaryText());
  return parts.join('\n\n');
}
function bioRefreshOpenSummary(){
  if (!document.getElementById('biodynamicCalendarModal')) return;
  const st = window.__bioModalState || {};
  if (st.selectedDate && typeof bioSelectDate === 'function') bioSelectDate(st.selectedDate);
}
function bioToMinutes(hhmm){
  const m = String(hhmm || '').match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  const out = (parseInt(m[1], 10) * 60) + parseInt(m[2], 10);
  return Math.max(0, Math.min(1440, out));
}
function bioDayBackground(day){
  const fallback = String(day?.dominant_accent || '#fff');
  const segments = Array.isArray(day?.segments) ? day.segments : [];
  if (!segments.length) return fallback;
  const stops = [];
  const dividerStops = [];
  for (const seg of segments) {
    let startMin = bioToMinutes(seg?.start);
    let endMin = bioToMinutes(seg?.end);
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
  if (!stops.length) return fallback;
  const base = `linear-gradient(90deg, ${stops.join(', ')})`;
  if (!dividerStops.length) return base;
  const overlay = `linear-gradient(90deg, ${dividerStops.join(', ')})`;
  return `${overlay}, ${base}`;
}
function bioGetWorkingNotes(){
  const st = window.__bioModalState || {};
  const data = st.data || {};
  const notes = Object.assign({}, (data && data.notes && typeof data.notes === 'object') ? data.notes : {});
  const inputEl = document.getElementById('bioNoteInput');
  if (st.selectedDate && inputEl) notes[st.selectedDate] = String(inputEl.value || '');
  return notes;
}
function renderBiodynamicPrintView(){
  const st = window.__bioModalState || {};
  const data = st.data || {};
  const reportEl = document.getElementById('bioPrintReportSheet');
  if (!reportEl) return;
  const weekdays = Array.isArray(data.weekday_labels) ? data.weekday_labels : ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const days = Array.isArray(data.calendar) ? data.calendar : [];
  const notes = bioGetWorkingNotes();
  const summaries = (data && data.daily_summaries && typeof data.daily_summaries === 'object') ? data.daily_summaries : {};
  if (!days.length) {
    reportEl.innerHTML = `<div class='bio-print-title'>Biodynamic Calendar Report</div><div class='bio-print-subtitle'>No printable calendar data available.</div>`;
    return;
  }
  const grid = weekdays.map((label) => `<div class='bio-print-weekday'>${bioEsc(label)}</div>`).join('') + days.map((day) => {
    const classes = ['bio-print-day'];
    if (!day.in_month) classes.push('out');
    if (day.is_today) classes.push('today');
    const dayBg = bioDayBackground(day);
    return `<div class='${classes.join(' ')}' style='background:${dayBg};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};'><div class='bio-print-day-head'><div class='bio-print-day-num'>${bioEsc(day.day)}</div></div></div>`;
  }).join('');
  const entries = days.filter((day) => day && day.in_month).map((day) => {
    const isSelected = st.selectedDate && st.selectedDate === day.date;
    const entryClasses = ['bio-print-entry'];
    if (isSelected) entryClasses.push('selected');
    const noteText = String(notes[day.date] || '');
    const summaryText = bioSummaryWithForecast(day.date, summaries[day.date] || '');
    const noteHtml = noteText.trim() ? bioTextToHtml(noteText) : '<span>None</span>';
    const summaryHtml = summaryText.trim() ? bioTextToHtml(summaryText) : '<span>None</span>';
    return `<section class='${entryClasses.join(' ')}'><div class='bio-print-entry-head'><div class='bio-print-entry-date'>${bioEsc(bioFmtDateLabel(day.date))}</div><div class='bio-print-entry-meta'>${bioEsc(day.dominant_sign || '--')} / ${bioEsc(day.dominant_plant_part || '--')}</div></div><div class='bio-print-label'>Daily Summary</div><div class='bio-print-block'>${summaryHtml}</div><div class='bio-print-label'>Notes</div><div class='bio-print-block'>${noteHtml}</div></section>`;
  }).join('');
  const selectedMsg = st.selectedDate ? `Selected day: ${bioEsc(bioFmtDateLabel(st.selectedDate))}. Unsaved text for that note is included.` : 'No day selected.';
  reportEl.innerHTML = `<div class='bio-print-title'>Biodynamic Calendar Report</div><div class='bio-print-subtitle'>${bioEsc(data.month_label || '--')} | ${selectedMsg}</div><div class='bio-print-section-title'>Calendar</div><div class='bio-print-calendar'>${grid}</div><div class='bio-print-section-title'>Daily Summary and Notes</div><div class='bio-print-sections'>${entries}</div>`;
}
function bioRunPrint(){
  renderBiodynamicPrintView();
  document.body.classList.add('bio-printing');
  window.print();
};
window.printBiodynamicReport = function(){ bioRunPrint(); };
if (!window.__bioPrintCleanupBound) {
  window.addEventListener('afterprint', () => document.body.classList.remove('bio-printing'));
  window.__bioPrintCleanupBound = true;
}
function bioSelectDate(dateIso){
  const st = window.__bioModalState || {};
  st.selectedDate = dateIso || '';
  const data = st.data || {};
  const notes = (data && data.notes && typeof data.notes === 'object') ? data.notes : {};
  const summaries = (data && data.daily_summaries && typeof data.daily_summaries === 'object') ? data.daily_summaries : {};
  const day = Array.isArray(data.calendar) ? data.calendar.find((d) => d && d.date === dateIso) : null;
  document.querySelectorAll('#biodynamicCalendarModal .bio-day').forEach((el) => el.classList.toggle('selected', el.getAttribute('data-date') === dateIso));
  const dateEl = document.getElementById('bioSelectedDate');
  const metaEl = document.getElementById('bioNoteMeta');
  const inputEl = document.getElementById('bioNoteInput');
  const summaryEl = document.getElementById('bioDailySummary');
  const statusEl = document.getElementById('bioNoteStatus');
  if (dateEl) dateEl.textContent = dateIso ? bioFmtDateLabel(dateIso) : 'Select a day';
  if (metaEl) metaEl.textContent = day ? `${day.dominant_sign || '--'} / ${day.dominant_plant_part || '--'}` : '';
  if (inputEl) inputEl.value = dateIso ? String(notes[dateIso] || '') : '';
  if (summaryEl) {
    const storedSummary = dateIso ? String(summaries[dateIso] || '').trim() : '';
    const summaryText = storedSummary ? bioSummaryWithForecast(dateIso, storedSummary) : '';
    summaryEl.innerHTML = summaryText ? bioTextToHtml(summaryText) : '';
    setBioSummaryLoading(!!dateIso && !storedSummary);
    if (dateIso && !storedSummary) scheduleBioSummaryRefresh(dateIso);
  }
  if (statusEl) statusEl.textContent = '';
  renderBiodynamicModalSummary();
  renderBiodynamicPrintView();
}
function setBioSummaryLoading(isLoading){
  const card = document.querySelector('#biodynamicCalendarModal .bio-summary-card');
  if (card) { card.classList.toggle('summary-loading', !!isLoading); card.setAttribute('aria-busy', isLoading ? 'true' : 'false'); }
}
function scheduleBioSummaryRefresh(dateIso){
  const refresh = window.__bioSummaryRefresh || (window.__bioSummaryRefresh = { date:'', attempts:0, timer:null });
  if (refresh.date !== dateIso) { if (refresh.timer) clearTimeout(refresh.timer); refresh.date = dateIso; refresh.attempts = 0; refresh.timer = null; }
  if (refresh.timer || refresh.attempts >= 40) return;
  refresh.timer = setTimeout(async () => {
    refresh.timer = null; refresh.attempts += 1;
    if (!document.getElementById('biodynamicCalendarModal')) return;
    const st = window.__bioModalState || {};
    if (st.selectedDate !== dateIso) return;
    try {
      const data = await fetchBiodynamicMonth(st.month, true);
      if (st.selectedDate !== dateIso) return;
      st.data = data;
      const summary = String(data?.daily_summaries?.[dateIso] || '').trim();
      if (summary) { bioSelectDate(dateIso); return; }
    } catch (_) {}
    scheduleBioSummaryRefresh(dateIso);
  }, 2000);
}
function renderBiodynamicModalSummary(){
  const st = window.__bioModalState || {};
  const data = st.data || {};
  const sumEl = document.getElementById('bioModalSummary');
  if (!sumEl) return;
  const days = Array.isArray(data.calendar) ? data.calendar : [];
  const selected = days.find((d) => d && d.date === st.selectedDate) || null;
  if (!selected) {
    const cur = data.current || {};
    const upcoming = Array.isArray(data.upcoming) ? data.upcoming.slice(0, 2) : [];
    sumEl.textContent = cur.sign ? `${cur.sign} Moon | ${cur.element || '--'} / ${cur.plant_part || '--'} | ${upcoming.map((item) => `${item.start_hm || '--'} ${item.sign || '--'}`).join('  |  ')}` : (data.reason || 'Unavailable');
    return;
  }
  const segs = Array.isArray(selected.segments) ? selected.segments : [];
  const first = segs[0] || null;
  const next = segs[1] || null;
  const sign = selected.dominant_sign || (first && first.sign) || '--';
  const element = selected.dominant_element || (first && first.element) || '--';
  const part = selected.dominant_plant_part || (first && first.plant_part) || '--';
  const currentWindow = first ? `${first.start || '--'} to ${first.end || '--'}` : '--';
  const transitions = [];
  if (next && next.start) transitions.push(`${next.start} ${next.sign || '--'}`);
  if (segs[2] && segs[2].start) transitions.push(`${segs[2].start} ${segs[2].sign || '--'}`);
  sumEl.innerHTML = `<div><strong>${bioEsc(sign)} Moon</strong> | ${bioEsc(element)} / ${bioEsc(part)}</div><div>Current window: ${bioEsc(currentWindow)}</div><div>${bioEsc(transitions.join(' | ') || 'No later transitions for this day.')}</div>`;
}
function renderBiodynamicModal(){
  const modal = document.getElementById('biodynamicCalendarModal');
  if (!modal) return;
  const st = window.__bioModalState || {};
  const data = st.data || {};
  const monthEl = document.getElementById('bioModalMonthLabel');
  const calEl = document.getElementById('bioModalCalendar');
  if (monthEl) monthEl.textContent = data.month_label || '--';
  if (!calEl) return;
  const weekdays = Array.isArray(data.weekday_labels) ? data.weekday_labels : ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const days = Array.isArray(data.calendar) ? data.calendar : [];
  const notes = (data && data.notes && typeof data.notes === 'object') ? data.notes : {};
  let html = weekdays.map((label) => `<div class='bio-weekday'>${bioEsc(label)}</div>`).join('');
  html += days.map((day) => {
    const classes = ['bio-day'];
    if (!day.in_month) classes.push('out');
    if (day.is_today) classes.push('today');
    if (notes[day.date]) classes.push('noted');
    if ((st.selectedDate || '') === day.date) classes.push('selected');
    const style = `background:${bioDayBackground(day)};border-color:${bioEsc(day.dominant_color || '#d7d0bf')};`;
    const title = `${bioEsc(day.date || '')} ${bioEsc(day.dominant_sign || '')} ${bioEsc(day.dominant_plant_part || '')}`.trim();
    return `<button type='button' class='${classes.join(' ')}' data-date='${bioEsc(day.date)}' style='${style}' title='${title}'><span class='bio-day-num'>${bioEsc(day.day)}</span></button>`;
  }).join('');
  calEl.innerHTML = html;
  calEl.querySelectorAll('.bio-day').forEach((btn) => btn.addEventListener('click', () => bioSelectDate(btn.getAttribute('data-date') || '')));
  const hasSelectedDay = !!(st.selectedDate && days.some((d) => d && d.date === st.selectedDate));
  if (!hasSelectedDay && days.length) {
    const today = days.find((d) => d && d.in_month && d.is_today) || null;
    const firstInMonth = days.find((d) => d && d.in_month) || days[0];
    const defaultDay = today || firstInMonth || null;
    st.selectedDate = defaultDay ? defaultDay.date : '';
  }
  bioSelectDate(st.selectedDate || '');
}
function setBioMonthLoading(isLoading){
  const modal = document.getElementById('biodynamicCalendarModal');
  if (!modal) return;
  modal.classList.toggle('loading-month', !!isLoading);
  if (isLoading) setBioSummaryLoading(true);
  const prev = document.getElementById('bioPrevMonthBtn');
  const next = document.getElementById('bioNextMonthBtn');
  [prev, next].forEach((btn) => { if (btn) { btn.disabled = !!isLoading; btn.setAttribute('aria-busy', isLoading ? 'true' : 'false'); } });
}
async function fetchBiodynamicMonth(monthKey, forceRefresh=false){
  const key = String(monthKey || bioMonthKeyFromDate(new Date()));
  window.__bioMonthCache = window.__bioMonthCache || {};
  window.__bioMonthInflight = window.__bioMonthInflight || {};
  if (!forceRefresh && window.__bioMonthCache[key]) return window.__bioMonthCache[key];
  if (!forceRefresh && window.__bioMonthInflight[key]) return window.__bioMonthInflight[key];
  window.__bioMonthInflight[key] = fetch(`/api/biodynamic-calendar?month=${encodeURIComponent(key)}`, { cache:'no-store' })
    .then((resp) => { if (!resp.ok) throw new Error(`biodynamic_month_${resp.status}`); return resp.json(); })
    .then((data) => { window.__bioMonthCache[key] = data; return data; })
    .finally(() => { delete window.__bioMonthInflight[key]; });
  return window.__bioMonthInflight[key];
}
function prefetchBiodynamicAdjacentMonths(monthKey){
  const key = String(monthKey || '');
  if (!key) return;
  [-1, 1].forEach((delta) => {
    const nextKey = bioShiftMonth(key, delta);
    if (!nextKey || window.__bioMonthCache?.[nextKey] || window.__bioMonthInflight?.[nextKey]) return;
    setTimeout(() => { fetchBiodynamicMonth(nextKey).catch(() => {}); }, 0);
  });
}
async function loadBiodynamicMonth(monthKey, preferredDate){
  const st = window.__bioModalState || {};
  const key = String(monthKey || bioMonthKeyFromDate(new Date()));
  const requestSeq = (window.__bioMonthRequestSeq || 0) + 1;
  window.__bioMonthRequestSeq = requestSeq;
  st.month = key;
  setBioMonthLoading(true);
  try {
    const data = await fetchBiodynamicMonth(key);
    if (requestSeq !== window.__bioMonthRequestSeq) return;
    st.data = data;
    st.selectedDate = preferredDate || st.selectedDate || '';
    renderBiodynamicModal();
    prefetchBiodynamicAdjacentMonths(key);
  } finally {
    if (requestSeq === window.__bioMonthRequestSeq) setBioMonthLoading(false);
  }
}
function setBioOpenButtonLoading(isLoading){
  const btn = document.getElementById('bioOpenBtn');
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle('loading', !!isLoading);
  btn.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}
window.openBiodynamicCalendar = function(){
  setBioOpenButtonLoading(true);
  window.requestAnimationFrame(function(){
    window.requestAnimationFrame(function(){ window.location.assign('/calendar'); });
  });
};
window.openBiodynamicCalendarModal = async function(){
  try {
    if (!window.BackdropModal) return;
    setBioOpenButtonLoading(true);
    const html = `<div class='modal-backdrop' style='display:none'>  <div class='modal bio-modal' id='biodynamicCalendarModal'>    <div class='modal-header'>      <h3 class='modal-title'>Biodynamic Calendar</h3>      <button type='button' onclick='window.BackdropModal && window.BackdropModal.close("biodynamicCalendarModal")' style='border:none;background:transparent;font-size:1.2rem;cursor:pointer;' aria-label='Close'>&times;</button>    </div>    <div class='modal-body'>      <div class='bio-modal-main'>        <div class='bio-nav'>          <button type='button' class='bio-nav-btn' id='bioPrevMonthBtn' aria-label='Previous month' title='Previous month'>&lt;</button>          <div class='bio-month' id='bioModalMonthLabel'>--</div>          <button type='button' class='bio-nav-btn' id='bioNextMonthBtn' aria-label='Next month' title='Next month'>&gt;</button>        </div>        <div class='bio-modal-summary' id='bioModalSummary'></div>        <div class='bio-legend' aria-label='Biodynamic calendar color legend'>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-root' aria-hidden='true'></span>Root</span>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-leaf' aria-hidden='true'></span>Leaf</span>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-flower' aria-hidden='true'></span>Flower</span>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-fruit' aria-hidden='true'></span>Fruit</span>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-rest' aria-hidden='true'></span>Rest</span>          <span class='bio-legend-item'><span class='bio-legend-swatch bio-legend-transition' aria-hidden='true'></span>Transition</span>        </div>        <div class='bio-calendar-wrap'>          <div class='bio-calendar' id='bioModalCalendar'></div>          <div class='bio-loading-overlay bio-calendar-loading' role='status' aria-label='Loading biodynamic calendar'><span class='spinner' aria-hidden='true'></span></div>        </div>      </div>      <div class='bio-modal-side'>        <div class='bio-selected-date' id='bioSelectedDate'>Select a day</div>        <div class='bio-note-card bio-summary-card'>          <div class='bio-note-title'>Daily Summary</div>          <div class='bio-note-meta' id='bioNoteMeta'></div>          <div id='bioDailySummary' class='bio-summary-output'></div>          <div class='bio-loading-overlay bio-summary-loading' role='status' aria-label='Loading daily summary'><span class='spinner' aria-hidden='true'></span></div>        </div>        <div class='bio-note-card bio-notes-card'>          <div class='bio-note-title'>Daily Notes</div>          <textarea id='bioNoteInput' class='bio-note-input' placeholder='Add a note for the selected biodynamic day...'></textarea>          <div class='bio-note-actions'>            <button type='button' class='bio-save-btn' id='bioSaveNoteBtn'>Save Note</button>            <button type='button' class='bio-print-btn' id='bioPrintReportBtn'>Print Report</button>            <div class='bio-note-status' id='bioNoteStatus'></div>          </div>        </div>      </div>    </div>    <div class='bio-print-sheet' id='bioPrintReportSheet' aria-hidden='true'></div>  </div></div>`;
    window.BackdropModal.close('biodynamicCalendarModal');
    const modal = window.BackdropModal.openFromHtml(html, 'biodynamicCalendarModal');
    if (!modal) return;
    const monthKey = (window.__bioModalState && window.__bioModalState.month) || bioMonthKeyFromDate(new Date());
    const todayIso = bioTodayIso();
    const preferredDate = monthKey === bioMonthKeyFromDate(new Date(`${todayIso}T00:00:00`)) ? todayIso : '';
    document.getElementById('bioPrevMonthBtn').addEventListener('click', () => loadBiodynamicMonth(bioShiftMonth(window.__bioModalState.month, -1), ''));
    document.getElementById('bioNextMonthBtn').addEventListener('click', () => loadBiodynamicMonth(bioShiftMonth(window.__bioModalState.month, 1), ''));
    document.getElementById('bioPrintReportBtn').addEventListener('click', () => { if (window.printBiodynamicReport) window.printBiodynamicReport(); });
    document.getElementById('bioSaveNoteBtn').addEventListener('click', async () => {
      const st = window.__bioModalState || {};
      const dateIso = st.selectedDate || '';
      const input = document.getElementById('bioNoteInput');
      const status = document.getElementById('bioNoteStatus');
      const btn = document.getElementById('bioSaveNoteBtn');
      if (!dateIso || !input || !status) { if (status) status.textContent = 'Select a day first.'; return; }
      if (btn) { if (!btn.dataset.baseLabel) btn.dataset.baseLabel = btn.textContent || 'Save Note'; btn.disabled = true; btn.textContent = 'Saving...'; }
      status.textContent = 'Saving...';
      const noteText = input.value || '';
      const resp = await fetch('/api/biodynamic-note', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ date: dateIso, note: noteText }) });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) { status.textContent = (payload && payload.error) ? payload.error : 'Save failed'; if (typeof window.showToast === 'function') window.showToast('Failed to save biodynamic note', 'error'); if (btn) { btn.disabled = false; btn.textContent = btn.dataset.baseLabel || 'Save Note'; } return; }
      st.data = st.data || {};
      st.data.notes = st.data.notes || {};
      if ((noteText || '').trim()) st.data.notes[dateIso] = String(noteText).trim(); else delete st.data.notes[dateIso];
      renderBiodynamicModal();
      status.textContent = 'Saved';
      if (typeof window.showToast === 'function') window.showToast('Biodynamic note saved', 'ok');
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.baseLabel || 'Save Note'; }
    });
    await loadBiodynamicMonth(monthKey, preferredDate);
  } catch (e) {
    console.error('Failed to open biodynamic modal', e);
  } finally {
    setBioOpenButtonLoading(false);
  }
};
document.addEventListener('DOMContentLoaded',()=>{
  if (typeof drawSunPath === 'function') drawSunPath(astroData);
  if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData);
  if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);
  if (weatherForecastEnabled && typeof loadWeatherForecast === 'function') {
    loadWeatherForecast();
    window.setInterval(function(){ loadWeatherForecast(false, true); }, 300000);
  }
  const form = document.querySelector('form');
  const forecastFiveDayBtn = document.getElementById('forecastFiveDayBtn');
  const bioOpenBtn = document.getElementById('bioOpenBtn');
  const btn = document.getElementById('saveBtn');
  const spinner = document.getElementById('saveSpinner');
  if (forecastFiveDayBtn) {
    forecastFiveDayBtn.addEventListener('click', function(){ window.openWeatherForecastModal(); });
    window.addEventListener('pageshow', function(){ setForecastButtonLoading(false); });
  }
  if (bioOpenBtn) {
    bioOpenBtn.addEventListener('click', function(){ if (window.openBiodynamicCalendar) window.openBiodynamicCalendar(); });
    window.addEventListener('pageshow', function(){ setBioOpenButtonLoading(false); });
  }
  if(form && btn && spinner){
    form.addEventListener('submit',()=>{
      spinner.style.display='inline-block';
      btn.disabled=true;
    });
  }
  if (window.initializeSensorRowCollapse) window.initializeSensorRowCollapse();
});
let knownSensors = new Set();
let pendingLayoutRefresh = false;
let deferredLayoutRefresh = false;
let __lastSuppressedSwitchLayoutAt = 0;
function ensureSensorInSelector(sid) {
  const sel = document.getElementById('sensorSelect');
  if (!sel) return;
  if (![...sel.options].some(o => o.value === sid)) {
    const opt = document.createElement('option');
    opt.value = sid;
    opt.textContent = displaySensorId(sid);
    sel.appendChild(opt);
  }
}
function ensureSensorUI(sid, metricList, locationText) {
 if (!sid) return;
 const pageSensor = document.getElementById('sensor_id')?.value || 'All';
 if (pageSensor !== 'All' && pageSensor !== sid) return;

 const safeId = (x) => x.replace(/[^a-zA-Z0-9_\-]/g,'_');
 const toSafeMetric = (m) => (typeof toSafe === 'function' ? toSafe(m) : safeId(m));

 const existingRow = document.querySelector('.sensor-row');
 const byGraphModal = document.getElementById('graphModal')?.parentElement || null;
 const dashboard = document.querySelector('.dashboard');
 const parent = document.querySelector('.dashboard-content') || (existingRow && existingRow.parentElement) || dashboard || byGraphModal || document.body;
 const switchGroup = document.getElementById('group_switches');

  const groupId = `group_${sid}`;
  let group = document.getElementById(groupId);
  if (!group) {
    group = document.createElement('div');
    group.id = groupId;
    group.className = 'sensor-group';
    group.dataset.sensorId = sid;
    group.dataset.dashboardRowId = sid;
    group.dataset.sensorExpanded = dashboardMetricSet === 'All' ? 'true' : 'false';
    group.style.width = '100%';
    group.style.flex = '0 0 100%';
    group.style.display = 'block';
    if (switchGroup && switchGroup.parentElement === parent) parent.insertBefore(group, switchGroup);
    else parent.appendChild(group);
  }

  const headerId = `${sid}_header`;
  if (!document.getElementById(headerId)) {
    const headerWrap = document.createElement('div');
    headerWrap.style.textAlign = 'center';
    headerWrap.style.width = '100%';
    headerWrap.style.marginTop = '1rem';
    const locText = (locationText || displaySensorId(sid));
    const sidUpper = displaySensorId(sid);
    const sidLower = (sid || '').toLowerCase();
    const pendingColor = '#ffc107';
    const header = document.createElement('h3');
    header.id = `${sid}_header`;
    header.className = 'sensor-group-title';
    const dot = document.createElement('span');
    dot.className = 'sensor-status-dot';
    dot.id = `${sid}_statusdot`;
    dot.dataset.sid = sid;
    dot.setAttribute('title', 'Connection status: unknown');
    dot.setAttribute('aria-label', 'Connection status: unknown');
    dot.setAttribute('style', `display:inline-block;width:15px;height:15px;border-radius:50%;vertical-align:middle;margin-right:6px;background:${pendingColor};border:1px solid #666;`);
    header.appendChild(dot);
    header.appendChild(document.createTextNode(` ${sidUpper} `));
    const settingsLink = document.createElement('a');
    settingsLink.href = '#';
    settingsLink.title = 'Open settings';
    settingsLink.setAttribute('aria-label', `Open ${sidUpper} Settings`);
    settingsLink.setAttribute('style', 'margin-left:2px; margin-right:8px; text-decoration:none; font-size:0.8em; vertical-align:middle;');
    settingsLink.addEventListener('click', function(ev) {
      ev.preventDefault();
      if (window.editSensorSettings) window.editSensorSettings(sidLower);
    });
    settingsLink.innerHTML = config.settingsGearSvg;
    header.appendChild(settingsLink);
    header.appendChild(document.createTextNode(locText));
    headerWrap.appendChild(header);
    group.appendChild(headerWrap);
  } else {
    const hdr = document.getElementById(headerId);
    if (hdr && !document.getElementById(`${sid}_statusdot`)) {
      const dot = document.createElement('span');
      dot.className = 'sensor-status-dot';
      dot.id = `${sid}_statusdot`;
      dot.setAttribute('data-sid', sid);
      dot.setAttribute('title', 'Connection status: unknown');
      dot.setAttribute('aria-label', 'Connection status: unknown');
      dot.setAttribute('style', 'display:inline-block;width:15px;height:15px;border-radius:50%;                                   vertical-align:middle;margin-right:6px;                                   background:#ffc107;border:1px solid #666;');
      hdr.insertBefore(dot, hdr.firstChild);
    }
  }

  const rowId = `row_${sid}`;
  let row = document.getElementById(rowId);
  if (!row) {
    row = document.createElement('div');
    row.id = rowId;
    row.className = 'sensor-row';
    group.appendChild(row);
  }

  const styleMap = (expectedDisplayStyleMap && expectedDisplayStyleMap[sid]) || {};
  (metricList || []).forEach((metric, idx) => {
    const safeMetric = toSafeMetric(metric);
    const containerId = `${sid}_${safeMetric}_container`;
    if (document.getElementById(containerId)) return;

    const container = document.createElement('div');
    container.className = 'metric-container';
    container.id = containerId;
    container.dataset.sensor = sid;
    container.dataset.tileStyle = metricTileStyleMap[sid] || 'Theme';
    container.dataset.metric = metric;
    const styleKey = `METRIC_${idx + 1}`;
    container.dataset.displayStyle = window.normalizeDisplayStyle(styleMap[styleKey] || window.displayStyle || 'Gauge');
    const lazyVisual = dashboardMetricSet !== 'All' && idx >= 6 && group.dataset.sensorExpanded !== 'true';
    if (lazyVisual) container.dataset.lazyMetricVisual = '1';

    const safe = `${sid}_${safeMetric}`;
    const gaugeVisual = lazyVisual ? '' : `<div class='gauge-view' id='${safe}GaugeContainer'><canvas id='${safe}Gauge'></canvas></div>`;
    const graphVisual = lazyVisual ? '' : `<div class='graph-view' id='${safe}GraphContainer'><canvas class='micrograph-canvas' id='${safe}Micrograph' width='${metricCanvasWidth}' height='${metricCanvasHeight}'></canvas></div>`;
    container.innerHTML =
      `<div class='metric-title'>${gaugeConfig?.[metric]?.display_label || metric}</div>` +
      `<div class='gauge-container'>${gaugeVisual}</div>` +
      `<div class='graph-container'>${graphVisual}</div>` +
      `<div class='metric-current-value'><span id='${safe}_val'>--</span>` +
        `<span class='trend-arrow' data-metric='${metric}' role='img' tabindex='0'>` +
          `<svg viewBox='0 0 32 32' aria-hidden='true' focusable='false'>` +
            `<path d='M2 16H29M22 10L29 16L22 22'/></svg></span></div>` +
      `<div class='metric-stats' id='${safe}_stats'>` +
        `<div>Min<br><small>--</small></div>` +
        `<div>Avg<br>--</div>` +
        `<div>Max<br><small>--</small></div>` +
      `</div>`;

    row.appendChild(container);
    if (window.ensureContainerDisplayStyle) {
      window.ensureContainerDisplayStyle(container);
    }
    if (window.bindMetricContainer) {
      window.bindMetricContainer(container);
    }
  });
  if (window.scheduleSensorRowCollapseRefresh) window.scheduleSensorRowCollapseRefresh(group);
}
window.ensureSensorCollapseToggle = function(group){
  if (!group) return null;
  const sid = String(group.dataset.sensorId || '').trim();
  const header = group.querySelector('.sensor-group-title');
  if (!sid || !header) return null;
  let button = header.querySelector('.sensor-collapse-toggle');
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'sensor-collapse-toggle';
    button.dataset.sensorId = sid;
    button.setAttribute('aria-controls', `row_${sid}`);
    button.setAttribute('aria-expanded', 'false');
    button.hidden = true;
    const icon = document.createElement('span');
    icon.className = 'sensor-collapse-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = `<svg viewBox='0 0 24 24' focusable='false'><path fill='currentColor' d='M7.2 3.9A1.5 1.5 0 0 0 5 5.25v13.5a1.5 1.5 0 0 0 2.2 1.34l11.65-6.75a1.55 1.55 0 0 0 0-2.68z'></path></svg>`;
    button.appendChild(icon);
    header.insertBefore(button, header.firstChild);
  }
  return button;
};
window.refreshSensorRowCollapse = function(group){
  if (!group || !document.body.contains(group)) return;
  const sid = String(group.dataset.sensorId || '').trim();
  const row = group.querySelector('.sensor-row');
  const button = window.ensureSensorCollapseToggle(group);
  if (!sid || !row || !button) return;
  const cards = Array.from(row.children).filter((child) => child.classList && child.classList.contains('metric-container'));
  row.classList.remove('is-collapsed');
  cards.forEach((card) => card.classList.remove('sensor-metric-overflow'));
  if (cards.length < 2) {
    button.hidden = true;
    button.setAttribute('aria-expanded', 'true');
    return;
  }
  const overflowCards = cards.slice(6);
  const hasAdditionalMetrics = overflowCards.length > 0;
  button.hidden = !hasAdditionalMetrics;
  if (!hasAdditionalMetrics) {
    button.setAttribute('aria-expanded', 'true');
    return;
  }
  overflowCards.forEach((card) => card.classList.add('sensor-metric-overflow'));
  const expanded = group.dataset.sensorExpanded === 'true';
  const sidUpper = displaySensorId(sid);
  row.classList.toggle('is-collapsed', !expanded);
  if (expanded && typeof window.materializeExpandedMetricVisuals === 'function') {
    window.materializeExpandedMetricVisuals(group);
  } else if (!expanded && typeof window.releaseCollapsedMetricVisuals === 'function') {
    window.releaseCollapsedMetricVisuals(group);
  }
  button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  button.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${sidUpper} metrics`);
  button.title = `${expanded ? 'Collapse' : 'Expand'} ${sidUpper} metrics`;
};
window.scheduleSensorRowCollapseRefresh = function(group){
  window.requestAnimationFrame(function(){
    window.requestAnimationFrame(function(){ window.refreshSensorRowCollapse(group); });
  });
};
window.initializeSensorRowCollapse = function(){
  document.querySelectorAll('.sensor-group[data-sensor-id]').forEach((group) => {
    window.scheduleSensorRowCollapseRefresh(group);
  });
};
if (!window.__sensorRowCollapseInit) {
  window.__sensorRowCollapseInit = true;
  document.addEventListener('click', function(ev){
    const button = ev.target.closest('.sensor-collapse-toggle');
    if (!button) return;
    const group = button.closest('.sensor-group[data-sensor-id]');
    if (!group) return;
    group.dataset.sensorExpanded = group.dataset.sensorExpanded === 'true' ? 'false' : 'true';
    window.refreshSensorRowCollapse(group);
  });
  let collapseResizeTimer = 0;
  window.addEventListener('resize', function(){
    window.clearTimeout(collapseResizeTimer);
    collapseResizeTimer = window.setTimeout(window.initializeSensorRowCollapse, 120);
  });
  const initializeCollapseRows = function(){ window.initializeSensorRowCollapse(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeCollapseRows);
  else initializeCollapseRows();
  window.addEventListener('load', initializeCollapseRows);
  window.addEventListener('pageshow', initializeCollapseRows);
  window.setTimeout(initializeCollapseRows, 500);
  window.setTimeout(initializeCollapseRows, 2000);
}
window.DISPLAY_STYLES = ['Gauge', 'Graph6hr', 'Graph24hr'];
window.metricDisplayStyles = window.metricDisplayStyles || {};
window.updateMetricCardTitle = function(container, style) {
  if (!container) return;
  const metric = String(container.dataset.metric || '').toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  const title = container.querySelector('.metric-title');
  if (!title) return;
  const normalized = window.normalizeDisplayStyle ? window.normalizeDisplayStyle(style) : String(style || 'Gauge');
  let baseTitle = String(container.dataset.metricCardTitle || title.textContent || '').replace(/^(?:6|24)hr\s+/i, '');
  if (!container.dataset.metricCardTitle) container.dataset.metricCardTitle = baseTitle;
  if (metric === 'wind direction' && normalized !== 'Gauge') {
    baseTitle = 'Wind-Rose';
  } else if (metric === 'wind direction') {
    const unit = String(gaugeConfig?.['Wind Direction']?.unit || 'deg');
    baseTitle = `Wind Direction (${unit})`;
  }
  const durationPrefix = normalized === 'Graph6hr' ? '6hr ' : (normalized === 'Graph24hr' ? '24hr ' : '');
  title.textContent = durationPrefix + baseTitle;
};
window.normalizeDisplayStyle = function(raw) {
  const s = (raw || '').toString().toLowerCase();
  if (s === 'gauge') return 'Gauge';
  if (s === 'graph' || s === 'graph24' || s === 'graph24hr' || s === '24h' || s === '24hr') return 'Graph24hr';
  if (s === 'graph6' || s === 'graph6hr' || s === '6h' || s === '6hr') return 'Graph6hr';
  return 'Gauge';
};
window.detectDisplayStyleFromDom = function(container) {
  if (!container) return 'Gauge';
  const ds = container.dataset.displayStyle;
  if (ds) return window.normalizeDisplayStyle(ds);
  const hasMicro = !!container.querySelector('.micrograph-canvas');
  if (hasMicro) {
    const r1 = container.dataset.range || container.dataset.graphRange || '';
    const r = r1.toString().toLowerCase();
    if (r.indexOf('6') >= 0) return 'Graph6hr';
    return 'Graph24hr';
  }
  const hasGauge = !!container.querySelector('canvas[id$="Gauge"]');
  if (hasGauge) return 'Gauge';
  return 'Gauge';
};
window.registerContainerStyle = function(container, style) {
  if (!container || !container.id) return;
  const norm = window.normalizeDisplayStyle(style);
  window.metricDisplayStyles[container.id] = norm;
  container.dataset.displayStyle = norm;
  if (typeof window.updateMetricCardTitle === 'function') window.updateMetricCardTitle(container, norm);
};
window.getContainerStyle = function(container) {
  if (!container || !container.id) return 'Gauge';
  const fromMap = window.metricDisplayStyles[container.id];
  if (fromMap) return fromMap;
  const inferred = window.detectDisplayStyleFromDom(container);
  window.metricDisplayStyles[container.id] = inferred;
  container.dataset.displayStyle = inferred;
  return inferred;
};
window.getContainerStyleById = function(containerId) {
  if (!containerId) return 'Gauge';
  const el = document.getElementById(containerId);
  if (!el) return 'Gauge';
  return window.getContainerStyle(el);
};
document.addEventListener('DOMContentLoaded', function() {
  try {
    const all = document.querySelectorAll('.metric-container');
    all.forEach(function(c) {
      if (!c.id) return;
      const cur = window.metricDisplayStyles[c.id];
      if (cur) {
        c.dataset.displayStyle = cur;
      } else {
        const inferred = window.detectDisplayStyleFromDom(c);
        window.metricDisplayStyles[c.id] = inferred;
        c.dataset.displayStyle = inferred;
      }
    });
  } catch (e) {
    console.warn('init metricDisplayStyles error', e);
  }
});
function normalizeDegrees(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return ((n % 360) + 360) % 360;
}

function drawCompassGauge(canvas, rawValue) {
  if (!canvas) return;
  canvas.style.setProperty('--gauge-center-shift', '0%');
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const cssSize = 205;
  const scale = cssSize / 170;
  canvas.style.width = `${cssSize}px`;
  canvas.style.height = `${cssSize}px`;
  canvas.width = Math.round(cssSize * dpr);
  canvas.height = Math.round(cssSize * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssSize, cssSize);
  const cx = cssSize / 2;
  const cy = cssSize / 2;
  const r = 66 * scale;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = '#dff7fb';
  ctx.fill();
  ctx.lineWidth = 10 * scale;
  ctx.strokeStyle = '#a9d7e3';
  ctx.stroke();
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(32,45,52,.35)';
  ctx.stroke();

  for (let deg = 0; deg < 360; deg += 15) {
    const rad = (deg - 90) * Math.PI / 180;
    const major = deg % 45 === 0;
    const inner = r - (major ? 13 : 8) * scale;
    const outer = r + scale;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(rad) * inner, cy + Math.sin(rad) * inner);
    ctx.lineTo(cx + Math.cos(rad) * outer, cy + Math.sin(rad) * outer);
    ctx.lineWidth = major ? 2 * scale : scale;
    ctx.strokeStyle = major ? '#1f2933' : 'rgba(31,41,51,.45)';
    ctx.stroke();
  }

  const labels = [{t:'N',d:0},{t:'E',d:90},{t:'S',d:180},{t:'W',d:270}];
  ctx.font = `700 ${Math.round(16 * scale)}px sans-serif`;
  ctx.fillStyle = '#1f2933';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  labels.forEach(({t,d}) => {
    const rad = (d - 90) * Math.PI / 180;
    ctx.fillText(t, cx + Math.cos(rad) * (r - 27 * scale), cy + Math.sin(rad) * (r - 27 * scale));
  });

  const value = normalizeDegrees(rawValue);
  if (value !== null) {
    const rad = (value - 90) * Math.PI / 180;
    const tipX = cx + Math.cos(rad) * (r - 14 * scale);
    const tipY = cy + Math.sin(rad) * (r - 14 * scale);
    const tailX = cx - Math.cos(rad) * 16 * scale;
    const tailY = cy - Math.sin(rad) * 16 * scale;
    const sideRad = rad + Math.PI / 2;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(tailX + Math.cos(sideRad) * 6 * scale, tailY + Math.sin(sideRad) * 6 * scale);
    ctx.lineTo(tailX - Math.cos(sideRad) * 6 * scale, tailY - Math.sin(sideRad) * 6 * scale);
    ctx.closePath();
    ctx.fillStyle = '#050505';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, 8 * scale, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

function getMetricCanvasSize(canvas) {
  const root = (canvas && canvas.closest && canvas.closest('.dashboard')) || document.documentElement;
  let cssWidth = metricCanvasWidth;
  let cssHeight = metricCanvasHeight;
  try {
    const styles = window.getComputedStyle(root);
    const rawW = Number.parseFloat(styles.getPropertyValue('--canvas-width'));
    const rawH = Number.parseFloat(styles.getPropertyValue('--canvas-height'));
    if (Number.isFinite(rawW) && rawW > 0) cssWidth = rawW;
    if (Number.isFinite(rawH) && rawH > 0) cssHeight = rawH;
  } catch (_) {}
  return { cssWidth, cssHeight };
}

function drawFallbackGauge(canvas, rawValue, config) {
  if (!canvas || !config) return;
  canvas.style.setProperty('--gauge-center-shift', '-6%');
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const size = getMetricCanvasSize(canvas);
  const cssWidth = size.cssWidth;
  const cssHeight = size.cssHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = '';
  canvas.style.height = '';
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  const cx = cssWidth / 2;
  const cy = cssHeight * 0.56;
  const radius = Math.min(cssWidth * 0.43, cssHeight * 0.48);
  const min = Number(config.min);
  const max = Number(config.max);
  const value = Number(rawValue);
  const start = Math.PI * 0.82;
  const end = Math.PI * 2.18;
  const span = end - start;
  const zones = Array.isArray(config.zones) ? config.zones : [];
  function angleFor(v) {
    if (!Number.isFinite(v) || !Number.isFinite(min) || !Number.isFinite(max) || max <= min) return start;
    const ratio = Math.max(0, Math.min(1, (v - min) / (max - min)));
    return start + (ratio * span);
  }
  ctx.lineWidth = 16;
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#d7dde2';
  ctx.beginPath();
  ctx.arc(cx, cy, radius, start, end);
  ctx.stroke();
  zones.forEach(z => {
    const zMin = Number(z?.min);
    const zMax = Number(z?.max);
    if (!Number.isFinite(zMin) || !Number.isFinite(zMax)) return;
    ctx.strokeStyle = z?.strokeStyle || z?.color || '#6FADCF';
    ctx.beginPath();
    ctx.arc(cx, cy, radius, angleFor(zMin), angleFor(zMax));
    ctx.stroke();
  });
  const needle = angleFor(value);
  ctx.lineWidth = 4;
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#111827';
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(needle) * radius * 0.72, cy + Math.sin(needle) * radius * 0.72);
  ctx.stroke();
  ctx.fillStyle = '#111827';
  ctx.beginPath();
  ctx.arc(cx, cy, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#374151';
  ctx.font = '12px sans-serif';
  ctx.textAlign = 'center';
  if (Number.isFinite(min)) ctx.fillText(String(min), cx - radius * 0.78, cy + 18);
  if (Number.isFinite(max)) ctx.fillText(String(max), cx + radius * 0.78, cy + 18);
  ctx.restore();
}

function initGauge() {
  const metricContainers = document.querySelectorAll('.metric-container');
  metricContainers.forEach(container => {
    try {
    const sensor = container.dataset.sensor;
    const metric = container.dataset.metric;
    const safe = `${sensor}_${toSafe(metric)}`;
    const canvasId = `${safe}Gauge`;
    const labelId = `${safe}_val`;
    const canvas = document.getElementById(canvasId);
    const label = document.getElementById(labelId);
    if (!canvas || !label) return;
    const config = pressureGaugeConfig(metric, gaugeConfig?.[metric], pressureAltitude, pressureSensorContext[sensor]);
    if (!config) return;
    let value = currentValues?.[sensor]?.[metric];
    const isNull = (value == null);
    if ((config.render || '').toLowerCase() === 'compass') {
      drawCompassGauge(canvas, isNull ? null : value);
      window[`${safe}_compass`] = canvas;
      const valueMetric = config.value_metric || metric;
      const valueConfig = gaugeConfig?.[valueMetric] || config;
      const displayValue = currentValues?.[sensor]?.[valueMetric];
      label.innerText = formatCurrentValue(displayValue, valueConfig);
      if (window.registerContainerStyle) {
        const initialStyle = window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');
        window.registerContainerStyle(container, initialStyle);
      }
      return;
    }
    if (isNull) value = 0;
    const gaugeValue = convertForDisplay(value, config);
    const existingGauge = window[`${safe}_gauge`];
    if (existingGauge && existingGauge.__sensoriusCanvas === canvas && typeof existingGauge.set === 'function') {
      if (existingGauge.maxValue !== config.max || existingGauge.minValue !== config.min) {
        existingGauge.maxValue = config.max;
        existingGauge.setMinValue(config.min);
        existingGauge.setOptions({...existingGauge.options, staticZones: config.zones, staticLabels: {...existingGauge.options.staticLabels, labels: config.ticks}});
      }
      existingGauge.set(gaugeValue ?? 0);
      label.innerText = isNull ? '--' : formatCurrentValue(value, config);
      if (window.registerContainerStyle) {
        const existingStyle = window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');
        window.registerContainerStyle(container, existingStyle);
      }
      return;
    }
    const opts = {
      angle: -0.2, lineWidth: 0.25, radiusScale: 0.9,
      pointer: { length: 0.5, strokeWidth: 0.035, color: '#000000' },
      staticZones: config.zones || [],
      limitMax: Boolean(config.rain_period),
      staticLabels: {
        font: '12px sans-serif',
        labels: config.ticks, color: '#000', fractionDigits: 1
      },
      colorStart: '#6FADCF', colorStop: '#8FC0DA', strokeColor: '#E0E0E0',
      generateGradient: true, highDpiSupport: true
    };
    const canvasSize = getMetricCanvasSize(canvas);
    canvas.width = Math.round(canvasSize.cssWidth);
    canvas.height = Math.round(canvasSize.cssHeight);
    canvas.style.width = '';
    canvas.style.height = '';
    if (typeof Gauge === 'function') {
      try {
        const gauge = new Gauge(canvas).setOptions(opts);
        gauge.maxValue = config.max;
        gauge.setMinValue(config.min);
        gauge.animationSpeed = 32;
        gauge.set(gaugeValue ?? 0);
        gauge.render();
        const pivotY = canvas.height * gauge.paddingTop + gauge.availableHeight - (gauge.radius + gauge.lineWidth / 2) * gauge.extraPadding;
        if (Number.isFinite(pivotY) && canvas.height > 0) {
          canvas.style.setProperty('--gauge-center-shift', String((0.5 - pivotY / canvas.height) * 100) + '%');
        }
        gauge.__sensoriusCanvas = canvas;
        window[`${safe}_gauge`] = gauge;
      } catch (e) {
        console.warn('Gauge render failed, using fallback', safe, e);
        drawFallbackGauge(canvas, gaugeValue ?? 0, config);
        window[`${safe}_gauge`] = null;
      }
    } else {
      drawFallbackGauge(canvas, gaugeValue ?? 0, config);
      window[`${safe}_gauge`] = null;
    }
    label.innerText = isNull ? '--' : formatCurrentValue(value, config);
    if (window.registerContainerStyle) {
      const initialStyle = window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');
      window.registerContainerStyle(container, initialStyle);
    }
    } catch (e) {
      console.warn('initGauge: metric card init failed', container && container.id, e);
    }
  });
}
let retryCount = 0;
const maxRetries = 3;
function checkAndRetryIfNoGauges() {
  let retryNeeded = false;
  for (const [sensorID, metrics] of Object.entries(expectedGaugeMap)) {
    for (const metric of metrics) {
      const sensor = sensorID;
      const metricLabel = metric;
      const safe = `${sensor}_${toSafe(metricLabel)}`;
      const el = document.getElementById(`${safe}_val`);
      if (!el) {
        retryNeeded = true;
        console.log(`[checkAndRetry] Missing gauge: ${safe}`);
      }
    }
  }
  if (retryNeeded) {
    if (retryCount < maxRetries) {
      retryCount++;
      console.log(`[checkAndRetry] Retrying initGauge() in 1s (attempt ${retryCount})`);
      setTimeout(initGauge, 1000);
    } else {
      console.log(`[checkAndRetry] Max retries reached.`);
    }
  }
}
document.addEventListener('DOMContentLoaded', () => {
  try { knownSensors = new Set(Object.keys(expectedGaugeMap || {})); } catch(_) { knownSensors = new Set(); }
  checkAndRetryIfNoGauges();
});
function formatIsoForStats(ts) {
  if (!ts || typeof ts !== 'string') return '--';
  const noMicros = ts.replace(/\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$)/, '');
  const noTz = noMicros.replace(/(Z|[+-]\d{2}:\d{2})$/, '');
  const match = noTz.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/);
  if (!match) return noTz.replace('T', '<br>').replace(' ', '<br>');
  const hour24 = Number.parseInt(match[2], 10);
  const hour12 = ((hour24 + 11) % 12) + 1;
  return `${match[1]}<br>${hour12}:${match[3]}:${match[4]} ${hour24 < 12 ? 'AM' : 'PM'}`;
}

function renderStatsHtml(minVal, avgVal, maxVal, minTs, maxTs) {
  return `<div>Min<br><small>${minVal} at<br> ${minTs}</small></div>` +
         `<div>Avg<br>${avgVal}</div>` +
         `<div>Max<br><small>${maxVal} at<br> ${maxTs}</small></div>`;
}

function toFixedOrDash(v) {
  const n = Number.parseFloat(v);
  return Number.isFinite(n) ? n.toFixed(1) : '--';
}
function formatStatValue(value, config) {
  const converted = convertForDisplay(value, config);
  if (converted === null) return '--';
  const precision = Number.isInteger(config?.display_precision) ? config.display_precision : 1;
  return converted.toFixed(precision);
}

function convertForDisplay(value, config, rate = false) {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  const factor = Number(config?.display_factor ?? 1);
  const offset = rate ? 0 : Number(config?.display_offset ?? 0);
  return numeric * (Number.isFinite(factor) ? factor : 1) + (Number.isFinite(offset) ? offset : 0);
}
function formatCurrentValue(value, config) {
  const unit = (config && config.unit) || '';
  const converted = convertForDisplay(value, config);
  if (converted === null) return '--';
  const precision = config && config.display_precision;
  const text = Number.isInteger(precision) ? converted.toFixed(precision) : String(Number(converted.toFixed(2)));
  return `${text} ${unit}`.trim();
}

const metricTrendAngles = {};
function pressureTrendMetric(metric) {
  const text = String(metric || '').trim();
  return text === 'Pressure' || text === 'Baro-Pressure' || text.endsWith(' Baro-Pressure');
}
function trendThresholds(metric) {
  const config = gaugeConfig?.[metric] || {};
  if (pressureTrendMetric(metric)) return [.1, 1].map(v => Math.abs(convertForDisplay(v, config, true) ?? v));
  const span = Math.max(.000001, Number(config.max ?? 100) - Number(config.min ?? 0));
  return [span * .0025, span * .025];
}
function trendScore(metric, rate) {
  const numeric = Number(rate);
  const thresholds = trendThresholds(metric);
  const dead = thresholds[0];
  const full = thresholds[1];
  if (!Number.isFinite(numeric) || Math.abs(numeric) <= dead) return 0;
  const strength = Math.min(1, (Math.abs(numeric) - dead) / Math.max(.000001, full - dead));
  return numeric < 0 ? -strength : strength;
}
function trendAngle(score) {
  const numeric = Math.max(-1, Math.min(1, Number(score) || 0));
  if (numeric >= 0) return numeric <= .5 ? -30 * numeric : -15 - 150 * (numeric - .5);
  const strength = -numeric;
  return strength <= .5 ? 100 * strength : 50 + 80 * (strength - .5);
}
function trendRateText(rate) {
  const numeric = Number(rate);
  const absolute = Math.abs(numeric);
  if (!Number.isFinite(numeric)) return '';
  const digits = absolute >= 100 ? 0 : absolute >= 10 ? 1 : absolute >= 1 ? 2 : 3;
  return (numeric > 0 ? '+' : '') + numeric.toFixed(digits);
}
function trendTitle(metric, trend, score) {
  trend = trend || {};
  const rate = Number(trend.rate_per_hour);
  const minutes = Math.max(0, Math.round(Number(trend.window_s || 0) / 60));
  if (!Number.isFinite(rate) || Number(trend.samples || 0) < 6) return 'Learning trend; at least six samples are required';
  const direction = score > 0 ? 'Rising' : score < 0 ? 'Falling' : 'Steady';
  const unit = (gaugeConfig?.[metric] || {}).unit || '';
  const rateUnit = unit ? `${unit}/hour` : 'units/hour';
  return `${direction} ${trendRateText(rate)} ${rateUnit} · ${minutes} minutes${trend.provisional ? ' · provisional' : ''}`;
}
function applyTrendArrow(arrow, metric, trend) {
  if (!arrow) return;
  trend = trend || {};
  const convertedRate = convertForDisplay(trend.rate_per_hour, gaugeConfig?.[metric] || {}, true);
  const displayTrend = { ...trend, rate_per_hour: convertedRate };
  const hasTrend = convertedRate !== null && Number(trend.samples || 0) >= 6;
  const score = hasTrend ? trendScore(metric, convertedRate) : 0;
  const nextAngle = trendAngle(score);
  const key = `${arrow.closest('.metric-container')?.dataset.sensor || ''}::${metric}`;
  const previous = metricTrendAngles[key];
  arrow.dataset.metric = metric;
  arrow.style.opacity = hasTrend ? '1' : '.35';
  arrow.style.transform = `rotate(${previous === undefined ? nextAngle : previous}deg)`;
  arrow.title = trendTitle(metric, displayTrend, score);
  arrow.setAttribute('aria-label', arrow.title);
  requestAnimationFrame(() => { arrow.style.transform = `rotate(${nextAngle}deg)`; });
  metricTrendAngles[key] = nextAngle;
}
function initializeTrendArrows() {
  document.querySelectorAll('.trend-arrow').forEach(arrow => {
    applyTrendArrow(arrow, arrow.dataset.metric || '', {
      rate_per_hour: arrow.dataset.rate === '' ? null : Number(arrow.dataset.rate),
      samples: Number(arrow.dataset.samples || 0),
      window_s: Number(arrow.dataset.window || 0),
      provisional: arrow.dataset.provisional === '1'
    });
  });
}
function _normSwitchId(id) {
  return String(id || '').trim().toLowerCase();
}
function _asStringSet(items) {
  const out = new Set();
  (items || []).forEach(v => {
    const s = String(v || '').trim();
    if (s) out.add(s);
  });
  return out;
}
function _sameStringSet(a, b) {
  if (a.size !== b.size) return false;
  for (const v of a) { if (!b.has(v)) return false; }
  return true;
}
function _renderedMetricsForSensor(sid) {
  const row = document.getElementById(`row_${sid}`);
  if (!row) return [];
  return Array.from(row.querySelectorAll('.metric-container[data-metric]'))
    .map(el => String(el.dataset.metric || '').trim())
    .filter(Boolean);
}
function _renderedSwitchIds() {
  return Array.from(document.querySelectorAll('.switch-metric-container')).flatMap((card) => {
    const raw = String(card.dataset.switchIds || '').trim();
    if (raw) {
      return raw.split(',').map(_normSwitchId).filter(Boolean);
    }
    const header = card.querySelector('h3[id$="_header"]');
    if (!header) return [];
    const fallback = _normSwitchId(String(header.id || '').replace(/_header$/, ''));
    return fallback ? [fallback] : [];
  });
}
function _layoutSignature(available, nextExpMap, renderableSwitches) {
  const sensors = (available || []).map(sid => ({
    sid: String(sid || ''),
    metrics: (Array.isArray(nextExpMap?.[sid]) ? nextExpMap[sid] : []).map(m => String(m || '').trim()).filter(Boolean).sort()
  })).sort((a,b) => a.sid.localeCompare(b.sid));
  const switches = (renderableSwitches || []).map(_normSwitchId).filter(Boolean).sort();
  return JSON.stringify({ sensors, switches });
}
function hasOpenBackdropModal() {
  return !!document.querySelector('.modal-backdrop .modal, .modal-backdrop');
}
function dashboardRefreshPaused() {
  const options = arguments.length ? arguments[0] : null;
  const ignoreVisibility = !!(options && options.ignoreVisibility);
  const ignoreModal = !!(options && options.ignoreModal);
  return !!(window.ModalBusyCursor && window.ModalBusyCursor.isBusy && window.ModalBusyCursor.isBusy())
    || document.documentElement.classList.contains('modal-busy-cursor')
    || document.body.classList.contains('modal-busy-cursor')
    || (!ignoreVisibility && document.visibilityState === 'hidden')
    || (!ignoreModal && hasOpenBackdropModal());
}
function scheduleLayoutRefresh(reason, sig) {
  if (pendingLayoutRefresh) return true;
  if (hasOpenBackdropModal()) {
    if (deferredLayoutRefresh) return true;
    deferredLayoutRefresh = true;
    console.info('[layout-refresh-deferred]', reason || 'layout changed');
    setTimeout(() => {
      deferredLayoutRefresh = false;
      scheduleLayoutRefresh(reason, sig);
    }, 1000);
    return true;
  }
  deferredLayoutRefresh = false;
  try {
    const prevSig = sessionStorage.getItem('layoutRefreshSig') || '';
    const prevAtRaw = sessionStorage.getItem('layoutRefreshSigAt') || '0';
    const doneSig = sessionStorage.getItem('layoutRefreshDoneSig') || '';
    const prevAt = Number.parseInt(prevAtRaw, 10) || 0;
    const nowMs = Date.now();
    if (sig && doneSig === sig) {
      const suppressedSig = sessionStorage.getItem('layoutRefreshSuppressedSig') || '';
      if (suppressedSig !== sig) console.info('[layout-refresh-suppressed]', reason || 'layout changed');
      sessionStorage.setItem('layoutRefreshSuppressedSig', sig);
      return false;
    }
    if (sig && prevSig === sig && (nowMs - prevAt) < 20000) return false;
    if (sig) sessionStorage.setItem('layoutRefreshSig', sig);
    if (sig) sessionStorage.setItem('layoutRefreshDoneSig', sig);
    sessionStorage.removeItem('layoutRefreshSuppressedSig');
    sessionStorage.setItem('layoutRefreshSigAt', String(nowMs));
  } catch (_) {}
  pendingLayoutRefresh = true;
  console.info('[layout-refresh]', reason || 'layout changed');
  setTimeout(() => window.location.reload(), 350);
  return true;
}
function shouldRefreshForLayoutDrift(available, nextExpMap, renderableSwitches, selectedView) {
  for (const sid of (available || [])) {
    const expected = _asStringSet(Array.isArray(nextExpMap?.[sid]) ? nextExpMap[sid] : []);
    if (!expected.size) continue;
    const row = document.getElementById(`row_${sid}`);
    if (!row) return { reason: `sensor:${sid}` };
    const rendered = _asStringSet(_renderedMetricsForSensor(sid));
    if (!_sameStringSet(expected, rendered)) return { reason: `metrics:${sid}` };
  }
  const expectedSwitches = new Set((renderableSwitches || []).map(_normSwitchId).filter(Boolean));
  if (expectedSwitches.size) {
    const renderedSwitches = new Set(_renderedSwitchIds());
    for (const swId of expectedSwitches) {
      if (!renderedSwitches.has(swId)) return { reason: `switch:${swId}` };
    }
  }
  return null;
}

window.__updateGaugesInFlight = window.__updateGaugesInFlight || false;
window.__updateGaugesQueued = window.__updateGaugesQueued || false;
window.__updateGaugesStartedAt = window.__updateGaugesStartedAt || 0;
window.__updateGaugesRunSeq = window.__updateGaugesRunSeq || 0;
window.__updateGaugesFinishedSeq = window.__updateGaugesFinishedSeq || 0;
window.__updateGaugesFinishedAt = window.__updateGaugesFinishedAt || 0;
window.__updateGaugesLastDurationMs = window.__updateGaugesLastDurationMs || 0;
window.__updateGaugesLastOk = window.__updateGaugesLastOk || false;
window.__updateGaugesLastError = window.__updateGaugesLastError || '';
function finishUpdateGaugesRun(options, result) {
  const finishedAt = Date.now();
  const startedAt = window.__updateGaugesStartedAt || finishedAt;
  window.__updateGaugesFinishedSeq = window.__updateGaugesRunSeq || window.__updateGaugesFinishedSeq || 0;
  window.__updateGaugesFinishedAt = finishedAt;
  window.__updateGaugesLastDurationMs = Math.max(0, finishedAt - startedAt);
  window.__updateGaugesLastOk = !!(result && result.ok);
  window.__updateGaugesLastError = String((result && result.error) || '');
  const refreshStatus = document.getElementById('dashboard_refresh_status');
  if (refreshStatus) refreshStatus.hidden = window.__updateGaugesLastOk;
  window.__updateGaugesInFlight = false;
  window.__updateGaugesStartedAt = 0;
  if (window.__updateGaugesQueued) {
    window.__updateGaugesQueued = false;
    setTimeout(() => updateGauges(options || {}), 0);
  }
}
async function updateGauges() {
  const options = arguments.length ? arguments[0] : null;
  const ignoreVisibility = !!(options && options.ignoreVisibility);
  const ignoreModal = !!(options && options.ignoreModal);
  if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused({ ignoreVisibility, ignoreModal })) {
    return;
  }
  const nowForInflight = Date.now();
  if (window.__updateGaugesInFlight) {
    window.__updateGaugesQueued = true;
    return;
  }
  window.__updateGaugesInFlight = true;
  window.__updateGaugesStartedAt = nowForInflight;
  window.__updateGaugesRunSeq = (window.__updateGaugesRunSeq || 0) + 1;
  const finishOptions = { ignoreVisibility, ignoreModal };
  try {
  const sensorIdEl = document.getElementById('sensor_id');
  const sensorId = sensorIdEl ? sensorIdEl.value : 'All';
  let d = null;
  try {
    d = await fetchWithTimeout(window.location.pathname + '?json_only=true&sensor_id=' + encodeURIComponent(sensorId), { cache: 'no-store' }, dashboardFetchTimeoutMs, async res => {
      if (!res.ok) throw new Error(`http_${res.status}`);
      return await res.json();
    });
    if (!d || typeof d !== 'object' || !d.values || typeof d.values !== 'object' || Array.isArray(d.values)) throw new Error('Invalid dashboard response');
    for (const [sid, latest] of Object.entries(d.values || {})) currentValues[sid] = Object.assign({}, latest);
    let pressureContextChanged = false;
    if (Object.prototype.hasOwnProperty.call(d, 'pressure_altitude') && d.pressure_altitude !== pressureAltitude) {
      pressureAltitude = d.pressure_altitude;
      pressureContextChanged = true;
    }
    if (d.pressure_sensor_context && JSON.stringify(d.pressure_sensor_context) !== JSON.stringify(pressureSensorContext)) {
      pressureSensorContext = d.pressure_sensor_context;
      pressureContextChanged = true;
    }
    if (pressureContextChanged) initGauge();
    __lastJsonOnly = d;
    __lastJsonOnlyAtMs = Date.now();
    try {
      if (typeof applySensorStatuses === 'function') applySensorStatuses(d);
    } catch (e) {
      console.warn('updateGauges: status update failed', e);
    }
    if (typeof refreshAndApplySwitchStatus === 'function') {
      try {
        const nowMs = Date.now();
        if (!window.__lastSwitchStatusFromGaugesAt || (nowMs - window.__lastSwitchStatusFromGaugesAt) >= 12000) {
          window.__lastSwitchStatusFromGaugesAt = nowMs;
          setTimeout(() => refreshAndApplySwitchStatus(), 0);
        }
      } catch (e) {
        console.warn('updateGauges: switch status scheduling failed', e);
      }
    }
  } catch (e) {
    console.warn('updateGauges: fetch failed', e);
    finishUpdateGaugesRun(finishOptions, { ok: false, error: String(e) });
    return;
  }

  const available = Array.isArray(d.available) ? d.available : [];
  const nextExpMap = d.expected_gauge_map || {};
  const nextStyleMap = d.expected_display_style_map || {};
  Object.assign(metricTileStyleMap, d.metric_tile_style_map || {});
  const renderableSwitches = Array.isArray(d.renderable_switches_view) ? d.renderable_switches_view : (Array.isArray(d.renderable_switches) ? d.renderable_switches : []);
  const locations  = d.locations || {};
  let layoutDrift = null;
  try {
    layoutDrift = shouldRefreshForLayoutDrift(available, nextExpMap, renderableSwitches, sensorId);
  } catch (e) {
    console.warn('updateGauges: layout drift check failed', e);
  }
  if (layoutDrift) {
    const sig = _layoutSignature(available, nextExpMap, renderableSwitches);
    const reason = String(layoutDrift.reason || '');
    if (reason.startsWith('switch:')) {
      const nowMs = Date.now();
      if ((nowMs - __lastSuppressedSwitchLayoutAt) > 5000) {
        __lastSuppressedSwitchLayoutAt = nowMs;
        console.info('[layout-refresh-switch]', reason);
      }
      if (scheduleLayoutRefresh(reason, sig)) { finishUpdateGaugesRun(finishOptions, { ok: true }); return; }
    } else {
      if (scheduleLayoutRefresh(layoutDrift.reason, sig)) { finishUpdateGaugesRun(finishOptions, { ok: true }); return; }
    }
  } else {
    try { sessionStorage.removeItem('layoutRefreshDoneSig'); sessionStorage.removeItem('layoutRefreshSuppressedSig'); } catch (_) {}
  }


  for (const sid of available) {
    try {
    const metrics = Array.isArray(nextExpMap[sid]) ? nextExpMap[sid] : [];
    if (!metrics.length) continue;

    if (!knownSensors.has(sid)) {
      ensureSensorInSelector(sid);
      expectedGaugeMap[sid] = metrics;
      expectedDisplayStyleMap[sid] = nextStyleMap[sid] || expectedDisplayStyleMap[sid] || {};
      ensureSensorUI(sid, metrics, locations[sid]);
      try { initGauge(); }
      catch (e) { console.error('initGauge() failed for new sensor', sid, e); }
      knownSensors.add(sid);
      refreshOnceAfterSensorAdded();
    } else {
      const needsNewUI = metrics.some(m => {
        const safeM = (typeof toSafe === 'function') ? toSafe(m) : m.replace(/[^a-zA-Z0-9_\-]/g,'_');
        return !document.getElementById(`${sid}_${safeM}_container`);
      });
      if (needsNewUI) {
        expectedGaugeMap[sid] = metrics;
        expectedDisplayStyleMap[sid] = nextStyleMap[sid] || expectedDisplayStyleMap[sid] || {};
        ensureSensorUI(sid, metrics, locations[sid]);
        try { initGauge(); }
        catch (e) { console.error('initGauge() failed while extending metrics', sid, e); }
      }
      const styleMap = nextStyleMap[sid] || {};
      metrics.forEach((metric, idx) => {
        const safeM = (typeof toSafe === 'function') ? toSafe(metric) : metric.replace(/[^a-zA-Z0-9_\-]/g,'_');
        const container = document.getElementById(`${sid}_${safeM}_container`);
        if (!container) return;
        container.dataset.tileStyle = metricTileStyleMap[sid] || 'Theme';
        if (container.dataset.userDisplayStyle === '1') return;
        const nextStyle = window.normalizeDisplayStyle(styleMap[`METRIC_${idx + 1}`] || container.dataset.displayStyle || window.displayStyle || 'Gauge');
        container.dataset.displayStyle = nextStyle;
        if (typeof window.registerContainerStyle === 'function') {
          window.registerContainerStyle(container, nextStyle);
        }
      });
    }
    } catch (e) {
      console.warn('updateGauges: sensor UI sync failed', sid, e);
    }
  }
  if (Array.isArray(d.dashboard_row_order)) window.applySensorGroupOrder(d.dashboard_row_order);

  const values     = d.values      || {};
  const stats      = d.stats       || {};
  const timestamps = d.timestamps  || {};

  let dataChanged = false;
  for (const sid in timestamps) {
    const newTs = timestamps[sid];
    const oldTs = lastTimestamps[sid];
    if (newTs && newTs !== oldTs) {
      dataChanged = true;
      lastTimestamps[sid] = newTs;
    }
  }
  if (dataChanged) lastSensorTimestampChangeMs = Date.now();

  for (const sid in values) {
    try {
      const vset = values[sid] || {};
      const sset = stats[sid]  || {};
      for (const metric in vset) {
        try {
      const safe = `${sid}_${toSafe(metric)}`;
      const val  = vset[metric];
      const labelEl = document.getElementById(`${safe}_val`);
      const g       = window[`${safe}_gauge`];
      const metricConfig = pressureGaugeConfig(metric, gaugeConfig?.[metric], pressureAltitude, pressureSensorContext[sid]) || {};
      const renderMode = ((metricConfig.render) || '').toLowerCase();
      const valueMetric = metricConfig.value_metric || metric;
      const statsMetric = metricConfig.stats_metric || metric;
      const valueConfig = gaugeConfig?.[valueMetric] || metricConfig;
      if (labelEl) {
        if (renderMode === 'compass') {
          const displayValue = vset[valueMetric];
          labelEl.textContent = formatCurrentValue(displayValue, valueConfig);
        } else {
          labelEl.textContent = formatCurrentValue(val, metricConfig);
        }
      }
      if (renderMode === 'compass') {
        const compassCanvas = window[`${safe}_compass`] || document.getElementById(`${safe}Gauge`);
        drawCompassGauge(compassCanvas, val);
      } else if (typeof val === 'number') {
        if (g && typeof g.set === 'function') {
          try { g.set(convertForDisplay(val, metricConfig) ?? 0); } catch (e) { console.warn('Gauge set() failed', safe, e); }
        } else {
          drawFallbackGauge(document.getElementById(`${safe}Gauge`), convertForDisplay(val, metricConfig) ?? 0, metricConfig);
        }
      }

      const stat = sset[statsMetric] || {};
      const trendArrowEl = labelEl?.parentElement?.querySelector('.trend-arrow');
      applyTrendArrow(trendArrowEl, statsMetric, stat.trend);
      const min = formatStatValue(stat.min, valueConfig);
      const avg = formatStatValue(stat.avg, valueConfig);
      const max = formatStatValue(stat.max, valueConfig);

      let min_ts = (stat.min_ts !== undefined && stat.min_ts !== null) ? String(stat.min_ts) : '--';
      let max_ts = (stat.max_ts !== undefined && stat.max_ts !== null) ? String(stat.max_ts) : '--';
      if (min_ts !== '--') min_ts = formatIsoForStats(min_ts);
      if (max_ts !== '--') max_ts = formatIsoForStats(max_ts);

      const blk = document.getElementById(`${safe}_stats`);
      if (blk) blk.innerHTML = renderStatsHtml(min, avg, max, min_ts, max_ts);
        } catch (e) {
          console.warn('updateGauges: metric update failed', sid, metric, e);
        }
      }
    } catch (e) {
      console.warn('updateGauges: sensor update failed', sid, e);
    }
  }

  const ts = document.getElementById('update_time');
  if (ts) {
    ts.classList.remove('flash-green', 'flash-red');
    const sensorTimestampStale = (Date.now() - lastSensorTimestampChangeMs) >= sensorTimestampStaleMs;
    const flashClass = dataChanged ? 'flash-green' : (isPiPlatform && sensorTimestampStale ? 'flash-red' : '');
    if (flashClass) {
      void ts.offsetWidth;
      ts.classList.add(flashClass);
      setTimeout(() => ts.classList.remove(flashClass), 1000);
    }
  }
  if (dataChanged && typeof window.refreshAllMicrographs === 'function') {
    window.refreshAllMicrographs(true, { ignoreModal });
  }
  if (dataChanged && typeof window.notifyFullscreenGraphDataChanged === 'function') {
    window.notifyFullscreenGraphDataChanged('sensor');
  }
  finishUpdateGaugesRun(finishOptions, { ok: true });
  } catch (e) {
    console.warn('updateGauges: update failed', e);
    finishUpdateGaugesRun(finishOptions, { ok: false, error: String(e) });
  }

}
window.updateGauges = updateGauges;
function refreshOnceAfterSensorAdded() {
  try {
    if (!sessionStorage.getItem('didAutoRefreshAfterAdd')) {
        sessionStorage.setItem('didAutoRefreshAfterAdd', '1');
        setTimeout(() => window.location.reload(), 400);
    }
  } catch (e) {
    setTimeout(() => window.location.reload(), 400);
  }
}
window.addEventListener('load', () => {
  sessionStorage.removeItem('didAutoRefreshAfterAdd');
});
const gaugeZonesBackgroundMicro = {
  id: 'gaugeZonesBackgroundMicro',
  beforeDraw(chart){
    const { ctx, chartArea, scales, options } = chart;
    const zones = options?.plugins?.micrographZones;
    if (!Array.isArray(zones) || !zones.length) return;
    const y = scales?.y || Object.values(scales).find(s => s.type==='linear');
    if (!y || !chartArea) return;
    const topBound = Math.min(chartArea.top, chartArea.bottom);
    const bottomBound = Math.max(chartArea.top, chartArea.bottom);
    ctx.save();
    zones.forEach(z => {
      if (!z) return;
      const zMin = Number(z.min);
      const zMax = Number(z.max);
      const color = z.color || z.strokeStyle;
      if (!Number.isFinite(zMin) || !Number.isFinite(zMax) || !color) return;
      const rawTop = y.getPixelForValue(zMax);
      const rawBottom = y.getPixelForValue(zMin);
      const yTop = Math.max(topBound, Math.min(bottomBound, Math.min(rawTop, rawBottom)));
      const yBottom = Math.max(topBound, Math.min(bottomBound, Math.max(rawTop, rawBottom)));
      if (yBottom <= yTop) return;
      ctx.fillStyle = color;
      ctx.fillRect(chartArea.left, yTop, chartArea.right - chartArea.left, yBottom - yTop);
    });
    ctx.restore();
  }
};
if (window.Chart) { try { window.Chart.register(gaugeZonesBackgroundMicro); } catch(e){} }
window.__micrographDataCache = window.__micrographDataCache || new Map();
window.__micrographDataInflight = window.__micrographDataInflight || new Map();
window.__lastMicrographRefreshAt = window.__lastMicrographRefreshAt || 0;
window.__lastMicrographForceRefreshAt = window.__lastMicrographForceRefreshAt || 0;
const micrographCacheMaxEntries = 24;
const micrographCacheMaxBytes = 8 * 1024 * 1024;
const micrographCacheMaxAgeMs = 5 * 60 * 1000;
function trimMicrographDataCache(now = Date.now()) {
  const cache = window.__micrographDataCache;
  if (!(cache instanceof Map)) return;
  for (const [key, entry] of cache) {
    if (!entry || (now - Number(entry.at || 0)) > micrographCacheMaxAgeMs) cache.delete(key);
  }
  let totalBytes = 0;
  for (const entry of cache.values()) totalBytes += Number(entry && entry.bytes || 0);
  while (cache.size > micrographCacheMaxEntries || totalBytes > micrographCacheMaxBytes) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey === undefined) break;
    const oldest = cache.get(oldestKey);
    totalBytes -= Number(oldest && oldest.bytes || 0);
    cache.delete(oldestKey);
  }
}
async function getMicrographJson(requestKey, url, force) {
  const now = Date.now();
  const cacheTtlMs = 60000;
  trimMicrographDataCache(now);
  const cached = window.__micrographDataCache.get(requestKey);
  if (!force && cached && (now - cached.at) < cacheTtlMs) {
    window.__micrographDataCache.delete(requestKey);
    window.__micrographDataCache.set(requestKey, cached);
    return cached.data;
  }
  const existing = window.__micrographDataInflight.get(requestKey);
  if (existing) {
    const existingPromise = existing.promise || existing;
    const existingAt = Number(existing.at || 0);
    if (!force && (!existingAt || (now - existingAt) < micrographInflightStaleMs)) {
      return existingPromise;
    }
    window.__micrographDataInflight.delete(requestKey);
  }
  const promise = fetchWithTimeout(url, { cache: 'no-store' }, micrographFetchTimeoutMs).then(async (resp) => {
    if (!resp.ok) {
      throw new Error('micrograph_http_' + resp.status);
    }
    const data = await resp.json();
    const contentLength = Number(resp.headers.get('content-length') || 0);
    const bytes = contentLength > 0 ? contentLength : JSON.stringify(data).length;
    window.__micrographDataCache.delete(requestKey);
    window.__micrographDataCache.set(requestKey, { at: Date.now(), bytes, data });
    trimMicrographDataCache();
    return data;
  }).finally(() => {
    window.__micrographDataInflight.delete(requestKey);
  });
  window.__micrographDataInflight.set(requestKey, { promise, at: now });
  return promise;
}

function renderWindRoseMicrograph(canvas, directionSeries, speedSeries, rangeLabel) {
  if (!canvas || !directionSeries || !speedSeries) return false;
  const directionTs = Array.isArray(directionSeries.ts) ? directionSeries.ts : [];
  const directions = Array.isArray(directionSeries.vals) ? directionSeries.vals : [];
  const speedTs = Array.isArray(speedSeries.ts) ? speedSeries.ts : [];
  const speeds = Array.isArray(speedSeries.vals) ? speedSeries.vals : [];
  const speedByTimestamp = new Map();
  speedTs.forEach((ts, index) => {
    const speed = Number(speeds[index]);
    if (Number.isFinite(speed)) speedByTimestamp.set(String(ts), speed);
  });
  const baseSpeedBands = [
    { min: 0, max: 5, label: '0-5', color: '#75bce8' },
    { min: 5, max: 15, label: '5-15', color: '#5290bf' },
    { min: 15, max: 30, label: '15-30', color: '#2e6396' },
    { min: 30, max: Infinity, label: '30+', color: '#0b376d' }
  ];
  const speedConfig = gaugeConfig?.['Wind Speed'] || {};
  const speedUnit = String(speedConfig.unit || speedConfig.source_unit || 'mph').trim();
  const convertSpeedBandLimit = value => Number.isFinite(value)
    ? Math.ceil(convertForDisplay(value, speedConfig) ?? value)
    : Infinity;
  const speedBands = baseSpeedBands.map(band => {
    const min = convertSpeedBandLimit(band.min);
    const max = convertSpeedBandLimit(band.max);
    return { ...band, min, max, label: Number.isFinite(max) ? `${min}-${max}` : `${min}+` };
  });
  const directionNames = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  const bins = directionNames.map(() => speedBands.map(() => 0));
  let sampleCount = 0;
  let speedTotal = 0;
  directions.forEach((rawDirection, index) => {
    const direction = Number(rawDirection);
    let rawSpeed = speedByTimestamp.get(String(directionTs[index]));
    if (!Number.isFinite(rawSpeed) && directions.length === speeds.length) rawSpeed = Number(speeds[index]);
    const speed = convertForDisplay(rawSpeed, speedConfig);
    if (!Number.isFinite(direction) || !Number.isFinite(speed) || speed < 0) return;
    const normalized = ((direction % 360) + 360) % 360;
    const directionIndex = Math.floor((normalized + 11.25) / 22.5) % 16;
    const speedIndex = speedBands.findIndex(band => speed >= band.min && speed < band.max);
    if (speedIndex < 0) return;
    bins[directionIndex][speedIndex] += 1;
    sampleCount += 1;
    speedTotal += speed;
  });
  if (!sampleCount) return false;
  const oldChart = chartMap.get(canvas);
  if (oldChart && typeof oldChart.destroy === 'function') oldChart.destroy();
  chartMap.delete(canvas);
  const ctx = canvas.getContext('2d');
  const width = Number(canvas.width) || 260;
  const height = Number(canvas.height) || 205;
  ctx.clearRect(0, 0, width, height);
  const style = window.getComputedStyle ? window.getComputedStyle(canvas) : null;
  const textColor = (style && style.color) || '#f2f6fb';
  const centerX = width / 2;
  const legendHeight = 30;
  const centerY = (height - legendHeight) / 2 + 2;
  const radius = Math.max(34, Math.min(width / 2 - 25, (height - legendHeight) / 2 - 16));
  const innerRadius = Math.max(13, radius * 0.23);
  const directionTotals = bins.map(parts => parts.reduce((sum, count) => sum + count, 0));
  const maxDirectionCount = Math.max(1, ...directionTotals);
  ctx.save();
  ctx.strokeStyle = 'rgba(130,160,195,0.28)';
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75, 1].forEach(fraction => {
    ctx.beginPath();
    ctx.arc(centerX, centerY, innerRadius + (radius - innerRadius) * fraction, 0, Math.PI * 2);
    ctx.stroke();
  });
  const sectorHalfWidth = (Math.PI * 2 / 16) * 0.39;
  bins.forEach((parts, directionIndex) => {
    const centerAngle = directionIndex * Math.PI * 2 / 16 - Math.PI / 2;
    let cumulative = 0;
    parts.forEach((count, speedIndex) => {
      if (!count) return;
      const previous = cumulative;
      cumulative += count;
      const inner = innerRadius + (radius - innerRadius) * previous / maxDirectionCount;
      const outer = innerRadius + (radius - innerRadius) * cumulative / maxDirectionCount;
      const startAngle = centerAngle - sectorHalfWidth;
      const endAngle = centerAngle + sectorHalfWidth;
      ctx.beginPath();
      ctx.arc(centerX, centerY, outer, startAngle, endAngle);
      ctx.arc(centerX, centerY, inner, endAngle, startAngle, true);
      ctx.closePath();
      ctx.fillStyle = speedBands[speedIndex].color;
      ctx.fill();
    });
  });
  const cardinals = [
    ['N', centerX, centerY - radius - 8],
    ['E', centerX + radius + 10, centerY + 3],
    ['S', centerX, centerY + radius + 12],
    ['W', centerX - radius - 10, centerY + 3]
  ];
  ctx.fillStyle = textColor;
  ctx.font = '600 11px system-ui, sans-serif';
  ctx.textAlign = 'center';
  cardinals.forEach(item => ctx.fillText(item[0], item[1], item[2]));
  const dominantIndex = directionTotals.indexOf(Math.max(...directionTotals));
  const dominantPercent = Math.round(directionTotals[dominantIndex] * 100 / sampleCount);
  ctx.beginPath();
  ctx.arc(centerX, centerY, innerRadius - 1, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(15,23,36,0.92)';
  ctx.fill();
  ctx.fillStyle = '#f2f6fb';
  ctx.font = '600 10px system-ui, sans-serif';
  ctx.fillText(directionNames[dominantIndex], centerX, centerY - 1);
  ctx.font = '9px system-ui, sans-serif';
  ctx.fillText(String(dominantPercent) + '%', centerX, centerY + 9);
  const legendY = height - 10;
  const legendItemWidth = Math.min(58, (width - 12) / speedBands.length);
  const legendStartX = centerX - legendItemWidth * speedBands.length / 2;
  ctx.font = '9px system-ui, sans-serif';
  ctx.textAlign = 'left';
  speedBands.forEach((band, index) => {
    const x = legendStartX + index * legendItemWidth;
    ctx.fillStyle = band.color;
    ctx.fillRect(x, legendY - 8, 9, 9);
    ctx.fillStyle = textColor;
    ctx.fillText(band.label, x + 12, legendY);
  });
  ctx.textAlign = 'right';
  ctx.fillText(speedUnit, width - 3, legendY);
  ctx.restore();
  const averageSpeed = speedTotal / sampleCount;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', `${rangeLabel} wind rose. Dominant direction ${directionNames[dominantIndex]} at ${dominantPercent} percent. Average wind speed ${averageSpeed.toFixed(1)} ${speedUnit}.`);
  canvas.title = `${rangeLabel} wind rose — ${sampleCount} samples — dominant ${directionNames[dominantIndex]} ${dominantPercent}% — average ${averageSpeed.toFixed(1)} ${speedUnit}`;
  return true;
}
window.renderWindRoseMicrograph = renderWindRoseMicrograph;

async function showMicrographForContainer(container, options = {}) {
  if (!container) return;
  const forceFetch = !!(options && options.force);
  const ignoreModal = !!(options && options.ignoreModal);
  if (!forceFetch && typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused({ ignoreModal })) {
    return;
  }
  if (typeof updateContainerDisplayStyle === 'function') {
    updateContainerDisplayStyle(container);
  }
  const gauge = container.querySelector('.gauge-container');
  const graph = container.querySelector('.graph-container');
  const canvas = container.querySelector('.micrograph-canvas');
  if (!canvas || !gauge || !graph) { return; }

  let token = '';
  try {
    const sensor = (container.dataset.sensor || '').trim();
    const metric = (container.dataset.metric || '').trim();
    if (!sensor || !metric) {
      return;
    }

    let style = 'Graph24hr';
    if (typeof window.getContainerStyle === 'function') {
      style = window.getContainerStyle(container);
    }

    let range = '24h';
    let xTitleText = '24 Hours';
    if (style === 'Graph6hr') {
      range = '6h';
      xTitleText = '6 Hours';
    } else if (style === 'Graph24hr') {
      range = '24h';
      xTitleText = '24 Hours';
    } else if (style === 'Gauge') {
      graph.style.display = 'none';
      gauge.style.display = 'block';
      canvas.style.cursor = 'default';
      return;
    }

    graph.style.display = 'block';
    gauge.style.display = 'none';

    const requestKey = sensor + '::' + metric + '::' + range;
    if (!forceFetch && canvas.dataset.micrographInflight === requestKey) {
      const inflightAt = Number.parseInt(canvas.dataset.micrographInflightAt || '0', 10) || 0;
      if (!inflightAt || (Date.now() - inflightAt) < micrographInflightStaleMs) {
      canvas.style.cursor = 'default';
      return;
      }
      canvas.dataset.micrographInflight = '';
      canvas.dataset.micrographInflightAt = '';
      container.dataset.micrographLoading = '0';
    }
    token = String(Date.now()) + ':' + String(Math.random());
    canvas.dataset.micrographToken = token;
    canvas.dataset.micrographInflight = requestKey;
    canvas.dataset.micrographInflightAt = String(Date.now());
    container.dataset.micrographLoading = '1';
    canvas.style.cursor = 'wait';

    const metricNormForRequest = String(metric || '').toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    const isWindDirectionRequest = metricNormForRequest === 'wind direction';
    let url = '/graph-data?sensor_id=' + encodeURIComponent(sensor)
              + '&metric1=' + encodeURIComponent(metric)
              + '&range=' + encodeURIComponent(range);
    if (isWindDirectionRequest) {
      url += '&sensor_id2=' + encodeURIComponent(sensor) + '&metric2=' + encodeURIComponent('Wind Speed');
    }
    const jsonData = await getMicrographJson(requestKey, url, forceFetch);
    if (canvas.dataset.micrographToken !== token) {
      return;
    }
    const liveStyle = (typeof window.getContainerStyle === 'function') ? window.getContainerStyle(container) : style;
    if (liveStyle !== style || liveStyle === 'Gauge') {
      if (liveStyle === 'Gauge') {
        graph.style.display = 'none';
        gauge.style.display = 'block';
      }
      return;
    }
    const allSeries = jsonData.series || {};
    const entries = Object.entries(allSeries);
    if (!entries.length) {
      return;
    }

    const exactKey = sensor + '::' + metric;
    const suffix = '::' + metric;
    let chosen = null;

    for (const kv of entries) {
      const k = kv[0];
      const v = kv[1];
      if (k === exactKey) { chosen = [k, v]; break; }
    }

    if (!chosen) {
      for (const kv of entries) {
        const k = kv[0];
        const v = kv[1];
        if (k.endsWith(suffix)) { chosen = [k, v]; break; }
      }
    }

    if (!chosen) {
      chosen = entries[0];
    }

    const seriesObj = chosen[1] || {};
    const labels = seriesObj.ts || [];
    const values = seriesObj.vals || [];
    const avgAll = (jsonData && (jsonData.simple_avg || jsonData.rolling_ema)) || {};
    const avgObj = (avgAll && avgAll[chosen[0]]) || {};
    const avgTs = avgObj.ts || [];
    const avgVals = avgObj.vals || [];

    if (!values.length) {
      return;
    }
    if (canvas.dataset.micrographToken !== token) {
      return;
    }

    const metricName = container.dataset.metric || metric;
    const metricNorm = String(metricName || '').toLowerCase().replace(/[_-]+/g, ' ');
    const isWindDirection = metricNorm.replace(/\s+/g, ' ').trim() === 'wind direction';
    if (isWindDirection) {
      const speedKey = sensor + '::Wind Speed';
      let speedSeries = allSeries[speedKey] || null;
      if (!speedSeries) {
        const speedEntry = entries.find(kv => String(kv[0] || '').toLowerCase().endsWith('::wind speed'));
        speedSeries = speedEntry ? speedEntry[1] : null;
      }
      if (renderWindRoseMicrograph(canvas, seriesObj, speedSeries, xTitleText)) {
        graph.style.display = 'block';
        gauge.style.display = 'none';
        return;
      }
    }
    const isSoilFertilityIndex = metricNorm === 'soil fertility index' || metricNorm.endsWith(' soil fertility index');
    const isBarometricPressure = pressureTrendMetric(metricName);
    const metricConfig = gaugeConfig?.[metricName] || gaugeConfig?.[metric] || (isSoilFertilityIndex ? gaugeConfig?.['Soil Fertility Index'] : null);
    const displayValues = values.map(value => convertForDisplay(value, metricConfig));
    const displayAvgVals = avgVals.map(value => convertForDisplay(value, metricConfig));
    const metricUnit = String(metricConfig?.unit || '').trim();
    const metricZones = Array.isArray(metricConfig?.zones) ? metricConfig.zones.map(z => ({
      min: Number(z?.min),
      max: Number(z?.max),
      color: z?.strokeStyle || z?.color || ''
    })).filter(z => Number.isFinite(z.min) && Number.isFinite(z.max) && z.color) : [];
    const isLightMetric = ['Light Intensity', 'Auto Light', 'Visible Light Intensity', 'Estimated PPFD'].includes(metricName);
    const graphLineColor = isLightMetric ? '#000000' : '#ffffff';
    const fixedThreeHourTicks = style === 'Graph24hr';

    const formatYAxisTick = (val) => {
      const num = Number(val);
      if (!Number.isFinite(num)) return val;
      if (isBarometricPressure) return num.toFixed(1);
      return Number(num.toFixed(2)).toString();
    };
    const formatXAxisTick = (val) => {
      const date = new Date(val);
      if (!Number.isFinite(date.getTime())) return '';
      const pad2 = (part) => String(part).padStart(2, '0');
      if (date.getHours() === 0 && date.getMinutes() === 0) {
        const month = date.toLocaleString('en-US', { month: 'short' });
        return month + pad2(date.getDate());
      }
      const hour24 = date.getHours();
      const hour12 = ((hour24 + 11) % 12) + 1;
      const minute = date.getMinutes();
      const clock = minute === 0 ? String(hour12) : hour12 + ':' + pad2(minute);
      return clock + (hour24 < 12 ? ' AM' : ' PM');
    };

    const panelTickColor = ['Chrome', 'Brass', 'Mahogany & Brass'].includes(container.dataset.tileStyle) ? '#17232b' : undefined;
    const yScaleOptions = { title: { display: false }, ticks: { callback: formatYAxisTick, color: panelTickColor } };
    if (isSoilFertilityIndex) {
      const cfgMin = Number(metricConfig?.min);
      const cfgMax = Number(metricConfig?.max);
      if (Number.isFinite(cfgMin) && Number.isFinite(cfgMax) && cfgMax > cfgMin) {
        yScaleOptions.min = cfgMin;
        yScaleOptions.max = cfgMax;
      }
    }

    const chartOptions = {
      responsive: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { enabled: true, callbacks: { label: (context) => {
          const num = Number(context.parsed?.y);
          if (!Number.isFinite(num)) return '';
          const text = isBarometricPressure ? num.toFixed(1) : Number(num.toFixed(2)).toString();
          return metricUnit ? `${text} ${metricUnit}` : text;
        } } },
        micrographZones: metricZones
      },
      scales: {
        x: {
          type: 'time',
          afterBuildTicks: function(axis) {
            if (!fixedThreeHourTicks) return;
            axis.ticks = (axis.ticks || []).filter((tick) => {
              const tickDate = new Date(tick.value);
              return Number.isFinite(tickDate.getTime()) && tickDate.getMinutes() === 0 && tickDate.getHours() % 3 === 0;
            });
          },
          time: {
            unit: 'hour',
            tooltipFormat: 'yyyy/MM/dd h:mm a',
            displayFormats: { hour: 'h a' }
          },
          title: { display: false },
          ticks: {
            display: true,
            autoSkip: !fixedThreeHourTicks,
            maxTicksLimit: fixedThreeHourTicks ? 9 : 6,
            major: { enabled: true },
            font: { size: 10 },
            callback: formatXAxisTick,
            color: panelTickColor
          },
          grid: { display: true }
        },
        y: yScaleOptions
      }
    };

    function _alignAvg(labels, avgTs, avgVals){
      if (!labels.length || !avgTs.length || !avgVals.length) return [];
      if (labels.length === avgVals.length) return avgVals;
      const map = new Map();
      for (let i = 0; i < avgTs.length; i++){
        map.set(avgTs[i], avgVals[i]);
      }
      return labels.map(ts => map.has(ts) ? map.get(ts) : null);
    }

    const alignedAvg = _alignAvg(labels, avgTs, displayAvgVals);
    const hasAvg = alignedAvg.length === labels.length && alignedAvg.some(v => v !== null && v !== undefined);

    const sparseRadius = (labels.length <= 1) ? 2 : 0;
    const datasets = [{
      data: displayValues,
      borderColor: graphLineColor,
      backgroundColor: 'rgba(255,255,255,1)',
      order: 1,
      pointRadius: sparseRadius,
      pointHoverRadius: Math.max(3, sparseRadius),
      tension: 0.3
    }];

    if (hasAvg) {
      datasets.push({
        data: alignedAvg,
        borderColor: graphLineColor,
        borderDash: [6, 3],
        order: 2,
        pointRadius: sparseRadius,
        pointHoverRadius: Math.max(3, sparseRadius),
        tension: 0.3
      });
    }

    let chart = chartMap.get(canvas);
    if (!chart) {
      const ctx = canvas.getContext('2d');
      chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: datasets
        },
        options: chartOptions
      });
      chartMap.set(canvas, chart);
    } else {
      chart.data.labels = labels;
      chart.data.datasets = datasets;
      chart.options = chartOptions;
      chart.update('none');
    }

    graph.style.display = 'block';
    gauge.style.display = 'none';
  } catch (e) {
    console.warn('showMicrographForContainer error', e);
  } finally {
    if (canvas.dataset.micrographToken === token) {
      canvas.dataset.micrographInflight = '';
      canvas.dataset.micrographInflightAt = '';
      container.dataset.micrographLoading = '0';
      canvas.style.cursor = 'default';
    }
  }
}
const chartMap = new WeakMap();
window.materializeMetricVisual = function(container) {
  if (!container || container.dataset.lazyMetricVisual !== '1') return false;
  const sensor = String(container.dataset.sensor || '').trim();
  const metric = String(container.dataset.metric || '').trim();
  if (!sensor || !metric) return false;
  const safe = `${sensor}_${toSafe(metric)}`;
  const gauge = container.querySelector('.gauge-container');
  const graph = container.querySelector('.graph-container');
  if (!gauge || !graph) return false;
  gauge.innerHTML = `<div class='gauge-view' id='${safe}GaugeContainer'><canvas id='${safe}Gauge'></canvas></div>`;
  graph.innerHTML = `<div class='graph-view' id='${safe}GraphContainer'><canvas class='micrograph-canvas' id='${safe}Micrograph' width='${metricCanvasWidth}' height='${metricCanvasHeight}'></canvas></div>`;
  delete container.dataset.lazyMetricVisual;
  if (typeof window.bindMetricContainer === 'function') window.bindMetricContainer(container);
  return true;
};
window.materializeExpandedMetricVisuals = function(group) {
  if (!group) return;
  const cards = Array.from(group.querySelectorAll('.metric-container.sensor-metric-overflow'));
  let changed = false;
  cards.forEach((card) => { if (window.materializeMetricVisual(card)) changed = true; });
  if (!changed) return;
  if (typeof initGauge === 'function') initGauge();
  window.setTimeout(function() {
    cards.forEach((card) => {
      const style = typeof window.getContainerStyle === 'function' ? window.getContainerStyle(card) : 'Gauge';
      if ((style === 'Graph6hr' || style === 'Graph24hr') && typeof showMicrographForContainer === 'function') {
        showMicrographForContainer(card, { force: false, ignoreModal: true });
      }
    });
  }, 0);
};
window.releaseCollapsedMetricVisuals = function(group) {
  if (!group) return;
  group.querySelectorAll('.metric-container.sensor-metric-overflow').forEach((container) => {
    const sensor = String(container.dataset.sensor || '').trim();
    const metric = String(container.dataset.metric || '').trim();
    const safe = `${sensor}_${toSafe(metric)}`;
    const graphCanvas = container.querySelector('.micrograph-canvas');
    const chart = graphCanvas ? chartMap.get(graphCanvas) : null;
    if (chart && typeof chart.destroy === 'function') chart.destroy();
    if (graphCanvas) chartMap.delete(graphCanvas);
    const gaugeObject = window[`${safe}_gauge`];
    if (gaugeObject && gaugeObject.__sensoriusCanvas && container.contains(gaugeObject.__sensoriusCanvas)) {
      delete window[`${safe}_gauge`];
    }
    delete window[`${safe}_compass`];
    const gauge = container.querySelector('.gauge-container');
    const graph = container.querySelector('.graph-container');
    if (gauge) gauge.innerHTML = '';
    if (graph) graph.innerHTML = '';
    container.dataset.lazyMetricVisual = '1';
    window.__micrographDataCache.delete(`${sensor}::${metric}::6h`);
    window.__micrographDataCache.delete(`${sensor}::${metric}::24h`);
  });
};
const sensorUnits = config.sensorUnits;
window.handleMetricContainerClick = function(container) {
    const gauge = container.querySelector('.gauge-container');
    const graph = container.querySelector('.graph-container');
    const canvas = container.querySelector('.micrograph-canvas');
    if (!canvas || !gauge || !graph) { return; }
    const now = Date.now();
    const lastClick = Number.parseInt(container.dataset.metricClickAt || '0', 10) || 0;
    if ((now - lastClick) < 250) { return; }
    container.dataset.metricClickAt = String(now);
    let style = 'Gauge';
    if (typeof window.getContainerStyle === 'function') {
      style = window.getContainerStyle(container);
    }
    let nextStyle = 'Graph24hr';
    if (style === 'Graph24hr') {
      nextStyle = 'Graph6hr';
    } else if (style === 'Graph6hr') {
      nextStyle = 'Gauge';
    } else if (style === 'Gauge') {
      nextStyle = 'Graph24hr';
    } else {
      nextStyle = 'Graph24hr';
    }
    if (typeof window.registerContainerStyle === 'function') {
      window.registerContainerStyle(container, nextStyle);
    }
    container.dataset.userDisplayStyle = '1';
    if (nextStyle === 'Gauge') {
      canvas.dataset.micrographToken = 'gauge:' + String(Date.now());
      canvas.dataset.micrographInflight = '';
      canvas.dataset.micrographInflightAt = '';
      container.dataset.micrographLoading = '0';
      canvas.style.cursor = 'default';
      graph.style.display = 'none';
      gauge.style.display = 'block';
      if (typeof initGauge === 'function') {
        try { initGauge(); } catch (e) { console.warn('initGauge on view switch failed', e); }
      }
      return;
    }
    graph.style.display = 'block';
    gauge.style.display = 'none';
    showMicrographForContainer(container, { force: true });
};

window.bindMetricContainer = function(container) {
  if (!container || container.dataset.metricClickBound === '1') return;
  container.dataset.metricClickBound = '1';
  container.addEventListener('click', () => {
    if (window.handleMetricContainerClick) window.handleMetricContainerClick(container);
  });
};

document.querySelectorAll('.metric-container').forEach(container => {
  if (window.bindMetricContainer) window.bindMetricContainer(container);
});
(function() {
  const all = document.querySelectorAll('.metric-container');
  all.forEach(container => {
    const style = (typeof window.getContainerStyle === 'function')
      ? window.getContainerStyle(container)
      : window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');
    if (style === 'Graph6hr' || style === 'Graph24hr') {
      if (typeof window.registerContainerStyle === 'function') {
        window.registerContainerStyle(container, style);
      }
      window.__needsInitialMicrographRefresh = true;
    }
  });
})();
(function() {
  let lastRun = 0;
  const MIN_INTERVAL_MS = 60000;
  const MIN_FORCE_INTERVAL_MS = 5000;

  async function refreshAllMicrographs(force = false, options = {}) {
    const now = Date.now();
    const ignoreModal = !!(options && options.ignoreModal);
    if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused({ ignoreVisibility: !!force, ignoreModal })) {
      return;
    }
    if (force && window.__lastMicrographForceRefreshAt && (now - window.__lastMicrographForceRefreshAt) < MIN_FORCE_INTERVAL_MS) {
      return;
    }
    if (!force && (now - lastRun) < MIN_INTERVAL_MS) {
      return;
    }
    lastRun = now;
    window.__lastMicrographRefreshAt = now;
    if (force) window.__lastMicrographForceRefreshAt = now;

    const containers = document.querySelectorAll('.metric-container');
    for (const container of containers) {
      try {
        const gauge = container.querySelector('.gauge-container');
        const graph = container.querySelector('.graph-container');
        if (!gauge || !graph) continue;

        const desiredStyle = (typeof window.getContainerStyle === 'function')
          ? window.getContainerStyle(container)
          : window.normalizeDisplayStyle(container.dataset.displayStyle || window.displayStyle || 'Gauge');

        if (desiredStyle === 'Graph6hr' || desiredStyle === 'Graph24hr') {
          await showMicrographForContainer(container, { force, ignoreModal });
        }
      } catch (e) {
        console.warn('[micrograph] refresh error', e);
      }
    }
  }

  window.refreshAllMicrographs = refreshAllMicrographs;

  if (window.__needsInitialMicrographRefresh) {
    window.__needsInitialMicrographRefresh = false;
    setTimeout(() => refreshAllMicrographs(true), 250);
  }

  document.addEventListener('DOMContentLoaded', function() {
    try {
      const hasGraphContainer = Array.from(document.querySelectorAll('.metric-container')).some((container) => {
        const style = (typeof window.getContainerStyle === 'function') ? window.getContainerStyle(container) : 'Gauge';
        return style === 'Graph6hr' || style === 'Graph24hr';
      });
      if (hasGraphContainer) {
        setTimeout(() => refreshAllMicrographs(true), 500);
      }
    } catch (e) {
      console.warn('[micrograph] DOMContentLoaded init error', e);
    }
  });

  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'visible') {
      setTimeout(() => updateGauges({ ignoreVisibility: true, ignoreModal: true }), 50);
      setTimeout(() => refreshAllMicrographs(true, { ignoreModal: true }), 800);
    }
  });
  window.addEventListener('focus', function() {
    setTimeout(() => updateGauges({ ignoreVisibility: true, ignoreModal: true }), 50);
    setTimeout(() => refreshAllMicrographs(true, { ignoreModal: true }), 800);
  });
})();
function updateContainerDisplayStyle(container) {
  if (!container) return;

  const sensorId = (container.dataset.sensor || '').trim();
  const metric   = (container.dataset.metric || '').trim();
  if (!sensorId || !metric) return;

  const safeMetric = (typeof toSafe === 'function') ? toSafe(metric) : metric.replace(/[^a-zA-Z0-9_\-]/g,'_');
  const safeBase   = `${sensorId}_${safeMetric}`;

  let graph = container.querySelector('.graph-container');
  if (!graph) {
    const gauge = container.querySelector('.gauge-container');
    if (!gauge) return;
    graph = document.createElement('div');
    graph.className = 'graph-container';
    graph.style.display = 'none';
    graph.innerHTML =
      `<div class='graph-view' id='${safeBase}GraphContainer'>` +
        `<canvas class='micrograph-canvas' id='${safeBase}Micrograph' width='220' height='60'></canvas>` +
      `</div>`;
    gauge.insertAdjacentElement('afterend', graph);
  }
}

window.ensureContainerDisplayStyle = function(container) {
  if (!container) return;
  const raw = String(container.dataset.displayStyle || window.displayStyle || 'Gauge').toLowerCase();
  if (raw === 'graph' || raw === 'graph6hr' || raw === 'graph24hr') {
    updateContainerDisplayStyle(container);
    if (typeof window.registerContainerStyle === 'function') {
      const mapped = (raw === 'graph6hr') ? 'Graph6hr' : (raw === 'graph24hr' ? 'Graph24hr' : 'Graph24hr');
      window.registerContainerStyle(container, mapped);
    }
  }
};

document.addEventListener('DOMContentLoaded', function() {
  try {
    const all = document.querySelectorAll('.metric-container');
    for (const c of all) {
      window.ensureContainerDisplayStyle(c);
    }
  } catch (e) {
    console.warn('ensureContainerDisplayStyle DOMContentLoaded error', e);
  }
});
window.ensureButtonTooltips = window.ensureButtonTooltips || function(root){
  const scope = root && root.querySelectorAll ? root : document;
  const nodes = scope.querySelectorAll('button, a.button, [role="button"]');
  function inferTitle(el){
    const aria = String(el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const paneTarget = String(el.getAttribute('data-target') || el.getAttribute('data-pane-target') || '').trim();
    const text = String(el.textContent || '').replace(/\s+/g, ' ').trim();
    const id = String(el.id || '').trim();
    if (paneTarget && text) return `Open ${text}`;
    if (id === 'bioPrevMonthBtn') return 'Previous month';
    if (id === 'bioNextMonthBtn') return 'Next month';
    if (id && /toggle/i.test(id)) return text ? `${text} hidden value` : 'Toggle hidden value';
    if (text && /^(?:&times;|×)$/u.test(text)) return 'Close';
    if (text && /^(?:←|↖|<)$/u.test(text)) return 'Previous';
    if (text && /^(?:→|↘|>)$/u.test(text)) return 'Next';
    return text;
  }
  nodes.forEach(function(el){
    if (el.hasAttribute('title')) return;
    const title = inferTitle(el);
    if (title) el.setAttribute('title', title);
  });
};

window.ModalBusyCursor = window.ModalBusyCursor || (function(){
  let depth = 0;
  let startedAt = 0;
  function apply(){
    document.documentElement.classList.toggle('modal-busy-cursor', depth > 0);
    document.body.classList.toggle('modal-busy-cursor', depth > 0);
  }
  function begin(){
    if (depth <= 0) startedAt = Date.now();
    depth += 1;
    apply();
  }
  function end(){
    depth = Math.max(0, depth - 1);
    if (depth === 0) startedAt = 0;
    apply();
  }
  function isBusy(){
    if (depth > 0 && startedAt && (Date.now() - startedAt) > 30000) {
      depth = 0;
      startedAt = 0;
      apply();
      return false;
    }
    return depth > 0;
  }
  async function untilPaint(){
    await new Promise(resolve => requestAnimationFrame(() => resolve()));
    await new Promise(resolve => requestAnimationFrame(() => resolve()));
  }
  return { begin, end, isBusy, untilPaint };
})();

window.editSensorSettings = async function(id) {
  window.ModalBusyCursor.begin();
  try {
    const url = `/edit-sensor?sensor_id=${encodeURIComponent(id)}&embed=1&t=${Date.now()}`;
    const res = await fetch(url, { cache: 'no-store' });
    const html = await res.text();
    if (!res.ok) {
      console.error('[editSensorSettings] non-OK', res.status, html.slice(0,200));
      alert('Failed to load Sensor Settings');
      return;
    }
    if (!window.BackdropModal) {
      console.error('BackdropModal is not defined');
      return;
    }
    if (!html || !html.trim()) {
      console.error('[editSensorSettings] empty HTML');
      return;
    }
    window.BackdropModal.close('sensorSettingsModal');
    const modal = window.BackdropModal.openFromHtml(html, 'sensorSettingsModal');
    if (modal) {
      modal.dataset.sensorId = id;
      if (window.initSensorSettingsModal) window.initSensorSettingsModal(modal);
      const hasCalibrationPane = !!modal.querySelector('#sensor-calibration-pane, #system-calibration-pane');
      if (hasCalibrationPane) {
        const TAG_ID = 'system-calibration-js';
        let needLoadSystemCalJs = true;
        if (window.initSystemCalibrationModal) needLoadSystemCalJs = false;
        if (needLoadSystemCalJs) {
          const existing = document.getElementById(TAG_ID);
          if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
          await new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.id = TAG_ID;
            s.src = '/ui_static/js/system_calibration.js?v=' + Date.now();
            s.onload = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
          });
        }
        if (window.initSystemCalibrationModal) await window.initSystemCalibrationModal(modal);
      }
      window.ensureButtonTooltips(modal.closest('.modal-backdrop') || modal);
      await window.ModalBusyCursor.untilPaint();
    }
  } catch (e) {
    console.error('Failed to load sensor modal', e);
    if (typeof window.showToast === 'function') window.showToast('Failed to load Sensor Settings', 'error');
    else alert('Failed to load Sensor Settings');
  } finally {
    window.ModalBusyCursor.end();
  }
};
window.editSystemSettings = async function() {
  window.ModalBusyCursor.begin();
  try {
    const host = document.querySelector('#modal-host') || document.body;
    const old = document.getElementById('system-settings-root');
    if (old) {
      try {
        const stateUrl = `/edit-system?embed=1&t=${Date.now()}`;
        const stateRes = await fetch(stateUrl, { cache: 'no-store' });
        if (stateRes.ok) {
          const stateDoc = new DOMParser().parseFromString(await stateRes.text(), 'text/html');
          const savedTheme = stateDoc.querySelector('input[name="weather_forecast_theme"]:checked')?.value || '';
          const currentTheme = Array.from(old.querySelectorAll('input[name="weather_forecast_theme"]')).find(input => input.value === savedTheme);
          if (currentTheme) currentTheme.checked = true;
        }
      } catch (stateErr) {
        console.warn('Failed to refresh General Settings state', stateErr);
      }
      if (typeof window.openSetupModal === 'function') {
        window.openSetupModal();
      } else {
        const modal = document.getElementById('setupPiModal');
        if (modal) modal.style.display = 'block';
      }
      await window.ModalBusyCursor.untilPaint();
      return;
    }

    const url = `/edit-system?embed=1&t=${Date.now()}`;
    const res = await fetch(url, { cache: 'no-store' });
    const html = await res.text();
    if (!res.ok) {
      console.error('[editSystemSettings] non-OK', res.status, html.slice(0,200));
      alert('Failed to load General Settings');
      return;
    }
    if (!html || !html.trim()) {
      console.error('[editSystemSettings] empty HTML');
      return;
    }

    const root = document.createElement('div');
    root.id = 'system-settings-root';
    root.innerHTML = html;
    host.appendChild(root);

    const scripts = root.querySelectorAll('script');
    scripts.forEach(oldScript => {
      const s = document.createElement('script');
      for (const a of oldScript.attributes) s.setAttribute(a.name, a.value);
      s.textContent = oldScript.textContent || '';
      oldScript.parentNode.replaceChild(s, oldScript);
    });
    window.ensureButtonTooltips(root);

    if (typeof window.openSetupModal === 'function') {
      window.openSetupModal();
    } else {
      const modal = document.getElementById('setupPiModal');
      if (modal) modal.style.display = 'block';
    }
    await window.ModalBusyCursor.untilPaint();
  } catch (e) {
    console.error('Failed to load system modal', e);
  } finally {
    window.ModalBusyCursor.end();
  }
};

window.closeSystemSettingsModal = function(){
  window.setMobileWorkspaceViewport?.(document.getElementById('setupPiModal'), false);
  const ids = [
    'setupPiModal',
    'ha-settings-overlay',
    'device-locations-overlay',
    'remove-device-overlay',
    'onboard-progress-overlay'
  ];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
};
function _safeName(name) { return (name || '').toLowerCase().replaceAll(' ', '_'); }
function _realName(s) {
  if (typeof s !== 'string') return '';
  return s.includes(' ') ? s : s.replaceAll('_',' ');
}
function _cssEsc(s){try{return CSS&&CSS.escape?CSS.escape(String(s)):String(s);}catch(_){return String(s);}}
function _splitKey(key){
  const k=String(key||'');
  if(k.includes('::')){const [sid,...rest]=k.split('::');return {switchId:sid,label:rest.join('::'),channel:rest.join('::')};}
  if(k.includes(':')){const [sid,...rest]=k.split(':');return {switchId:sid,label:rest.join(':'),channel:rest.join(':')};}
  return {switchId:'',label:k,channel:k};
}
function _stripIsoExtras(ts){
  const s = String(ts||'');
  const noMicros = s.replace(/\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$)/,'');
  const noTz = noMicros.replace(/(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:Z|[+-]\d{2}:\d{2})\b/, '$1');
  return noTz.replace('T',' ');
}
const _switchEventsCache = new Map();
const _switchRefreshBlockUntil = new Map();
const _switchTimerState = new Map();
function _timerStateKey(key, fallbackName){
  const k = String(key || '').trim();
  if (k) return k;
  return String(fallbackName || '').trim();
}
function _findTimerPanel(key){
  const k = String(key || '').trim();
  if (k) {
    const exact = document.querySelector(`.switch-timer-panel[data-switch-ui-key="${_cssEsc(k)}"]`);
    if (exact) return exact;
  }
  return null;
}
function _setTimerEditorOpen(panel, open){
  if (!panel) return;
  const editor = panel.querySelector('.switch-timer-editor');
  const btn = panel.querySelector('.switch-timer-edit-btn');
  const container = panel.closest('.switch-metric-container');
  if (editor) editor.style.display = open ? 'flex' : 'none';
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  panel.classList.toggle('timer-editor-open', !!open);
  if (container) {
    const anyOpen = !!container.querySelector('.switch-timer-panel.timer-editor-open');
    container.classList.toggle('timer-editor-open', anyOpen);
  }
}
function _closeAllTimerEditors(){
  document.querySelectorAll('.switch-timer-panel').forEach((panel) => _setTimerEditorOpen(panel, false));
}
function _renderSwitchTimer(key, fallbackName){
  const stateKey = _timerStateKey(key, fallbackName);
  const panel = _findTimerPanel(stateKey);
  if (!panel) return;
  const input = panel.querySelector('.switch-timer-input');
  const status = panel.querySelector('.switch-timer-status');
  const model = _switchTimerState.get(stateKey) || {};
  const seconds = Number(model.timer_seconds || 0);
  const deadline = Number(model.timer_deadline_epoch || 0);
  const isOn = !!model.state;
  if (input && document.activeElement !== input) {
    input.value = String(seconds);
    input.dataset.lastGoodValue = String(seconds);
  }
  if (!status) return;
  if (seconds <= 0) {
    status.textContent = 'Timer disabled';
    return;
  }
  if (!isOn || !deadline) {
    status.textContent = `Timer set: ${seconds}s`;
    return;
  }
  const remaining = Math.max(0, Math.ceil(deadline - (Date.now() / 1000)));
  status.textContent = remaining > 0 ? `Countdown: ${remaining}s` : 'Turning off...';
}
function updateSwitchTimerUi(key, stateData, fallbackName){
  const stateKey = _timerStateKey((stateData && stateData.ui_key) || key, fallbackName);
  const prev = _switchTimerState.get(stateKey) || {};
  const next = Object.assign({}, prev);
  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'state')) next.state = !!stateData.state;
  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_seconds')) next.timer_seconds = Number(stateData.timer_seconds || 0);
  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_deadline_epoch')) next.timer_deadline_epoch = stateData.timer_deadline_epoch ? Number(stateData.timer_deadline_epoch) : 0;
  if (stateData && Object.prototype.hasOwnProperty.call(stateData, 'timer_remaining_s') && !Object.prototype.hasOwnProperty.call(stateData, 'timer_deadline_epoch')) {
    const rem = Number(stateData.timer_remaining_s || 0);
    next.timer_deadline_epoch = rem > 0 ? ((Date.now() / 1000) + rem) : 0;
  }
  if (!next.state) next.timer_deadline_epoch = 0;
  _switchTimerState.set(stateKey, next);
  _renderSwitchTimer(stateKey, fallbackName);
}
function tickSwitchCountdowns(){
  for (const key of _switchTimerState.keys()) {
    _renderSwitchTimer(key, '');
  }
}
function initSwitchTimersFromDom(){
  document.querySelectorAll('.switch-timer-panel').forEach((panel) => {
    const key = panel.dataset.switchUiKey || '';
    const input = panel.querySelector('.switch-timer-input');
    const initial = Number((input && input.value) || 0);
    _switchTimerState.set(key, { timer_seconds: initial, timer_deadline_epoch: 0, timer_remaining_s: 0, state: false });
    if (input) input.dataset.lastGoodValue = String(initial);
    _setTimerEditorOpen(panel, false);
    _renderSwitchTimer(key, panel.dataset.label || '');
  });
}
function _findEventsListElem(key){
  const {switchId,label}=_splitKey(key);
  const norm=(s)=>String(s||'').trim().toLowerCase().replaceAll('_',' ').replace(/\s+/g,' ').replace(/s$/,'');
  let el=document.querySelector(`ul.switch-events-list[data-switch-key="${_cssEsc(key)}"]`);
  if(el) return el;
  const id1=_safeName((switchId?switchId+"_":"")+label)+"_events_list";
  el=document.getElementById(id1);
  if(el && el.tagName==='UL') return el;
  const id2=_safeName(label)+"_events_list";
  el=document.getElementById(id2);
  if(el && el.tagName==='UL') return el;
  if(el && el.tagName!=='UL'){
    const row=el.closest('tr');
    const ul=row?row.querySelector('ul.switch-events-list'):null;
    if(ul) return ul;
  }
  const want=norm(label);
  const matches=[];
  for(const ul of document.querySelectorAll('ul.switch-events-list')){
    const k=ul.getAttribute('data-switch-key')||'';
    const {label:lab2}=_splitKey(k);
    if(norm(lab2)===want) matches.push(ul);
  }
  if(matches.length===1) return matches[0];
  return null;
}
window.showSwitchSettingsModal = function(html){
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const modal = wrapper.querySelector('#switchSettingsModal') || wrapper.firstElementChild;
  if (!modal) return;
  const existing = document.getElementById('switchSettingsModal');
  if (existing) existing.remove();
  document.body.appendChild(modal);
  if (window.SwitchModal && typeof window.SwitchModal.mount === 'function') {
    window.SwitchModal.mount(modal);
  }
  window.SwitchModal && window.SwitchModal.open();
};
if (typeof window.getToastContainer !== 'function') {
  window.getToastContainer = function(context){
    function visible(el){
      if (!el || !document.body.contains(el)) return false;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }
    const paneSelectors = ['.system-settings-content', '.switch-dialog-content', '.modal-body .split > section.panel'];
    function paneFrom(root){
      if (!root || !document.body.contains(root)) return null;
      const selectors = paneSelectors;
      for (const selector of selectors) {
        if (root.matches && root.matches(selector) && visible(root)) return root;
        const pane = root.querySelector(selector);
        if (visible(pane)) return pane;
      }
      return null;
    }
    function contextRoot(){
      if (!context) return null;
      const el = context.nodeType === 1 ? context : context.parentElement;
      if (!el || !document.body.contains(el)) return null;
      return el.closest('.modal-backdrop') || el.closest('.system-settings-shell, .modal') || el;
    }
    function activePane(){
      const contextual = paneFrom(contextRoot());
      if (contextual) return contextual;
      const roots = Array.from(document.querySelectorAll('.modal-backdrop, .system-settings-shell, .modal')).filter(visible);
      for (let i = roots.length - 1; i >= 0; i -= 1) {
        const pane = paneFrom(roots[i]);
        if (pane) return pane;
      }
      const panes = Array.from(document.querySelectorAll(paneSelectors.join(','))).filter(visible);
      if (panes.length) return panes[panes.length - 1];
      return null;
    }
    const pane = activePane();
    if (pane) {
      let c = Array.from(pane.children).find((el) => el.classList && el.classList.contains('toast-container') && el.classList.contains('modal-pane-toast-container'));
      if (!c) { c = document.createElement('div'); c.className = 'toast-container modal-pane-toast-container'; pane.appendChild(c); }
      return c;
    }
    let c = document.querySelector('body > .toast-container:not(.modal-pane-toast-container)');
    if (!c) { c = document.createElement('div'); c.className = 'toast-container'; document.body.appendChild(c); }
    return c;
  };
}
if (typeof window.showToast !== 'function') {
  window.showToast = function(text, type, context){
    const c = window.getToastContainer ? window.getToastContainer(context) : null;
    if (!c) return;
    const t = document.createElement('div'); t.className = `toast ${type||''}`; t.textContent = text||'';
    c.appendChild(t); setTimeout(()=>{ t.remove(); if(!c.children.length) c.remove(); }, 2500);
  };
}
window.updateSensorOrderButtons = function(){
  const groups = Array.from(document.querySelectorAll('.dashboard-content > .sensor-group[data-dashboard-row-id]'));
  groups.forEach((group, index) => {
    group.querySelectorAll('.sensor-order-item').forEach((button) => {
      button.disabled = !!window.__sensorOrderMoving || (button.dataset.move === 'up' ? index === 0 : index === groups.length - 1);
    });
  });
};
window.applySensorGroupOrder = function(order){
  if (!Array.isArray(order) || !order.length) return;
  const firstSensorGroup = document.querySelector('.sensor-group[data-dashboard-row-id]');
  const dashboardContent = (firstSensorGroup && firstSensorGroup.parentElement) || document.querySelector('.dashboard-content');
  if (!dashboardContent) return;
  const groups = new Map();
  dashboardContent.querySelectorAll('.sensor-group[data-dashboard-row-id]').forEach((el) => {
    groups.set(el.getAttribute('data-dashboard-row-id') || '', el);
  });
  const completeOrder = [...order, ...Array.from(groups.keys()).filter((id) => !order.includes(id))];
  const visibleOrder = completeOrder.filter((id) => groups.has(id));
  const currentOrder = Array.from(groups.keys());
  if (visibleOrder.every((id, index) => id === currentOrder[index])) return;
  visibleOrder.forEach((sid) => {
    const el = groups.get(String(sid || ''));
    if (el) dashboardContent.appendChild(el);
  });
};
window.applySensorGroupOrder(config.dashboardRowOrder);
window.updateSensorOrderButtons();
window.reorderSensorGroup = async function(sensorId, direction){
  const sid = String(sensorId || '').trim();
  const move = String(direction || '').trim().toLowerCase();
  if (!sid || !['up','down'].includes(move) || window.__sensorOrderMoving) return;
  window.__sensorOrderMoving = true;
  window.updateSensorOrderButtons();
  const group = document.querySelector(`.sensor-group[data-dashboard-row-id="${CSS.escape(sid)}"]`);
  if (group) group.classList.add('moving');
  try {
    const res = await fetch('/dashboard/metric-position', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sensor_id: sid, direction: move, include_switches: !!document.getElementById('group_switches') })
    });
    const js = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(String(js.error || js.detail || ('HTTP ' + res.status)));
    if (Array.isArray(js.order)) window.applySensorGroupOrder(js.order);
    if (js.moved) {
      const select = document.getElementById('sensor_row_order');
      if (select) select.value = 'existing';
      if (group && !document.querySelector('.dashboard-order-section[open]')) group.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } else if (typeof window.showToast === 'function') {
      window.showToast(move === 'up' ? 'Row is already at the top' : 'Row is already at the bottom');
    }
    return js;
  } catch (e) {
    console.error('Failed to reorder sensor group', e);
    if (typeof window.showToast === 'function') window.showToast('Failed to save dashboard order', 'error');
  } finally {
    if (group) group.classList.remove('moving');
    window.__sensorOrderMoving = false;
    window.updateSensorOrderButtons();
  }
};
if (!window.__sensorOrderInit) {
  window.__sensorOrderInit = true;
  document.addEventListener('click', function(ev){
    const moveBtn = ev.target.closest('.sensor-order-item');
    if (moveBtn && !moveBtn.disabled) {
      ev.preventDefault();
      ev.stopPropagation();
      window.reorderSensorGroup(moveBtn.getAttribute('data-row-id') || moveBtn.getAttribute('data-sensor-id'), moveBtn.getAttribute('data-move'));
      return;
    }
  });
}
window.initSwitchSettingsModal = function(modalEl){
  const modal = modalEl || document.getElementById('switchSettingsModal');
  if (!modal) return;
  const btnSettings = modal.querySelector('#switchMenuSettings');
  window.initMobileSettings?.(modal);
  const btnStats = modal.querySelector('#switchMenuStatistics');
  const btnBacks = modal.querySelectorAll('[data-switch-pane-target="settings"]');
  const paneSettings = modal.querySelector('#switchSettingsPane');
  const paneStats = modal.querySelector('#switchStatisticsPane');
  const form = modal.querySelector('form[action="/submit-switch-settings"]');
  const saveBtn = modal.querySelector('#switchSettingsSaveBtn');
  const statusEl = modal.querySelector('#switchSettingsStatus');
  const restartBtn = modal.querySelector('#switchRestartBtn');
  const locationInput = form ? form.querySelector('input[name="location"]') : null;
  const initialLocation = locationInput ? String(locationInput.value || '').trim() : '';
  if (!btnSettings || !btnStats || !paneSettings || !paneStats) return;

  function formatStatDuration(seconds){
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'No offline events';
    let total = Math.max(0, Math.floor(seconds));
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const secs = total - minutes * 60;
    const parts = [];
    if (days) parts.push(days + 'd');
    if (hours || parts.length) parts.push(hours + 'h');
    if (minutes || parts.length) parts.push(minutes + 'm');
    parts.push(secs + 's');
    return parts.join(' ');
  }
  function numberFromDataset(panel, name){
    const raw = Number(panel.dataset[name] || 0);
    return Number.isFinite(raw) ? raw : 0;
  }
  function formatStatAgeFromEpoch(epoch, emptyText){
    const raw = Number(epoch || 0);
    if (!Number.isFinite(raw) || raw <= 0) return emptyText;
    return formatStatDuration((Date.now() / 1000) - raw) + ' ago';
  }
  function statStorageKey(panel){
    const kind = String(panel.dataset.statPanel || 'device');
    const id = String(panel.dataset.statId || '').trim() || 'unknown';
    return 'sensorius.stats.' + kind + '.' + id;
  }
  function readStatReset(panel){
    try {
      const raw = window.localStorage ? window.localStorage.getItem(statStorageKey(panel)) : '';
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_err) {
      return {};
    }
  }
  function writeStatReset(panel, payload){
    try {
      if (window.localStorage) window.localStorage.setItem(statStorageKey(panel), JSON.stringify(payload));
    } catch (_err) {}
  }
  function rebuildChannelRows(panel, rows){
    const list = panel ? panel.querySelector('[data-stat-channel-list]') : null;
    if (!list || !Array.isArray(rows)) return;
    list.innerHTML = '';
    rows.forEach(function(row){
      const wrap = document.createElement('div');
      wrap.className = 'statistics-row';
      wrap.setAttribute('data-stat-channel-row', '');
      wrap.dataset.channelId = String(row.channel_id || '');
      wrap.dataset.state = String(row.state || '');
      wrap.dataset.stateEpoch = row.state_epoch == null ? '' : String(row.state_epoch);
      const label = document.createElement('span');
      label.textContent = String(row.row_label || 'Switch current state, age:');
      const value = document.createElement('strong');
      value.setAttribute('data-stat-channel-value', '');
      value.textContent = String(row.state_age_label || 'No state changes');
      wrap.appendChild(label);
      wrap.appendChild(value);
      list.appendChild(wrap);
    });
  }
  function renderStatsPanel(panel){
    if (!panel) return;
    const reset = readStatReset(panel);
    const rawOffline = numberFromDataset(panel, 'offlineEvents24h');
    const resetEpoch = Number(reset.resetEpoch || 0);
    const offlineBase = resetEpoch > 0 ? Number(reset.offlineEventsBase || 0) : 0;
    const offlineEl = panel.querySelector('[data-stat-value="offline-events"]');
    const uptimeEl = panel.querySelector('[data-stat-value="uptime"]');
    const lastPacketEl = panel.querySelector('[data-stat-value="last-packet"]');
    if (offlineEl) offlineEl.textContent = String(Math.max(0, rawOffline - offlineBase));
    if (uptimeEl) {
      const lastOfflineEpoch = Number(panel.dataset.lastOfflineEpoch || 0);
      const baseEpoch = Math.max(lastOfflineEpoch > 0 ? lastOfflineEpoch : 0, resetEpoch > 0 ? resetEpoch : 0);
      uptimeEl.textContent = baseEpoch > 0 ? formatStatDuration((Date.now() / 1000) - baseEpoch) : 'No offline events';
    }
    if (lastPacketEl) {
      lastPacketEl.textContent = formatStatAgeFromEpoch(panel.dataset.lastPacketEpoch, 'No packets');
    }
    panel.querySelectorAll('[data-stat-channel-row]').forEach(function(row){
      const value = row.querySelector('[data-stat-channel-value]');
      const stateEpoch = Number(row.dataset.stateEpoch || 0);
      const state = String(row.dataset.state || '').trim();
      if (value) value.textContent = stateEpoch > 0 ? ((state || 'Unknown') + ', ' + formatStatDuration((Date.now() / 1000) - stateEpoch)) : 'No state changes';
    });
  }
  function applyStatsPayload(panel, data){
    if (!panel || !data || data.ok === false) return;
    if (Object.prototype.hasOwnProperty.call(data, 'offline_events_24h')) panel.dataset.offlineEvents24h = String(data.offline_events_24h || 0);
    if (Object.prototype.hasOwnProperty.call(data, 'last_offline_epoch')) panel.dataset.lastOfflineEpoch = data.last_offline_epoch == null ? '' : String(data.last_offline_epoch);
    if (Object.prototype.hasOwnProperty.call(data, 'switch_last_packet_epoch')) panel.dataset.lastPacketEpoch = data.switch_last_packet_epoch == null ? '' : String(data.switch_last_packet_epoch);
    const lastOfflineEl = panel.querySelector('[data-stat-value="last-offline"]');
    if (lastOfflineEl && data.last_offline_event_label) lastOfflineEl.textContent = String(data.last_offline_event_label);
    rebuildChannelRows(panel, data.switch_channel_state_stats);
    renderStatsPanel(panel);
  }
  async function refreshStatsPanel(panel){
    if (!panel || !panel.dataset.statsUrl) return;
    try {
      const resp = await fetch(panel.dataset.statsUrl, {
        cache: 'no-store',
        headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await resp.json().catch(function(){ return {}; });
      if (!resp.ok || data.ok === false) return;
      applyStatsPayload(panel, data);
    } catch (_err) {}
  }
  function isStatsPanelVisible(panel){
    return !!panel && window.getComputedStyle(panel).display !== 'none' && !panel.hidden;
  }
  function initStatsPanels(){
    const panels = modal.querySelectorAll('[data-stat-panel]');
    panels.forEach(function(panel){ renderStatsPanel(panel); });
    if (!modal.dataset.statsResetBound) {
      modal.dataset.statsResetBound = '1';
      modal.querySelectorAll('[data-stat-reset]').forEach(function(btn){
        btn.addEventListener('click', function(){
          const panel = btn.closest('[data-stat-panel]');
          if (!panel) return;
          writeStatReset(panel, {
            resetEpoch: Date.now() / 1000,
            offlineEventsBase: numberFromDataset(panel, 'offlineEvents24h')
          });
          renderStatsPanel(panel);
        });
      });
    }
    if (!modal.__statsTimer) {
      modal.__statsTimer = window.setInterval(function(){
        if (!document.body.contains(modal)) {
          window.clearInterval(modal.__statsTimer);
          modal.__statsTimer = null;
          return;
        }
        panels.forEach(function(panel){ renderStatsPanel(panel); });
      }, 1000);
    }
    if (!modal.__statsRefreshTimer) {
      modal.__statsRefreshTimer = window.setInterval(function(){
        if (!document.body.contains(modal)) {
          window.clearInterval(modal.__statsRefreshTimer);
          modal.__statsRefreshTimer = null;
          return;
        }
        panels.forEach(function(panel){ if (isStatsPanelVisible(panel)) refreshStatsPanel(panel); });
      }, 5000);
    }
  }
  initStatsPanels();

  function activate(which){
    const showSettings = (which === 'settings');
    const showStats = (which === 'statistics');
    paneSettings.hidden = !showSettings;
    paneStats.hidden = !showStats;
    btnSettings.classList.toggle('active', showSettings);
    btnStats.classList.toggle('active', showStats);
    btnSettings.setAttribute('aria-selected', showSettings ? 'true' : 'false');
    btnStats.setAttribute('aria-selected', showStats ? 'true' : 'false');
  }

  btnSettings.onclick = function(){
    activate('settings');
  };

  btnBacks.forEach(function(btn){
    btn.onclick = function(){
      activate('settings');
    };
  });

  btnStats.onclick = function(){
    activate('statistics');
    initStatsPanels();
    refreshStatsPanel(paneStats);
  };

  if (form && saveBtn && statusEl && form.dataset.ajaxBound !== '1') {
    form.dataset.ajaxBound = '1';
    function setBusy(isBusy){
      saveBtn.disabled = !!isBusy;
      if (restartBtn && restartBtn.dataset.pending !== '1') restartBtn.disabled = !!isBusy;
    }
    function setStatus(text){
      statusEl.textContent = text || '';
    }
    function setRestartPending(isPending){
      if (!restartBtn) return;
      if (!restartBtn.dataset.baseLabel) restartBtn.dataset.baseLabel = (restartBtn.textContent || 'Restart Device').trim();
      restartBtn.dataset.pending = isPending ? '1' : '0';
      restartBtn.disabled = !!isPending;
      restartBtn.textContent = isPending ? 'Device Restarting...' : (restartBtn.dataset.baseLabel || 'Restart Device');
    }
    async function parseResponseError(resp){
      const contentType = String(resp.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('application/json')) {
        const js = await resp.json().catch(() => ({}));
        return String(js.error || js.message || ('HTTP ' + resp.status));
      }
      return String((await resp.text().catch(() => '')) || ('HTTP ' + resp.status)).trim();
    }
    form.addEventListener('submit', async function(ev){
      ev.preventDefault();
      setBusy(true);
      setStatus('Saving...');
      try {
        const body = new URLSearchParams(new FormData(form));
        const resp = await fetch(form.action, {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          },
          body: body.toString(),
        });
        const js = await resp.json().catch(() => ({}));
        if (!resp.ok || js.ok === false) throw new Error(String(js.error || js.message || ('HTTP ' + resp.status)));
        setStatus('Saved.');
        if (typeof window.showToast === 'function') window.showToast('Switch settings saved', 'ok');
        const savedLocation = locationInput ? String(locationInput.value || '').trim() : '';
        if (savedLocation !== initialLocation) setTimeout(() => window.location.reload(), 400);
      } catch (err) {
        const msg = err && err.message ? err.message : 'Failed to save switch settings.';
        setStatus(msg);
        if (typeof window.showToast === 'function') window.showToast(msg, 'error');
      } finally {
        setBusy(false);
      }
    });
    if (restartBtn) {
      setRestartPending(false);
      restartBtn.addEventListener('click', async function(){
        const switchIdInput = form.querySelector('input[name="switch_id"]');
        const switchId = (switchIdInput && switchIdInput.value) ? String(switchIdInput.value) : String(modal.dataset.switchId || '');
        if (!switchId) {
          const msg = 'Missing switch id.';
          setStatus(msg);
          if (typeof window.showToast === 'function') window.showToast(msg, 'error');
          return;
        }
        if (!window.confirm('Restart this device now? Unsaved changes in this modal will not be applied.')) return;
        setBusy(true);
        setRestartPending(true);
        setStatus('Device Restarting...');
        try {
          const body = new URLSearchParams();
          body.set('switch_id', switchId);
          const resp = await fetch(restartBtn.dataset.restartUrl || '/switch-settings/restart-device', {
            method: 'POST',
            headers: {
              'Accept': 'application/json',
              'X-Requested-With': 'XMLHttpRequest',
              'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
            },
            body: body.toString(),
          });
          if (!resp.ok) throw new Error(await parseResponseError(resp));
          const js = await resp.json().catch(() => ({}));
          const message = String(js.message || 'Device restarting...');
          setStatus(message);
          if (typeof window.showToast === 'function') window.showToast(message, 'ok');
        } catch (err) {
          const msg = err && err.message ? err.message : 'Failed to restart device.';
          setRestartPending(false);
          setStatus(msg);
          if (typeof window.showToast === 'function') window.showToast(msg, 'error');
        } finally {
          setBusy(false);
        }
      });
    }
  }

  activate('settings');
};
window.editSwitchSettings = async function(id) {
  window.ModalBusyCursor.begin();
  try {
    const url = `/edit-switch?switch_id=${encodeURIComponent(id)}&embed=1&t=${Date.now()}`;
    const res = await fetch(url, { cache: 'no-store' });
    const html = await res.text();
    if (!res.ok) {
      console.error('[editSwitchSettings] non-OK', res.status, html.slice(0,200));
      alert('Failed to load Switch Settings');
      return;
    }
    if (!window.BackdropModal) {
      console.error('BackdropModal is not defined');
      return;
    }
    if (!html || !html.trim()) {
      console.error('[editSwitchSettings] empty HTML');
      return;
    }
    window.BackdropModal.close('switchSettingsModal');
    const modal = window.BackdropModal.openFromHtml(html, 'switchSettingsModal');
    if (modal) {
      modal.dataset.switchId = id;
      modal.dataset.automationInit = '0';
      if (typeof window.initSwitchSettingsModal === 'function') window.initSwitchSettingsModal(modal);
      window.ensureButtonTooltips(modal.closest('.modal-backdrop') || modal);
      await window.ModalBusyCursor.untilPaint();
    }
  } catch (e) {
    console.error('Failed to load switch modal', e);
  } finally {
    window.ModalBusyCursor.end();
  }
};
window.closeSwitchSettingsModal = function() {
  const modal = document.getElementById('switchSettingsModal');
  if (!modal) return;
  const backdrop = modal.closest('.modal-backdrop');
  if (backdrop && backdrop.parentNode) {
    backdrop.parentNode.removeChild(backdrop);
  } else if (modal.parentNode) {
    modal.parentNode.removeChild(modal);
  }
};
window.BackdropModal = window.BackdropModal || (function(){
  function ensureHost(){
    let host = document.querySelector('#modal-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'modal-host';
      document.body.appendChild(host);
    }
    return host;
  }
  function close(modalId){
    if (!modalId) return;
    const modal = document.getElementById(modalId);
    if (!modal) return;
    window.setMobileWorkspaceViewport?.(modal, false);
    const backdrop = modal.closest('.modal-backdrop');
    if (backdrop && backdrop.parentNode) {
      backdrop.parentNode.removeChild(backdrop);
    } else if (modal.parentNode) {
      modal.parentNode.removeChild(modal);
    }
  }
  function openFromHtml(html, modalId){
    if (!html || !html.trim()) {
      console.error('BackdropModal.openFromHtml: empty HTML');
      return null;
    }
    const tmp = document.createElement('div');
    tmp.innerHTML = html.trim();
    const backdrop = tmp.querySelector('.modal-backdrop');
    const modal = tmp.querySelector('#' + modalId);
    if (!modal) {
      console.error('BackdropModal.openFromHtml: modalId not found in HTML', modalId);
      return null;
    }
    const mount = ensureHost();
    if (backdrop) {
      mount.appendChild(backdrop);
      backdrop.style.display = 'flex';
      if (window.ensureButtonTooltips) window.ensureButtonTooltips(backdrop);
    } else {
      mount.appendChild(modal);
      modal.style.display = 'block';
      if (window.ensureButtonTooltips) window.ensureButtonTooltips(modal);
    }
    return modal;
  }
  return { openFromHtml, close };
})();
document.addEventListener('keydown', function(ev){
  if (ev.key !== 'Escape') return;
  const backdrops = document.querySelectorAll('.modal-backdrop');
  if (!backdrops.length) return;
  const last = backdrops[backdrops.length - 1];
  const modal = last.querySelector('.modal');
  const id = modal ? modal.id : null;
  if (id && window.BackdropModal) window.BackdropModal.close(id);
});
document.addEventListener('click', function(ev){
  const backdrop = ev.target.closest('.modal-backdrop');
  if (!backdrop) return;
  if (ev.target !== backdrop) return;
  const modal = backdrop.querySelector('.modal');
  const id = modal ? modal.id : null;
  if (id && window.BackdropModal) window.BackdropModal.close(id);
});
window.closeSwitchSettingsModal = function(){
  if (window.BackdropModal) window.BackdropModal.close('switchSettingsModal');
};
window.closeSensorSettingsModal = function(){
  if (window.BackdropModal) window.BackdropModal.close('sensorSettingsModal');
};
window.closeSystemCalibrationModal = function(){
  if (window.BackdropModal) window.BackdropModal.close('systemCalibrationModal');
};
window.openSystemCalibrationModal = async function(sensorId) {
  try {
    const params = new URLSearchParams();
    const sid = (sensorId && String(sensorId).trim()) || '';
    if (sid) params.set('sensor_id', sid);
    const url = `/ui/modal/system-calibration?${params.toString()}`;
    const resp = await fetch(url, { cache: 'no-store' });
    const html = await resp.text();
    if (!resp.ok) {
      console.error('[SystemCal] non-OK', resp.status, html.slice(0,200));
      alert('Failed to load System Calibration');
      return;
    }
    if (!window.BackdropModal) {
      console.error('BackdropModal not defined');
      return;
    }
    const modal = window.BackdropModal.openFromHtml(html, 'systemCalibrationModal');
    if (!modal) {
      alert('Unable to open System Calibration modal');
      return;
    }
    const TAG_ID = 'system-calibration-js';
    const existing = document.getElementById(TAG_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.id = TAG_ID;
      s.src = '/ui_static/js/system_calibration.js?v=' + Date.now();
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
    const modalInDom = document.querySelector('#systemCalibrationModal');
    if (!modalInDom) {
      console.error('[SystemCal] modal not found after mount');
      alert('Unable to open System Calibration modal');
      return;
    }
    if (window.initSystemCalibrationModal) {
      const ok = await window.initSystemCalibrationModal(modalInDom);
      if (!ok) {
        console.error('[SystemCal] initSystemCalibrationModal returned false');
        alert('Unable to open System Calibration modal');
      }
    } else {
      console.error('[SystemCal] initSystemCalibrationModal is not defined');
      alert('Unable to open System Calibration modal');
    }
  } catch (err) {
    console.error('[SystemCal] error', err);
    alert('Error opening System Calibration modal');
  }
};
function _selectSwitchElements(name, key) {
  const real = _realName(name);
  const safe = _safeName(real);
  let box = null, labelEl = null, timeEl = null;
  if (key) {
    box     = document.querySelector(`.switch-box[data-switch-key='${key}'], button.button[data-switch-key='${key}']`);
    labelEl = document.querySelector(`.switch-state[data-switch-key='${key}']`)
          || document.querySelector(`.switch-state[data-switch-name='${real}'][data-switch-key='${key}']`);
    timeEl  = document.querySelector(`.switch-time[data-switch-key='${key}']`)
          || document.querySelector(`.switch-time[data-switch-name='${real}'][data-switch-key='${key}']`);
    if (!box) { return { real, safe, box:null, labelEl:null, timeEl:null }; }
  }
  if (!box) {
    box     = document.querySelector(`.switch-box[data-switch-name='${real}'], button.button[data-switch-name='${real}']`)
           || document.getElementById(`${safe}_btn`)
           || document.getElementById(`${safe}_box`);
    labelEl = document.querySelector(`.switch-state[data-switch-name='${real}']`) || document.getElementById(`${safe}_state`);
    timeEl  = document.querySelector(`.switch-time[data-switch-name='${real}']`)  || document.getElementById(`${safe}_time`);
  }
  if (!labelEl) {
    const pref = (key ? key.replaceAll('::','_') + '_' : '') + safe;
    labelEl = document.getElementById(`${pref}_state`) || document.getElementById(`${safe}_state`);
  }
  if (!timeEl) {
    const pref = (key ? key.replaceAll('::','_') + '_' : '') + safe;
    timeEl  = document.getElementById(`${pref}_time`)  || document.getElementById(`${safe}_time`);
  }
  return { real, safe, box, labelEl, timeEl };
}
function updateSwitchVisuals(name, stateData, key) {
  const { safe, box, labelEl, timeEl } = _selectSwitchElements(name, key);
  try {
    console.debug('[switch-ui] updateSwitchVisuals', {
      name,
      key,
      safe,
      incomingState: stateData && stateData.state,
      boxFound: !!box,
      boxKey: box && box.dataset ? box.dataset.switchKey : '',
      boxName: box && box.dataset ? box.dataset.switchName : '',
      boxText: box ? (box.textContent || '').trim() : '',
      labelFound: !!labelEl,
      labelText: labelEl ? (labelEl.textContent || '').trim() : '',
    });
  } catch (_) {}
  if (!box) { return; }
  const isOn = !!(stateData && (stateData.state===true || String(stateData.state).toLowerCase()==='on'));
  const lastTime = stateData && stateData.time ? stateData.time : '';
  if (!stateData || stateData.pending !== true) setSwitchBoxState(box, isOn, !stateData || stateData.confirmed !== false);
  window.SwitchCommands.apply(box, stateData || {});
  const plugStatus = box.parentElement && box.parentElement.querySelector('.ecowitt-plug-status');
  if (plugStatus && stateData && typeof stateData.online === 'boolean') {
    plugStatus.textContent = stateData.online ? 'Online' : (stateData.confirmed ? 'Offline' : 'Awaiting status');
    plugStatus.title = stateData.error || '';
  }
  try { console.debug('[switch-ui] setSwitchBoxState', { key, name, isOn, boxKey: box.dataset ? box.dataset.switchKey : '', boxText: (box.textContent || '').trim() }); } catch (_) {}
  updateSwitchTimerUi(key, Object.assign({}, stateData || {}, { state: isOn }), name);
  if (labelEl) {
    labelEl.textContent = isOn ? ' ON' : 'OFF';
    labelEl.style.color = isOn ? '#080' : '#666';
    labelEl.style.fontWeight = 'bold';
  }
  try {
    console.debug('[switch-ui] updated', {
      key,
      name,
      isOn,
      boxText: box ? (box.textContent || '').trim() : '',
      labelText: labelEl ? (labelEl.textContent || '').trim() : '',
    });
  } catch (_) {}
}
let switchWS;
let __switchInventoryRefreshAt = 0;
function startSwitchWS(){
  const proto = (location.protocol === 'https:') ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}/ws/switch-updates`;
  try{
    switchWS = new WebSocket(url);
    switchWS.onopen = () => console.debug('Switch WS connected');
    switchWS.onmessage = (ev) => {
      try{
        const msg = JSON.parse(ev.data);
        if (msg.type === 'switch_event'){
          const key = msg.key || '';
          const uiKey = msg.ui_key || key;
          const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key], button[data-switch-key]');
          const label = uiKey.includes('::') ? uiKey.split('::')[1] : (key.includes('::') ? key.split('::')[1] : key);
          const data  = { state: !!msg.state, time: [], timer_seconds: msg.timer_seconds, timer_deadline_epoch: msg.timer_deadline_epoch, timer_remaining_s: msg.timer_remaining_s, ui_key: uiKey };
          if (msg.state_confirmed !== false) {
            updateSwitchVisuals(label, data, uiKey);
          }
          if (typeof window.notifyFullscreenGraphDataChanged === 'function') window.notifyFullscreenGraphDataChanged('switch');
          if (typeof appendSwitchEventLine === 'function'){
            const srcRaw = String(msg.source || '').trim();
            const src = srcRaw.toLowerCase();
            let origin = '';
            if (src.startsWith('mqtt-auto:')) {
              const detail = (srcRaw.split(':').slice(1).join(':') || '').trim();
              origin = detail ? `auto - ${detail}` : 'auto';
            } else if (src.startsWith('mqtt-manual')) {
              origin = 'manual';
            } else if (src.startsWith('auto/rule:')) {
              let detail = srcRaw.split(':').slice(1).join(':') || '';
              if (detail.toLowerCase().endsWith('/mqtt')) detail = detail.slice(0, -5).trim();
              origin = detail ? `auto - ${detail}` : 'auto';
            } else if (src.includes('manual') || src.includes('/ui') || src.includes('ui/')) {
              origin = 'manual';
            } else if (src.includes('auto') || src.includes('rule') || src.includes('timer') || src.includes('automation') || src.includes('schedule')) {
              origin = 'auto';
            }
            const line = `${msg.state ? 'On' : 'Off'} ${msg.timestamp || ''}${origin ? ` (${origin})` : ''}`;
            appendSwitchEventLine(uiKey, line);
          }
        } else if (msg.type === 'automation_toggle'){
          const swId  = msg.switch_id || '';
          const label = msg.label || '';
          applyAutomationStateToSwitch(swId, label, !!msg.enabled);
        } else if (msg.type === 'email_failure'){
          const attempts = Math.max(1, Number(msg.attempts || 1));
          const recipient = String(msg.recipient || '').trim();
          const subject = String(msg.subject || 'Email notification').trim();
          const error = String(msg.error || 'Unknown SMTP error').trim();
          const text = `Email delivery failed after ${attempts} attempt${attempts === 1 ? '' : 's'}${recipient ? ` — To: ${recipient}` : ''} — ${subject} — ${error}`;
          const c = window.getToastContainer ? window.getToastContainer(document.body) : null;
          if (c) {
            const t = document.createElement('div');
            t.className = 'toast error email-failure-toast';
            t.textContent = text;
            t.title = 'Click to dismiss';
            t.setAttribute('role', 'alert');
            t.addEventListener('click', () => { t.remove(); if (!c.children.length) c.remove(); });
            c.appendChild(t);
          }
        } else if (msg.type === 'automation_notification'){
          if (msg.weather_alert_id && Array.from(document.querySelectorAll('[data-weather-alert-id]')).some(el => el.dataset.weatherAlertId === msg.weather_alert_id)) return;
          const name = String(msg.name || msg.rule_id || 'Automation').trim();
          const trigger = msg.trigger && typeof msg.trigger === 'object' ? msg.trigger : {};
          const device = String(trigger.device || trigger.sensor_id || 'Unknown device').trim();
          const metric = String(trigger.metric || 'Unknown metric').trim();
          const value = trigger.value === null || trigger.value === undefined ? 'unavailable' : String(trigger.value);
          const unit = String(trigger.unit || '');
          const conditions = Array.isArray(msg.trigger_conditions) ? msg.trigger_conditions.filter(Boolean).map(String) : [];
          const triggerValues = Array.isArray(msg.trigger_values) ? msg.trigger_values.filter(value => value !== null && value !== undefined).map(String) : [];
          const rawAt = String(msg.occurred_at || '');
          const parsedAt = rawAt ? new Date(rawAt) : new Date();
          const alertDate = parsedAt.toLocaleDateString([], { year: 'numeric', month: 'long', day: 'numeric' });
          const alertTime = parsedAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
          const when = `${alertDate}, ${alertTime}`;
          const conditionText = conditions.length ? conditions.join(' OR ') : `${device} ${metric}`;
          const valueText = triggerValues.length ? triggerValues.join('; ') : `${value}${unit}`;
          const text = `${name} @ ${when}
${conditionText}
${valueText}`;
          const c = window.getToastContainer ? window.getToastContainer(document.body) : null;
          if (c) {
            const t = document.createElement('div');
            t.className = 'toast automation-notification-toast';
            if (msg.weather_alert_id) t.dataset.weatherAlertId = msg.weather_alert_id;
            if (Array.isArray(msg.weather_event_ids) && msg.weather_event_ids.length) { t.style.maxHeight = '60vh'; t.style.overflowY = 'auto'; t.style.overflowWrap = 'anywhere'; t.setAttribute('tabindex', '0'); }
            t.textContent = text;
            t.style.whiteSpace = 'pre-line';
            t.title = 'Click to dismiss';
            t.setAttribute('role', 'status');
            t.addEventListener('click', async () => {
              if (msg.weather_alert_id) {
                try { const response = await fetch('/api/weather-alerts/' + encodeURIComponent(msg.weather_alert_id) + '/dismiss', {method: 'POST'}); if (!response.ok) throw new Error('Dismissal failed'); }
                catch (_) { t.title = 'Could not save dismissal. Click to retry.'; return; }
              }
              t.remove(); if (!c.children.length) c.remove();
            });
            c.appendChild(t);
          }
        } else if (msg.type === 'bd_transition'){
          const rawAt = String(msg.transition_at || '');
          const parsedAt = rawAt ? new Date(rawAt) : null;
          const when = parsedAt && !Number.isNaN(parsedAt.getTime())
            ? parsedAt.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
            : rawAt;
          const segmentText = (segment) => [segment && segment.sign, segment && segment.element, segment && segment.plant_part].map(v => String(v || '').trim()).filter(Boolean).join(' • ') || 'Unknown';
          const transition = `From ${segmentText(msg.from)} → To ${segmentText(msg.to)}`;
          const text = `BD Transition @ ${when}
${transition}`;
          const c = window.getToastContainer ? window.getToastContainer(document.body) : null;
          if (c) {
            const t = document.createElement('div');
            t.className = 'toast bd-transition-toast';
            t.textContent = text;
            t.style.whiteSpace = 'pre-line';
            const colorHelper = window.biodynamicActionColors;
            const actionColors = typeof colorHelper === 'function'
              ? colorHelper(msg.to)
              : { background: String((msg.to && msg.to.color) || ''), text: '#27313a' };
            if (/^#[0-9a-f]{6}$/i.test(actionColors.background)) {
              t.style.backgroundColor = actionColors.background;
              t.style.color = actionColors.text;
            }
            t.title = 'Click to dismiss';
            t.setAttribute('role', 'status');
            t.addEventListener('click', () => { t.remove(); if (!c.children.length) c.remove(); });
            c.appendChild(t);
          }
        } else if (msg.type === 'switch_inventory_changed' || msg.type === 'dashboard_inventory_changed'){
          const now = Date.now();
          if ((now - __switchInventoryRefreshAt) > 1500) {
            __switchInventoryRefreshAt = now;
            if (typeof updateGauges === 'function') {
              setTimeout(() => updateGauges({ ignoreVisibility: true, ignoreModal: true }), 120);
            }
          }
        }
      } catch(e){ console.warn('Bad WS payload', e); }
    };
    window.receiveWeatherAlert = (msg) => switchWS.onmessage({data: JSON.stringify(msg)});
    switchWS.onclose = () => {
      console.warn('Switch WS closed; retrying...');
      setTimeout(startSwitchWS, 2000);
    };
  } catch(e){
    console.warn('WS not available; polling fallback active');
  }
}
document.addEventListener('DOMContentLoaded', startSwitchWS);
const _switchStatusRefreshTimers = new Set();
function scheduleSwitchStatusRefreshes(delays){
  const msList = Array.isArray(delays) ? delays : [0];
  for (const raw of msList) {
    const delay = Math.max(0, Number(raw) || 0);
    const timer = window.setTimeout(async () => {
      _switchStatusRefreshTimers.delete(timer);
      try { await refreshAndApplySwitchStatus(); } catch (_) {  }
    }, delay);
    _switchStatusRefreshTimers.add(timer);
  }
}
async function refreshAndApplySwitchStatus() {
  try {
    const resp = await fetch('/switch-status-update');
    if (!resp.ok) return;
    const statusMap = await resp.json();
    if (!statusMap) return;
    const hasKeyedBoxes = !!document.querySelector('.switch-box[data-switch-key], button[data-switch-key]');
    const now = Date.now();
    Object.entries(statusMap).forEach(([key, data]) => {
      const hasKey = key.includes('::');
      if (!hasKey && hasKeyedBoxes) return;
      if (hasKey && (_switchRefreshBlockUntil.get(key) || 0) > now) return;
      const label = hasKey ? key.split('::')[1] : key;
      updateSwitchVisuals(label, data, hasKey ? key : '');
    });
    updateSwitchEventsFromStatus(statusMap);
  } catch (err) { console.error('Error refreshing switch status', err); }
}
function setSwitchBoxState(box, isOn, confirmed = true) {
  if (!box) return;
  const state = isOn ? 'on' : 'off';
  box.dataset.state = state;
  if (box.tagName === 'BUTTON') {
    box.textContent = confirmed ? (isOn ? 'On' : 'Off') : 'Unknown';
    box.classList.toggle('switch-unconfirmed', !confirmed);
    const name = box.dataset.switchName || 'Switch';
    box.setAttribute('aria-label', confirmed ? name : `${name}: Unknown`);
    box.setAttribute('role', confirmed ? 'switch' : 'button');
    if (confirmed) box.setAttribute('aria-checked', isOn ? 'true' : 'false');
    else box.removeAttribute('aria-checked');
    box.classList.toggle('green', isOn);
    box.classList.toggle('black', !isOn);
  } else {
    box.classList.toggle('on',  isOn);
    box.classList.toggle('off', !isOn);
    box.style.background = isOn ? 'green' : '#aaa';
    box.style.border     = isOn ? '2px solid #080' : '1px solid #666';
  }
}
function applyAutomationStateToSwitch(switchId, label, isEnabled) {
  const selector = label
    ? `[data-automation-switch-id="${switchId}"][data-automation-label="${label}"]`    : `[data-automation-switch-id="${switchId}"]`;
  const nodes = document.querySelectorAll(selector);
  nodes.forEach(node => {
    node.dataset.automationEnabled = isEnabled ? '1' : '0';
    node.classList.toggle('automation-enabled', !!isEnabled);
    if (node.tagName === 'BUTTON') {
      node.title = isEnabled
        ? 'Automation enabled. Disable automation to toggle manually.'
        : `Toggle state for ${node.dataset.switchName || label || 'switch'}`;
      node.setAttribute('aria-disabled', isEnabled ? 'true' : 'false');
    }
  });
}
function formatSwitchEventDisplayLine(line){
  return String(line || '').replace(/(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})/, (_match, date, hour, minute, second) => {
    const hour24 = Number.parseInt(hour, 10);
    if (!Number.isFinite(hour24)) return _match;
    const hour12 = ((hour24 + 11) % 12) + 1;
    return `${date} ${hour12}:${minute}:${second} ${hour24 < 12 ? 'AM' : 'PM'}`;
  });
}

  const _eventIdentityKey = (line) => {
    const text = String(line || '').trim();
    const stateMatch = text.match(/^(On|Off)\b/i);
    const tsMatch = text.match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?/);
    if (!stateMatch || !tsMatch) return text;
    const epoch = Date.parse(tsMatch[0].replace(' ', 'T'));
    return `${stateMatch[1].toLowerCase()}|${Number.isFinite(epoch) ? epoch : tsMatch[0]}`;
  };
  const _eventLineRank = (line) => {
    const text = String(line || '').toLowerCase();
    if (/\((manual|auto)(\s*-\s*[^)]*)?\)/.test(text)) return 2;
    return 1;
  };
  const _mergeEventLines = (existingLines, newLines, maxEvents) => {
    const bestByEvent = new Map();
    for (const line of [...existingLines, ...newLines]) {
      const text = String(line || '').trim();
      if (!text) continue;
      const identity = _eventIdentityKey(text);
      const prior = bestByEvent.get(identity);
      if (!prior || _eventLineRank(text) >= _eventLineRank(prior)) bestByEvent.set(identity, text);
    }
    const extractTs = (line) => {
      const m = String(line || '').match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?/);
      return m ? Date.parse(m[0].replace(' ', 'T')) : NaN;
    };
    return Array.from(bestByEvent.values()).sort((a,b) => {
      const ta = extractTs(a);
      const tb = extractTs(b);
      if (Number.isFinite(ta) && Number.isFinite(tb)) return tb - ta;
      if (Number.isFinite(tb)) return 1;
      if (Number.isFinite(ta)) return -1;
      return 0;
    }).slice(0, maxEvents);
  };
function updateSwitchEventsFromStatus(statusData){
  if(!statusData || typeof statusData!=='object') return;
  for(const [key,data] of Object.entries(statusData)){
    const listElem=_findEventsListElem(key);
    if(!listElem){
      continue;
    }

    const hasTime = Object.prototype.hasOwnProperty.call(data,'time') || Array.isArray(data?.events);
    let events=[];
    if(Array.isArray(data?.events)){
      events = data.events.slice();
    } else if(Array.isArray(data?.time)){
      events = data.time.slice();
    } else if(typeof data?.time==='string'){
      events = [data.time];
    } else if(data && (data.state!==undefined || data.time!==undefined)){
      const isOn = (data.state===true) || (String(data.state).toLowerCase()==='on');
      const ts   = (typeof data.time==='string') ? data.time : '';
      events = [ ts ? ((isOn?' ON ':'OFF ')+ts) : (isOn?' ON':'OFF') ];
    }

    if(!hasTime || events.length===0){
      continue;
    }

    const _originFromSource = (source) => {
      const srcRaw = String(source || '').trim();
      const src = srcRaw.toLowerCase();
      if (!src) return '';
      if (src.startsWith('mqtt-auto:')) {
        const detail = (srcRaw.split(':').slice(1).join(':') || '').trim();
        return detail ? `auto - ${detail}` : 'auto';
      }
      if (src.startsWith('mqtt-manual')) return 'manual';
      if (src.startsWith('auto/rule:')) {
        let detail = srcRaw.split(':').slice(1).join(':') || '';
        if (detail.toLowerCase().endsWith('/mqtt')) detail = detail.slice(0, -5).trim();
        return detail ? `auto - ${detail}` : 'auto';
      }
      if (src === 'ui' || src.includes('manual') || src.includes('/ui') || src.includes('ui/') || src.includes('user')) return 'manual';
      if (src.includes('auto') || src.includes('rule') || src.includes('timer') || src.includes('automation') || src.includes('schedule')) return 'auto';
      return '';
    };

    const normLine=(evt)=>{
      if(evt && typeof evt==='object'){
        const isOn=(String(evt.state).toLowerCase()==='on')||(evt.state===true);
        const tsRaw=evt.ts||evt.time||'';
        const ts=String(tsRaw).replace('T', ' ');
        const origin = _originFromSource(evt.source);
        const suffix = origin ? ` (${origin})` : '';
        const line = ts ? ((isOn?' On ':' Off ')+ts+suffix) : ((isOn?' On':' Off')+suffix);
        return line.trim();
      }
      let s=String(evt||'').replace(/^'+|'+$/g,'');
      s=s.replace(/(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})(?:\.\d{1,6}(?=Z|[+-]\d{2}:\d{2}|$))?/g,(_,a)=>a);
      s=s.replace('T',' ');
      return s;
    };

    const hasTimestamp = (line) => /(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/.test(String(line || ''));

    const newLines = events.map(normLine).filter(Boolean).filter(hasTimestamp);
    const existingLines = Array.from(listElem.querySelectorAll('li'))
          .map(li => (li.dataset.rawEvent || li.textContent || '').trim())
          .filter(Boolean)
          .filter(hasTimestamp);

    const MAX_EVENTS = 10;
    const trimmedLines = _mergeEventLines(existingLines, newLines, MAX_EVENTS);

    const sig   = trimmedLines.join('\n');
    const prev  = _switchEventsCache.get(key) || listElem.dataset.eventsSig || '';
    if(sig===prev){
      window.SwitchHistory.render(listElem);
      continue;
    }

    listElem.textContent='';
    for(const textLine of trimmedLines){
      const li=document.createElement('li');
      li.dataset.rawEvent=textLine;
      li.textContent=formatSwitchEventDisplayLine(textLine);
      if(/^on\b/i.test(textLine)) li.classList.add('switch-event-on');
      else                          li.classList.add('switch-event-off');
      listElem.appendChild(li);
    }
    _switchEventsCache.set(key,sig);
    listElem.dataset.eventsSig=sig;
    window.SwitchHistory.render(listElem);
  }
}

function appendSwitchEventLine(key, line){
  const listElem = _findEventsListElem(key);
  if(!listElem) return;
  const text = String(line || '').trim();
  if(!text) return;
  const hasTimestamp = /(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/.test(text);
  if(!hasTimestamp) return;
  const existingLines = Array.from(listElem.querySelectorAll('li'))
        .map(li => (li.dataset.rawEvent || li.textContent || '').trim())
        .filter(Boolean)
        .filter(value => /(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/.test(String(value || '')));
  const sortedLines = _mergeEventLines(existingLines, [text], 10);
  listElem.textContent = '';
  for(const textLine of sortedLines){
    const li = document.createElement('li');
    li.dataset.rawEvent = textLine;
    li.textContent = formatSwitchEventDisplayLine(textLine);
    if(/^on\b/i.test(textLine)) li.classList.add('switch-event-on');
    else                         li.classList.add('switch-event-off');
    listElem.appendChild(li);
  }
  const sig = sortedLines.join('\n');
  _switchEventsCache.set(key, sig);
  listElem.dataset.eventsSig = sig;
  window.SwitchHistory.render(listElem);
}
window.toggleSwitchInline = function(elOrLabel) {
  let el = null;
  let real = '';

  if (elOrLabel && typeof elOrLabel === 'object' && elOrLabel.nodeType === 1) {
    el = elOrLabel;
    real = el.dataset.switchName || '';
  } else {
    real = _realName(elOrLabel);
    const safe = _safeName(real);
    el = document.getElementById(`${safe}_box`) || document.querySelector(`.switch-box[data-switch-name='${real}']`);
  }

  if (!el) { console.warn('[toggleSwitchInline] no element for', elOrLabel); return; }
  if (el.classList.contains('switch-pending')) return;
  if ((el.dataset.automationEnabled || '') === '1') {
    alert('Automation is enabled for this switch. Disable automation before toggling manually.');
    return;
  }
  window.SwitchCommands.begin(el);

  const key = el.dataset.switchKey || '';
  const switchId = el.dataset.switchId || '';
  const params = new URLSearchParams();
  params.set('switch_name', real);
  if (key) params.set('switch_key', key);
  if (switchId) params.set('switch_id', switchId);

  fetch(`/switch/toggle?${params.toString()}`, { method: 'POST' })
    .then(async r => {
      if (r.status === 409) {
        const info = await r.json().catch(() => null);
        const msg = (info && info.message) || 'Multiple devices have this label. Please specify a device.';
        console.warn('Ambiguous switch label', info && info.options);
        alert(msg);
        throw new Error('Ambiguous');
      }
      if (r.status === 423) {
        const info = await r.json().catch(() => null);
        const msg = (info && info.message) || 'Automation is enabled for this switch. Disable automation before toggling manually.';
        alert(msg);
        throw new Error('Automation enabled');
      }
      if (key) _switchRefreshBlockUntil.set(key, Date.now() + 1200);
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        throw new Error(`HTTP ${r.status} ${txt}`);
      }
      return r.json();
    })
    .then(data => {
      updateSwitchVisuals(real, data, key);
      updateSwitchEventsFromStatus({ [key || `::${real}`]: data });
    })
    .catch(e => {
      console.warn('toggleSwitchInline failed', e);
      window.SwitchCommands.apply(el, {pending:false, command_error:'Switch command failed. Check device status.'});
      const status = el.parentElement && el.parentElement.querySelector('.ecowitt-plug-status');
      if (status) status.textContent = 'Plug command failed; check gateway status.';
    })
    .finally(() => {
      window.SwitchCommands.finish(el);
      scheduleSwitchStatusRefreshes([1500, 6000, 12000, 46000]);
    });
};
window.toggleAutomation = async function(btn, switchId, label) {
  try {
    const key = `${switchId}::${label}`;
    const isEnabled = ((btn.textContent || '').trim().toLowerCase() === 'enabled');
    const nextEnabled = !isEnabled;
    btn.disabled = true;
    const res = await fetch(`/switch/override?switch_key=${encodeURIComponent(key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: nextEnabled })
    });
    if (!res.ok) {
      const info = await res.json().catch(() => ({}));
      alert(info.error || 'Failed to update automation.');
      btn.disabled = false;
      return;
    }
    const json = await res.json();
    const ruleEnabled = !!json.enabled;
    btn.textContent = ruleEnabled ? 'Enabled' : 'Disabled';
    btn.classList.toggle('green', ruleEnabled);
    btn.classList.toggle('black', !ruleEnabled);
  } catch (e) {
    console.error('toggleAutomation error', e);
  } finally {
    btn.disabled = false;
  }
};
async function saveSwitchTimer(inputEl){
  if (!inputEl) return;
  const switchId = inputEl.dataset.switchId || '';
  const label = inputEl.dataset.label || '';
  const uiKey = inputEl.dataset.switchUiKey || `${switchId}::${label}`;
  const raw = String(inputEl.value || '').trim();
  let parsed = Number(raw);
  const prior = String(inputEl.dataset.lastGoodValue || '0');
  if (Number.isInteger(parsed) && parsed > 0 && parsed < 30) parsed = 30;
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 9999) {
    inputEl.value = prior;
    alert('Timer must be 0 or between 30 and 9999 seconds.');
    return;
  }
  if (parsed !== 0 && parsed % 30 !== 0) {
    parsed = Math.min(9990, Math.max(30, Math.round(parsed / 30) * 30));
  }
  inputEl.value = String(parsed);
  inputEl.disabled = true;
  try {
    const res = await fetch(`/switch/timer?switch_id=${encodeURIComponent(switchId)}&switch_name=${encodeURIComponent(label)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: parsed })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      inputEl.value = prior;
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    inputEl.dataset.lastGoodValue = String(data.timer_seconds || 0);
    updateSwitchTimerUi((data && data.ui_key) || uiKey, data || {}, label);
    const panel = _findTimerPanel((data && data.ui_key) || uiKey);
    _setTimerEditorOpen(panel, false);
  } catch (err) {
    console.error('saveSwitchTimer failed', err);
    alert('Failed to update timer.');
  } finally {
    inputEl.disabled = false;
  }
}
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.switch-toggle');
  if (!btn) return;
  if ((btn.dataset.automationEnabled || '') === '1') {
    alert('Automation is enabled for this switch. Disable automation before toggling manually.');
    return;
  }
  const key        = btn.dataset.switchKey || '';
  const _switch_id = btn.dataset.switchId  || '';
  const label      = btn.dataset.switchLabel || '';
  try {
    const params = new URLSearchParams();
    if (label)      params.set('switch_name', label);
    if (key)        params.set('switch_key', key);
    if (_switch_id) params.set('switch_id', _switch_id);
    const res = await fetch(`/switch/toggle?${params.toString()}`, { method: 'POST' });
    if (res.status === 409) {
      const info = await res.json();
      console.warn('Ambiguous switch name. Options:', info.options);
      return;
    }
    if (res.status === 423) {
      const info = await res.json().catch(() => null);
      alert((info && info.message) || 'Automation is enabled for this switch. Disable automation before toggling manually.');
      return;
    }
    const data = await res.json();
    if (data && data.state !== undefined) {
      const keyEff = key || (_switch_id && label ? `${_switch_id}::${label}` : '');
      if (keyEff && typeof _switchRefreshBlockUntil !== 'undefined' && _switchRefreshBlockUntil) {
        try {
          _switchRefreshBlockUntil.set(keyEff, Date.now() + 1200);
        } catch (_) {  }
      }
      if (label) {
        updateSwitchVisuals(label, data, keyEff);
      }
      if (keyEff) {
        updateSwitchEventsFromStatus({ [keyEff]: data });
      }
      scheduleSwitchStatusRefreshes([1500, 6000, 12000]);
    }
  } catch (e) {
    console.error('Toggle failed', e);
  }
});
document.addEventListener('keydown', function(ev){
  const input = ev.target.closest('.switch-timer-input');
  if (!input) return;
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  saveSwitchTimer(input);
});
document.addEventListener('click', function(ev){
  const editBtn = ev.target.closest('.switch-timer-edit-btn');
  if (editBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const panel = editBtn.closest('.switch-timer-panel');
    if (!panel) return;
    const editor = panel.querySelector('.switch-timer-editor');
    const willOpen = !editor || editor.style.display === 'none';
    _closeAllTimerEditors();
    _setTimerEditorOpen(panel, willOpen);
    if (willOpen) {
      const input = panel.querySelector('.switch-timer-input');
      if (input) { input.focus(); input.select(); }
    }
    return;
  }
  const confirmBtn = ev.target.closest('.switch-timer-confirm-btn');
  if (confirmBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const inputId = confirmBtn.dataset.inputId || '';
    const input = inputId ? document.getElementById(inputId) : null;
    if (input) saveSwitchTimer(input);
    return;
  }
  const cancelBtn = ev.target.closest('.switch-timer-cancel-btn');
  if (cancelBtn) {
    ev.preventDefault();
    ev.stopPropagation();
    const inputId = cancelBtn.dataset.inputId || '';
    const input = inputId ? document.getElementById(inputId) : null;
    if (input) input.value = String(input.dataset.lastGoodValue || '0');
    const editorId = cancelBtn.dataset.editorId || '';
    const editor = editorId ? document.getElementById(editorId) : null;
    const panel = editor ? editor.closest('.switch-timer-panel') : null;
    _setTimerEditorOpen(panel, false);
    return;
  }
  if (!ev.target.closest('.switch-timer-panel')) _closeAllTimerEditors();
});
window.fetchSensorIds = async function(){
  try { const r = await fetch('/sensor-ids'); return await r.json(); }
  catch(e){ console.error('Failed to load sensors', e); return []; }
};
window.SwitchModal = window.SwitchModal || (function(){
  function mount(modal){
    if (!modal) return;
    if (modal.id !== 'switchSettingsModal') modal.id = 'switchSettingsModal';
    modal.dataset.switchMounted = '1';
    modal.style.display = 'none';
  }
  function open(){
    const m = document.getElementById('switchSettingsModal');
    if (m) m.style.display = 'block';
  }
  function close(){
    const m = document.getElementById('switchSettingsModal');
    if (m) m.remove();
  }
  return { mount, open, close };
})();

const SENSOR_STATUS_COLORS = { online:'#28a745', degraded:'#fd7e14', offline:'#dc3545', unknown:'#ffc107', migration_required:'#6c757d' };
let __lastJsonOnly = null;
let __lastJsonOnlyAtMs = 0;
function applySensorStatuses(data){
  const statuses = data && data.statuses ? data.statuses : {};
  Object.entries(statuses).forEach(([sid,st]) => {
    const dot = document.getElementById(`${sid}_statusdot`);
    if (!dot) return;
    if (sid.startsWith('ecowitt-') || sid.startsWith('weewx') || Object.hasOwn(data.battery_statuses || {}, sid)) {
      let icon = document.getElementById(`${sid}_battery`);
      if (!icon) { icon = document.createElement('img'); icon.id = `${sid}_battery`; icon.className = 'sensor-battery-icon'; icon.width = 26; icon.height = 18; icon.style.cssText = 'vertical-align:middle;margin-right:6px'; dot.after(icon); }
      const raw = data.battery_statuses?.[sid];
      const battery = ['OK', 'LOW'].includes(raw) ? raw : 'UNKNOWN';
      icon.src = `/ui_static/icons/battery-${battery.toLowerCase()}.svg`;
      icon.alt = icon.title = `Battery Status: ${battery}`;
    }
    const s = (st||'unknown').toLowerCase();
    const color = SENSOR_STATUS_COLORS[s] || SENSOR_STATUS_COLORS.unknown;
    dot.style.background = color;
    dot.title = `Measurement status: ${s}`;
    dot.setAttribute('aria-label', `Measurement status: ${s}`);
  });
}
function isDashboardWarmingPayload(payload){
  const reason = String((payload && payload.reason) || '').trim();
  const cacheStatus = String((payload && payload.cache_status) || '').trim();
  return reason === 'warming' || cacheStatus === 'warming';
}
function setDashboardCardLoading(cardId, isLoading){
  const card = document.getElementById(cardId);
  if (!card) return;
  card.classList.toggle('card-loading', !!isLoading);
  card.setAttribute('aria-busy', isLoading ? 'true' : 'false');
}
function setAstroCardsLoading(isLoading){
  ['moonBox','sunBox','sunMoon29Card'].forEach((id) => setDashboardCardLoading(id, isLoading));
}
function setBiodynamicCardLoading(isLoading){
  setDashboardCardLoading('bioBox', isLoading);
}
function applyAstroPayloadIfStable(payload){
  if (!payload || typeof payload !== 'object') return;
  const warming = isDashboardWarmingPayload(payload);
  const keepExistingAstro = warming && astroData && astroData.ok;
  setAstroCardsLoading(warming && !keepExistingAstro);
  if (keepExistingAstro) return;
  if (!payload.ok && astroData && astroData.ok && !payload.reason && !payload.cache_status) return;
  if (!warming) { delete astroData.reason; delete astroData.cache_status; }
  Object.assign(astroData, payload);
  if (typeof drawSunPath === 'function') drawSunPath(astroData);
  if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData);
  if (typeof drawSunMoon29Day === 'function' && sunMoon29IsOpen()) drawSunMoon29Day(astroData);
}
function applyBiodynamicPayloadIfStable(payload){
  if (!payload || typeof payload !== 'object') return;
  const warming = isDashboardWarmingPayload(payload);
  const keepExistingBiodynamic = warming && biodynamicData && biodynamicData.ok;
  setBiodynamicCardLoading(warming && !keepExistingBiodynamic);
  if (keepExistingBiodynamic) return;
  Object.keys(biodynamicData).forEach((k) => { delete biodynamicData[k]; });
  Object.assign(biodynamicData, payload);
  if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData);
}
let dashboardStatusRefreshTimer = null;
let dashboardStatusRefreshInFlight = false;
async function refreshAndApplySensorStatus(){
  if (dashboardStatusRefreshInFlight) return;
  clearTimeout(dashboardStatusRefreshTimer);
  let retrySoon = false;
  dashboardStatusRefreshInFlight = true;
  try {
    if (typeof dashboardRefreshPaused === 'function' && dashboardRefreshPaused()) {
      return;
    }
    const now = Date.now();
    let data = null;
    let fetchedExtras = false;
    const extrasWarming = !!window.__dashboardExtrasWarming || isDashboardWarmingPayload(astroData) || isDashboardWarmingPayload(biodynamicData);
    const lastExtrasRefreshAt = window.__lastExtrasRefreshAtMs || 0;
    const lastExtrasWarmAt = window.__lastExtrasWarmAtMs || 0;
    const wantExtras = !lastExtrasRefreshAt || ((now - lastExtrasRefreshAt) >= dashboardExtrasRefreshMs) || (extrasWarming && (!lastExtrasWarmAt || ((now - lastExtrasWarmAt) >= dashboardExtrasWarmRetryMs)));
    if (__lastJsonOnly && (now - __lastJsonOnlyAtMs) < 20000 && !wantExtras) {
      data = __lastJsonOnly;
    } else {
      const url = new URL(window.location.href);
      url.searchParams.set('json_only','true');
      if (wantExtras) url.searchParams.set('include_extras','true');
      data = await fetchWithTimeout(url.toString(), { cache:'no-store' }, dashboardFetchTimeoutMs, async resp => {
        if (!resp.ok) throw new Error('dashboard_' + resp.status);
        return await resp.json();
      });
      fetchedExtras = wantExtras;
      __lastJsonOnly = data;
      __lastJsonOnlyAtMs = now;
      if (wantExtras) {
        const astroWarming = data && data.astro && isDashboardWarmingPayload(data.astro);
        const biodynamicWarming = data && data.biodynamic && isDashboardWarmingPayload(data.biodynamic);
        if (!data?.astro || !data?.biodynamic || astroWarming || biodynamicWarming) {
          window.__dashboardExtrasWarming = true;
          window.__lastExtrasWarmAtMs = now;
        } else {
          window.__dashboardExtrasWarming = false;
          window.__lastExtrasRefreshAtMs = now;
          window.__lastExtrasWarmAtMs = 0;
        }
      }
    }
    applySensorStatuses(data);
    if (fetchedExtras && data && data.astro && typeof data.astro === 'object') {
      applyAstroPayloadIfStable(data.astro);
    }
    if (fetchedExtras && data && data.biodynamic && typeof data.biodynamic === 'object') {
      applyBiodynamicPayloadIfStable(data.biodynamic);
    }
    retrySoon = !!window.__dashboardExtrasWarming;
  } catch (_) {
    retrySoon = true;
  } finally {
    dashboardStatusRefreshInFlight = false;
    dashboardStatusRefreshTimer = setTimeout(refreshAndApplySensorStatus, retrySoon ? dashboardExtrasWarmRetryMs : 15000);
  }
}
function startDashboardStatusRefresh(){
  dashboardStatusRefreshTimer = setTimeout(refreshAndApplySensorStatus, 400);
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startDashboardStatusRefresh, {once:true});
} else {
  startDashboardStatusRefresh();
}
window.toggleScriptEnable = async function(btn, channel, scriptName, enabled){
  try {
    btn.disabled = true;
    const next = !enabled;
    btn.textContent = next ? 'On':'Off';
    btn.classList.toggle('green', next);
    btn.classList.toggle('black', !next);
    btn.setAttribute('onclick', `toggleScriptEnable(this, "${channel}", "${scriptName}", ${next})`);
  } finally { btn.disabled = false; }
};
window.fetchMetrics = async function(sensorId){
  try { const r = await fetch(`/sensor-metrics?sensor_id=${encodeURIComponent(sensorId)}`); return await r.json(); }
  catch(e){ console.error('Failed metrics for', sensorId, e); return {}; }
};
let dashboardThemeReturnFocus = null;
let savedDashboardTheme = (Array.from(document.body.classList).find(function(name){ return /^dashboard-theme-(leaf|root|leaf-crop|flower|fruit|custom)$/.test(name); }) || 'dashboard-theme-leaf').slice(16);
let savedDashboardCustomStyle = document.body.getAttribute('style') || '';
function applyDashboardPreviewTheme(theme){
  const supported = new Set(['leaf','root','leaf-crop','flower','fruit']);
  const nextTheme = supported.has(theme) ? theme : 'leaf';
  Array.from(document.body.classList).filter(function(name){ return /^dashboard-theme-(leaf|root|leaf-crop|flower|fruit|custom)$/.test(name); }).forEach(function(name){ document.body.classList.remove(name); });
  document.body.classList.add('dashboard-theme-' + nextTheme);
  document.querySelectorAll('[data-dashboard-preview-theme]').forEach(function(button){
    const active = button.dataset.dashboardPreviewTheme === nextTheme;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}
function openDashboardThemeView(){
  const view = document.getElementById('dashboardThemeView');
  if (!view || document.body.classList.contains('dashboard-theme-preview-mode')) return;
  dashboardThemeReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  savedDashboardTheme = (Array.from(document.body.classList).find(function(name){ return /^dashboard-theme-(leaf|root|leaf-crop|flower|fruit|custom)$/.test(name); }) || ('dashboard-theme-' + savedDashboardTheme)).slice(16);
  savedDashboardCustomStyle = document.body.getAttribute('style') || '';
  if (savedDashboardTheme === 'custom') { document.body.removeAttribute('style'); applyDashboardPreviewTheme('leaf'); } else { applyDashboardPreviewTheme(savedDashboardTheme); }
  view.hidden = false;
  document.body.classList.add('dashboard-theme-preview-mode');
  document.querySelector('.dashboard-content')?.setAttribute('aria-hidden', 'true');
  document.getElementById('closeDashboardThemeBtn')?.focus({preventScroll:true});
}
function closeDashboardThemeView(){
  const view = document.getElementById('dashboardThemeView');
  if (!view || !document.body.classList.contains('dashboard-theme-preview-mode')) return;
  document.body.classList.remove('dashboard-theme-preview-mode');
  view.hidden = true;
  document.querySelector('.dashboard-content')?.removeAttribute('aria-hidden');
  if (savedDashboardTheme === 'custom') {
    Array.from(document.body.classList).filter(function(name){ return /^dashboard-theme-(leaf|root|leaf-crop|flower|fruit|custom)$/.test(name); }).forEach(function(name){ document.body.classList.remove(name); });
    document.body.classList.add('dashboard-theme-custom');
    if (savedDashboardCustomStyle) document.body.setAttribute('style', savedDashboardCustomStyle);
  } else { applyDashboardPreviewTheme(savedDashboardTheme); }
  if (dashboardThemeReturnFocus && dashboardThemeReturnFocus.isConnected) dashboardThemeReturnFocus.focus({preventScroll:true});
  dashboardThemeReturnFocus = null;
}
function initializeDashboardThemePreview(){
  document.getElementById('dashboardThemeBtn')?.addEventListener('click', openDashboardThemeView);
  document.getElementById('closeDashboardThemeBtn')?.addEventListener('click', closeDashboardThemeView);
  document.querySelectorAll('[data-dashboard-preview-theme]').forEach(function(button){ button.addEventListener('click', function(){ applyDashboardPreviewTheme(button.dataset.dashboardPreviewTheme || 'leaf'); }); });
  document.getElementById('dashboardThemeView')?.addEventListener('click', function(event){ if (event.target === event.currentTarget) closeDashboardThemeView(); });
  document.addEventListener('keydown', function(event){ if (event.key === 'Escape' && document.body.classList.contains('dashboard-theme-preview-mode')) { event.preventDefault(); closeDashboardThemeView(); } });
}
window.onload = function() {
  setTimeout(checkAndRetryIfNoGauges, 1000);
  initGauge();
  startRainGauges(gaugeConfig, initGauge);
  initializeDashboardThemePreview();
  initializeTrendArrows();
  initSwitchTimersFromDom();
  refreshAndApplySwitchStatus();
  setTimeout(updateGauges, 600);
  const sel = document.getElementById('sensorSelect');
  if (sel) {
    sel.addEventListener('change', function() {
      const selected = this.value;
      const path = window.location.pathname;
      window.location.href = `${path}?sensor_id=${encodeURIComponent(selected)}`;
    });
  }
  setInterval(updateLocalTime, 1000);
  setInterval(tickSwitchCountdowns, 1000);
  setInterval(function(){ if (typeof drawSunPath === 'function') drawSunPath(astroData); if (typeof drawSunMoon29Day === 'function' && sunMoon29IsOpen()) drawSunMoon29Day(astroData); }, 60000);
  setInterval(function(){ if (typeof drawMoonPhase === 'function') drawMoonPhase(astroData); }, 3600000);
  setInterval(function(){ if (typeof drawBiodynamic === 'function') drawBiodynamic(biodynamicData); }, 60000);
  setInterval(checkAndRetryIfNoGauges, 60000);
  setInterval(() => updateGauges({ ignoreModal: true }), 15000);
  setInterval(refreshAndApplySwitchStatus, 60000);
  setInterval(function() {
    if (typeof window.refreshAllMicrographs === 'function') {
      window.refreshAllMicrographs(false, { ignoreModal: true });
    }
  }, 60000);
}

}
