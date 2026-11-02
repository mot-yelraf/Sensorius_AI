import { renderWindRose, gaugeZonesBackgroundMicro, convertMetricValue } from '/ui_static/js/metric_visuals.js';
import { pressureGaugeConfig } from '/ui_static/js/pressure_gauges.js';
import { startRainGauges } from '/ui_static/js/rain_gauges.js';

const grid = document.getElementById('stationMetricGrid');
const cards = new Map();
const windCharts = new Map();
if (window.Chart) window.Chart.register(gaugeZonesBackgroundMicro);
const rainConfigs = {};
let stopRain = null;
let rainSignature = '';
let current = {};
let stationExpanded = false;
const stationToggle = document.getElementById('stationMetricsToggle');
const stationLabel = document.getElementById('stationMetricsLabel');
const modes = ['Gauge', 'Graph24hr', 'Graph6hr'];
const labels = {Gauge: 'Gauge', Graph24hr: '24Hr Graph', Graph6hr: '6Hr Graph'};

function expansionKey() {
  return `caelus:station-expanded:${current.sensor_id || ''}`;
}

function applyStationExpansion() {
  if (!grid || !stationToggle) return;
  const allCards = Array.from(grid.children);
  const first = allCards[0];
  if (!first) {
    stationToggle.disabled = true;
    stationToggle.setAttribute('aria-expanded', 'false');
    return;
  }
  const gap = Number.parseFloat(getComputedStyle(grid).columnGap) || 10;
  const width = first.getBoundingClientRect().width;
  const capacity = Math.max(1, Math.floor((grid.clientWidth + gap) / (width + gap)));
  const hasOverflow = allCards.length > capacity;
  allCards.forEach((node, index) => {
    node.hidden = !stationExpanded && index >= capacity;
    if (node.hidden) {
      const card = cards.get(node.dataset.metric);
      if (card) { card.visible = false; card.controller?.abort(); }
      if (node.contains(document.activeElement)) stationToggle.focus({preventScroll: true});
    }
  });
  stationToggle.disabled = !hasOverflow;
  stationToggle.setAttribute('aria-expanded', String(stationExpanded || !hasOverflow));
  stationToggle.title = hasOverflow ? (stationExpanded ? 'Collapse station metrics to one row' : 'Expand all station metrics') : 'All station metrics fit in one row';
}

stationToggle?.addEventListener('click', () => {
  stationExpanded = !stationExpanded;
  try { sessionStorage.setItem(expansionKey(), String(stationExpanded)); } catch (_) {}
  applyStationExpansion();
});
let stationWidth = -1;
if (grid) new ResizeObserver(entries => {
  const width = entries[0].contentRect.width;
  if (Math.abs(width - stationWidth) < .5) return;
  stationWidth = width;
  applyStationExpansion();
}).observe(grid);

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function format(value, config) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return Number.isInteger(config.display_precision) ? Number(value).toFixed(config.display_precision) : String(Number(Number(value).toFixed(2)));
}

function storageKey(metric) {
  return `caelus:metric-mode:${current.sensor_id}:${metric}`;
}

function setMode(card, mode) {
  card.mode = mode;
  card.node.dataset.displayStyle = mode;
  card.gaugeView.hidden = mode !== 'Gauge';
  card.graphView.hidden = mode === 'Gauge';
  updateTitle(card);
  card.node.setAttribute('aria-label', `${card.metric.name}: ${labels[mode]}. Select to change view.`);
  if (card.visible) render(card);
}

function updateTitle(card) {
  const graph = card.mode !== 'Gauge';
  const prefix = graph ? (card.mode === 'Graph6hr' ? '6hr ' : '24hr ') : '';
  const name = graph && card.metric.config.render === 'compass' ? 'Wind-Rose' : card.metric.name;
  const unit = graph && card.metric.config.render === 'compass' ? '' : card.metric.config.unit;
  card.title.textContent = `${prefix}${name}${unit ? ` (${unit})` : ''}`;
}

function timestampText(value) {
  if (!value) return '—';
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (!match) return String(value);
  const hour = Number(match[4]);
  return `${match[1]}/${match[2]}/${match[3]}\n${hour % 12 || 12}:${match[5]}:${match[6]} ${hour < 12 ? 'AM' : 'PM'}`;
}

