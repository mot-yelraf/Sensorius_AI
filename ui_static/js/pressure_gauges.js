/** Display-only pressure ranges; stored measurements retain their original values. */
export function pressureGaugeConfig(metric, config, altitude, context = {}) {
  if (!config || !(metric === 'Pressure' || metric === 'Baro-Pressure' || metric.endsWith(' Baro-Pressure'))) return config;
  const device = String(context.device || '').trim().toLowerCase();
  const calibrationAltitude = Number(context.altitude);
  const corrected = !metric.includes('Absolute Baro-Pressure') && (
    metric.startsWith('Gateway ') || context.weewx || device.includes('ecowitt') || device.includes('weewx') ||
    (['aqi', 'bme680', 'bme688', 'vpd', 'avpd', 'bme280', 'apvpd'].includes(device) &&
      Number.isFinite(calibrationAltitude) && calibrationAltitude !== 0)
  );
  let center = 1013.25;
  if (!corrected) {
    if (altitude == null || String(altitude).trim() === '') return config;
    const meters = Number(altitude);
    if (!Number.isFinite(meters) || meters < -500 || meters > 10000) return config;
    center *= Math.pow(1 - meters / 44330, 5.255);
  }
  const imperial = config.unit === 'inHg';
  const factor = imperial ? 0.0295299830714 : 1;
  const rounding = imperial ? 0.1 : 10;
  const step = imperial ? 0.5 : 20;
  const clean = value => Number(value.toFixed(6));
  const min = clean(Math.floor((center - 50) * factor / rounding) * rounding);
  const max = clean(Math.ceil((center + 50) * factor / rounding) * rounding);
  const ticks = [];
  for (let tick = min; tick < max; tick = clean(tick + step)) {
    if (tick === min || clean(max - tick) >= step) ticks.push(tick);
  }
  ticks.push(max);
  return { ...config, min, max, ticks, zones: [{ min, max, strokeStyle: '#add8e6' }] };
}
