/* Location-based rain limits are loaded independently of sensor readings. */
"use strict";
export function startRainGauges(gaugeConfig, initGauge) {
  const defaults = new Map(Object.entries(gaugeConfig)
    .filter(([, config]) => config.rain_period)
    .map(([metric, config]) => [metric, JSON.parse(JSON.stringify(config))]));
  let timer;
  let stopped = false;

  function apply(payload) {
    let changed = false;
    for (const [metric, original] of defaults) {
      const config = gaugeConfig[metric];
      const maximumMM = payload.maxima_mm?.[config.rain_period];
      const ready = payload.status === "ready" && Number.isFinite(maximumMM) && maximumMM >= 0;
      // Keep a usable axis even where the baseline contains no rain.
      const maximum = ready ? Math.round(Math.max(1, maximumMM) / 25.4 * (config.display_factor || 1) * 1e6) / 1e6 : original.max;
      if (config.max !== maximum) {
        changed = true;
        config.min = 0;
        config.max = maximum;
        config.ticks = ready ? [0, .2, .4, .6, .8, 1].map(f => f * maximum) : original.ticks;
        config.zones = ready ? [
          { strokeStyle: "#add8e6", min: 0, max: maximum * .2 },
          { strokeStyle: "#66b2ff", min: maximum * .2, max: maximum * .6 },
          { strokeStyle: "#0033cc", min: maximum * .6, max: maximum },
        ] : original.zones;
      }
      for (const container of document.querySelectorAll('.metric-container')) {
        if (container.dataset.metric !== metric) continue;
        container.dataset.rainGaugeMax = String(maximum);
        container.dataset.rainGaugePeriod = config.rain_period;
        container.dataset.rainClimateStatus = ready ? 'ready' : payload.status;
      }
    }
    if (changed && typeof initGauge === 'function') initGauge();
  }

  async function refresh() {
    if (stopped) return;
    const hasRain = [...document.querySelectorAll('.metric-container')].some(node => defaults.has(node.dataset.metric));
    let delay = 300000;
    if (hasRain) {
      try {
        const response = await fetch('/api/rain-climate', { signal: AbortSignal.timeout(10000), cache: 'no-store' });
        if (!response.ok) throw new Error('Rain climate request failed');
        const payload = await response.json();
        apply(payload);
        if (payload.status === 'warming') delay = 3000;
      } catch (_) {
        apply({ status: 'unavailable' });
      }
    }
    if (!stopped) timer = setTimeout(refresh, delay);
  }
  addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); });
  addEventListener('pageshow', event => { if (event.persisted) { stopped = false; refresh(); } });
  apply({ status: 'warming' });
  refresh();
}