function updateDetails(card) {
  const metric = card.metric;
  const config = metric.value_config || metric.config;
  card.value.replaceChildren(document.createTextNode(`${format(metric.value, config)} ${metric.unit || ''}`.trim()));
  const trend = metric.statistics?.trend;
  if (trend && Number.isFinite(Number(trend.rate_per_hour))) {
    const statConfig = metric.statistics_config || config;
    const rate = convertMetricValue(trend.rate_per_hour, statConfig, true);
    const pressure = /(?:^| )Baro-Pressure$|^Pressure$/.test(metric.metric);
    const span = Math.max(.000001, Number(statConfig.max ?? 100) - Number(statConfig.min ?? 0));
    const dead = pressure ? Math.abs(convertMetricValue(.1, statConfig, true)) : span * .0025;
    const full = pressure ? Math.abs(convertMetricValue(1, statConfig, true)) : span * .025;
    const strength = Math.abs(rate) <= dead ? 0 : Math.min(1, (Math.abs(rate) - dead) / Math.max(.000001, full - dead));
    const angle = rate >= 0 ? (strength <= .5 ? -30 * strength : -15 - 150 * (strength - .5)) : (strength <= .5 ? 100 * strength : 50 + 80 * (strength - .5));
    const arrow = element('span', 'metric-trend', '→');
    arrow.style.transform = `rotate(${angle}deg)`;
    arrow.title = `${format(rate, statConfig)} ${metric.unit || ''}/hour · ${trend.samples || 0} samples${trend.provisional ? ' · provisional' : ''}`;
    arrow.setAttribute('role', 'img');
    arrow.setAttribute('aria-label', arrow.title);
    card.value.append(arrow);
  }
  card.stats.replaceChildren();
  const statConfig = metric.statistics_config || config;
  for (const [key, label] of [['min', 'Min'], ['avg', 'Avg'], ['max', 'Max']]) {
    const column = element('div', '');
    column.append(element('span', '', label));
    const statValue = metric.statistics?.[key];
    const extrema = key !== 'avg';
    column.append(element('strong', '', `${format(statValue, {...statConfig, display_precision: statConfig.display_precision ?? 1})}${extrema && statValue != null ? ' at' : ''}`));
    if (extrema) column.append(element('small', '', timestampText(metric.statistics?.[`${key}_ts`])));
    card.stats.append(column);
  }
  card.stats.title = current.statistics_available === false ? '24-hour statistics are unavailable.' : 'Statistics over the last 24 hours';
}

function drawGauge(card) {
  if (typeof window.Gauge !== 'function') {
    card.status.textContent = 'Gauge unavailable. Select this tile to view its graph.';
    return;
  }
  const config = pressureGaugeConfig(card.metric.metric, rainConfigs[card.metric.metric] || card.metric.config, current.pressure_altitude, current.pressure_context);
  const options = {
    angle: -.25, lineWidth: .25, radiusScale: .9,
    pointer: {length: .5, strokeWidth: .035, color: '#000000'},
    staticZones: config.zones || [],
    staticLabels: {font: '12px sans-serif', labels: config.ticks || [], color: '#000000', fractionDigits: 1},
    colorStart: '#6FADCF', colorStop: '#8FC0DA', strokeColor: '#E0E0E0',
    limitMax: Boolean(config.rain_period), highDpiSupport: true
  };
  if (!card.gauge) {
    card.gaugeCanvas.width = current.metric_layout?.canvas_width || 260;
    card.gaugeCanvas.height = current.metric_layout?.canvas_height || 205;
    card.gauge = new window.Gauge(card.gaugeCanvas).setOptions(options);
    card.gauge.animationSpeed = 32;
  } else {
    card.gauge.setOptions(options);
  }
  card.gauge.maxValue = config.max;
  card.gauge.setMinValue(config.min);
  card.gauge.set(card.metric.gauge_value ?? config.min);
  card.gauge.render();
  const gauge = card.gauge;
  const pivotY = card.gaugeCanvas.height * gauge.paddingTop + gauge.availableHeight - (gauge.radius + gauge.lineWidth / 2) * gauge.extraPadding;
  if (Number.isFinite(pivotY)) card.gaugeCanvas.style.setProperty('--gauge-center-shift', `${(0.5 - pivotY / card.gaugeCanvas.height) * 100}%`);
  card.status.textContent = current.statistics_available === false ? '24-hour statistics unavailable.' : card.metric.gauge_value == null ? 'Waiting for a reading.' : '';
}

