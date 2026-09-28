/* Official NWS event presentation, shared by the dashboard and Caelus. */
(() => {
  let snapshot = null;
  let busy = false;
  function render() {
    if (!snapshot) return;
    const now = Date.now();
    const events = snapshot.enabled ? (snapshot.events || []).filter(event => {
      const effective = Date.parse(event.effective);
      const expires = Date.parse(event.expires);
      const ends = event.ends ? Date.parse(event.ends) : expires;
      return effective <= now && now < Math.min(expires, ends);
    }) : [];
    const active = events.length > 0;
    const message = events.map(event => event.message).join('\n\n');
    const tile = document.getElementById('weatherForecastBox');
    tile?.classList.toggle('severe-weather-active', active);
    const button = tile?.querySelector('.forecast-open-btn');
    if (button) button.title = active ? 'Severe Weather — open Caelus for NWS event details' : 'Open Caelus Forecast';
    const panel = document.querySelector('.forecast-panel');
    panel?.classList.toggle('severe-weather-active', active);
    const synopsis = panel?.querySelector('.forecast-synopsis');
    if (synopsis) {
      if (!synopsis.hasAttribute('data-normal-synopsis')) synopsis.dataset.normalSynopsis = synopsis.textContent;
      synopsis.textContent = active
        ? message + (snapshot.stale ? '\nAlert updates temporarily unavailable; showing the last received warning.' : '')
        : synopsis.dataset.normalSynopsis;
      synopsis.setAttribute('aria-label', active ? 'Severe Weather' : 'Forecast synopsis');
    }
  }
  window.renderSevereWeather = render;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/weather-alerts', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('Weather alerts unavailable');
      snapshot = await response.json();
      const pending = snapshot.pending_alerts || [];
      document.querySelectorAll('[data-weather-alert-id]').forEach(el => {
        if (!pending.some(msg => msg.weather_alert_id === el.dataset.weatherAlertId)) el.remove();
      });
      for (const msg of pending) window.receiveWeatherAlert?.(msg);
    } catch (_) {
      if (snapshot) snapshot.stale = true;
    } finally {
      busy = false;
      render();
    }
  }
  refresh();
  window.setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
