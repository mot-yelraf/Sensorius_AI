export function convertMetricValue(value, config = {}, rate = false) {
  if (value == null || value === '') return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  const factor = Number(config.display_factor ?? 1);
  const offset = rate ? 0 : Number(config.display_offset ?? 0);
  return numeric * (Number.isFinite(factor) ? factor : 1) + (Number.isFinite(offset) ? offset : 0);
}

export const gaugeZonesBackgroundMicro = {
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

export function renderWindRose(canvas, directionSeries, speedSeries, rangeLabel, speedConfig = {}, chartMap = new Map()) {
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
  const speedUnit = String(speedConfig.unit || speedConfig.source_unit || 'mph').trim();
  const convertSpeedBandLimit = value => Number.isFinite(value)
    ? Math.ceil(convertMetricValue(value, speedConfig) ?? value)
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
    const speed = convertMetricValue(rawSpeed, speedConfig);
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