async function drawGraph(card) {
  if (card.pendingMode === card.mode) return;
  card.controller?.abort();
  const controller = new AbortController();
  card.controller = controller;
  const mode = card.mode;
  card.pendingMode = mode;
  card.status.textContent = 'Loading history…';
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const query = new URLSearchParams({sensor_id: current.sensor_id, metric1: card.metric.metric, range: mode === 'Graph6hr' ? '6h' : '24h'});
    if (card.metric.config.render === 'compass') { query.set('sensor_id2', current.sensor_id); query.set('metric2', 'Wind Speed'); }
    const response = await fetch(`/graph-data?${query}`, {signal: controller.signal});
    if (!response.ok) throw new Error('History could not be loaded. Select the tile to change view.');
    const payload = await response.json();
    if (card.mode !== mode || card.controller !== controller) return;
    const series = payload.series?.[`${current.sensor_id}::${card.metric.metric}`] || Object.values(payload.series || {})[0] || {};
    const config = card.metric.config;
    const points = (series.ts || []).map((timestamp, index) => ({x: Date.parse(timestamp), y: convertMetricValue(series.vals?.[index], config)})).filter(point => Number.isFinite(point.x));
    card.chart?.destroy();
    card.chart = null;
    if (config.render === 'compass') {
      card.graphCanvas.width = current.metric_layout?.canvas_width || 260;
      card.graphCanvas.height = current.metric_layout?.canvas_height || 205;
      card.graphCanvas.removeAttribute('aria-hidden');
      card.graphCanvas.style.color = '#17232b';
      const speed = payload.series?.[`${current.sensor_id}::Wind Speed`];
      if (renderWindRose(card.graphCanvas, series, speed, labels[mode], card.metric.value_config, windCharts)) {
        card.status.textContent = current.statistics_available === false ? '24-hour statistics unavailable.' : '';
        return;
      }
      card.graphCanvas.getContext('2d').clearRect(0, 0, card.graphCanvas.width, card.graphCanvas.height);
      card.status.textContent = 'No paired wind direction and speed readings in this period.';
      return;
    }
    if (!points.length) {
      card.status.textContent = 'No readings in this period.';
      return;
    }
    if (typeof window.Chart !== 'function') throw new Error('Graph library unavailable. Reload to try again.');
    const averageSeries = payload.simple_avg?.[`${current.sensor_id}::${card.metric.metric}`] || payload.rolling_ema?.[`${current.sensor_id}::${card.metric.metric}`];
    const average = averageSeries ? (averageSeries.ts || []).map((timestamp, index) => ({x: Date.parse(timestamp), y: convertMetricValue(averageSeries.vals?.[index], config)})).filter(point => Number.isFinite(point.x)) : [];
    const zones = (config.zones || []).map(zone => ({...zone, color: zone.strokeStyle || zone.color}));
    const lightMetric = ['Light Intensity', 'Auto Light', 'Visible Light Intensity', 'Estimated PPFD'].includes(card.metric.metric);
    const lineColor = lightMetric ? '#000000' : '#ffffff';
    const tickColor = '#17232b';
    const start = Date.now() - (mode === 'Graph6hr' ? 6 : 24) * 3600000;
    card.chart = new window.Chart(card.graphCanvas, {
      type: 'line',
      data: {datasets: [{label: `${card.metric.name} (${config.unit || ''})`, data: points, borderColor: lineColor, pointRadius: 0, borderWidth: 2, fill: false, spanGaps: false}, {label: 'Average', data: average, borderColor: lineColor, borderDash: [5, 4], pointRadius: 0, borderWidth: 2, fill: false}]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        parsing: false, normalized: true,
        plugins: {micrographZones: zones, decimation: {enabled: true, algorithm: 'min-max'}, legend: {display: false}, tooltip: {callbacks: {title: items => new Date(items[0].parsed.x).toLocaleString()}}},
        scales: {
          x: {type: 'linear', min: start, max: Date.now(), ticks: {maxTicksLimit: mode === 'Graph24hr' ? 9 : 6, color: tickColor, callback: value => { const date = new Date(value); return date.getHours() === 0 && date.getMinutes() === 0 ? date.toLocaleDateString([], {month: 'short', day: '2-digit'}) : date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }}},
          y: {ticks: {maxTicksLimit: 5, color: tickColor}}
        }
      }
    });
    card.status.textContent = current.statistics_available === false ? '24-hour statistics unavailable.' : '';
  } catch (error) {
    if (card.mode === mode && card.controller === controller) card.status.textContent = controller.signal.aborted ? 'History request timed out. Select the tile to change view.' : error.message;
  } finally {
    clearTimeout(timer);
    if (card.controller === controller) card.pendingMode = '';
  }
}

function render(card) {
  if (card.mode === 'Gauge') {
    card.controller?.abort();
    card.controller = null;
    card.pendingMode = '';
    drawGauge(card);
  } else {
    void drawGraph(card);
  }
}

const observer = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const card = cards.get(entry.target.dataset.metric);
    if (!card) continue;
    card.visible = entry.isIntersecting;
    if (card.visible) render(card);
  }
}, {rootMargin: '100px'});

function createCard(metric) {
  const node = element('article', 'metric-container');
  node.dataset.metric = metric.metric;
  node.tabIndex = 0;
  node.setAttribute('role', 'button');
  const title = element('div', 'metric-title');
  const gaugeView = element('div', 'gauge-view');
  const gaugeCanvas = element('canvas', '');
  gaugeCanvas.setAttribute('aria-hidden', 'true');
  gaugeView.append(gaugeCanvas);
  const graphView = element('div', 'graph-view');
  const graphCanvas = element('canvas', '');
  graphCanvas.setAttribute('aria-hidden', 'true');
  graphView.append(graphCanvas);
  const value = element('div', 'metric-current-value');
  const stats = element('div', 'metric-stats');
  const status = element('p', 'metric-status');
  status.setAttribute('role', 'status');
  node.append(title, gaugeView, graphView, value, stats, status);
  const card = {node, metric, title, value, gaugeView, gaugeCanvas, graphView, graphCanvas, stats, status, visible: false, mode: 'Gauge'};
  const cycle = () => {
    const mode = modes[(modes.indexOf(card.mode) + 1) % modes.length];
    try { localStorage.setItem(storageKey(metric.metric), mode); } catch (_) {}
    setMode(card, mode);
  };
  node.addEventListener('click', cycle);
  node.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); cycle(); }
  });
  let mode = modes.includes(metric.display_style) ? metric.display_style : 'Gauge';
  try { mode = localStorage.getItem(storageKey(metric.metric)) || mode; } catch (_) {}
  setMode(card, modes.includes(mode) ? mode : 'Gauge');
  return card;
}

function update(payload) {
  if (!grid) return;
  const previousLayout = JSON.stringify(current.metric_layout);
  const previousSensor = current.sensor_id;
  current = payload;
  if (previousSensor !== payload.sensor_id) {
    stationExpanded = false;
    try { stationExpanded = sessionStorage.getItem(expansionKey()) === 'true'; } catch (_) {}
  }
  const label = String(payload.sensor_id || '').toUpperCase().replace(/^(ECOWITT|ACURITE)-([A-Z0-9]+)$/, (_, prefix, id) => `${prefix}-${id.slice(-6)}`);
  if (stationLabel) stationLabel.textContent = label ? `${label} · ${payload.location || 'Weather Station'}` : 'Weather Station metrics';
  if (stationToggle) stationToggle.dataset.tileStyle = payload.metric_tile_style || 'Theme';
  const layout = payload.metric_layout || {};
  grid.style.setProperty('--container-width', layout.container_width || '260px');
  grid.style.setProperty('--canvas-width', String(layout.canvas_width || 260));
  grid.style.setProperty('--canvas-height', String(layout.canvas_height || 205));
  grid.style.setProperty('--stats-font', layout.stats_font || '.85rem');
  if (previousLayout !== JSON.stringify(payload.metric_layout)) {
    for (const card of cards.values()) {
      card.gauge?.stop?.();
      card.gauge = null;
    }
  }
  const metrics = payload.station_metrics || [];
  const active = new Set(metrics.map(metric => metric.metric));
  for (const [name, card] of cards) {
    if (active.has(name) && card.sensorId === payload.sensor_id) continue;
    observer.unobserve(card.node);
    card.controller?.abort();
    card.chart?.destroy();
    card.gauge?.stop?.();
    card.node.remove();
    cards.delete(name);
  }
  for (const metric of metrics) {
    let card = cards.get(metric.metric);
    if (!card) {
      card = createCard(metric);
      card.sensorId = payload.sensor_id;
      cards.set(metric.metric, card);
      grid.append(card.node);
      observer.observe(card.node);
    }
    card.metric = metric;
    card.node.dataset.tileStyle = payload.metric_tile_style || 'Theme';
    updateTitle(card);
    updateDetails(card);
    if (card.visible) render(card);
  }
  applyStationExpansion();
  document.getElementById('stationMetricsEmpty').hidden = metrics.length > 0;
  const signature = JSON.stringify([payload.sensor_id, metrics.map(metric => [metric.metric, metric.config.unit])]);
  if (metrics.length && signature !== rainSignature) {
    stopRain?.();
    rainSignature = signature;
    for (const name of Object.keys(rainConfigs)) delete rainConfigs[name];
    for (const metric of metrics) rainConfigs[metric.metric] = {...metric.config};
    stopRain = startRainGauges(rainConfigs, () => { for (const card of cards.values()) if (card.visible && card.mode === 'Gauge') drawGauge(card); });
  }
  if (!metrics.length) {
    stopRain?.();
    stopRain = null;
    rainSignature = '';
  }
}

window.addEventListener('caelus-readings', event => update(event.detail));
const bootstrap = document.getElementById('caelus-metrics-config');
if (bootstrap) update(JSON.parse(bootstrap.textContent));
window.addEventListener('pagehide', () => { for (const card of cards.values()) card.controller?.abort(); });
