/* Fixed-window switch transition plots backed by persisted and live events.
 * Canvas keeps dense 24-hour histories inexpensive on Raspberry Pi hubs.
 */
(() => {
  'use strict';
  const models = new WeakMap();
  const periods = [24, 6, 1];

  function originLabel(source) {
    const text = String(source || '').trim();
    if (/^auto\s*-\s*/i.test(text)) return text.replace(/^auto\s*-\s*/i, 'auto - ');
    if (/mqtt-auto:|auto\/rule:/i.test(text)) {
      const rule = text.split(':').slice(1).join(':').replace(/\/mqtt$/i, '').trim();
      return rule ? `auto - ${rule}` : 'auto';
    }
    if (/manual|^ui$|\/ui|ui\//i.test(text)) return 'manual';
    if (/auto|rule|timer|schedule/i.test(text)) return 'auto';
    return '';
  }

  function parseEvent(node) {
    const raw = node.dataset.rawEvent || '';
    const match = raw.match(/^(On|Off)\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?/i);
    if (!match) return null;
    const at = Date.parse(`${match[2]}T${match[3]}${match[4] || ''}`);
    if (!Number.isFinite(at)) return null;
    return { at, on: match[1].toLowerCase() === 'on', origin: originLabel((raw.match(/\(([^)]+)\)\s*$/) || [])[1]) };
  }

  function timestamp(at) {
    return new Date(at).toLocaleString([], {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true,
    });
  }

  function eventText(event) {
    return `${event.on ? 'On' : 'Off'}: ${timestamp(event.at)} · ${event.origin || 'origin unavailable'}`;
  }

  function paint(model) {
    const { canvas, plot, list } = model;
    const now = Date.now();
    const start = now - model.hours * 3600000;
    const unique = new Map();
    const recent = Array.from(list.children).map(parseEvent).filter(Boolean);
    for (const event of [...(model.latest ? [model.latest] : []), ...model.history, ...recent]) {
      if (event.at > now) continue;
      const key = `${Math.floor(event.at / 1000)}|${event.on}`;
      const previous = unique.get(key);
      if (!previous || (!previous.origin && event.origin)) unique.set(key, event);
    }
    const known = Array.from(unique.values()).sort((a, b) => a.at - b.at);
    model.latest = known.at(-1) || null;
    model.events = known.filter(event => event.at >= start);
    const width = plot.clientWidth;
    const height = plot.clientHeight;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);
    ctx.strokeStyle = '#adb4a6';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 2; i++) {
      const x = .5 + i * Math.max(0, width - 1) / 2;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke();
    }
    ctx.lineWidth = 2;
    for (const event of model.events) {
      const x = 1 + (event.at - start) / (now - start) * Math.max(0, width - 2);
      ctx.strokeStyle = event.on ? '#28a745' : '#000000';
      ctx.beginPath(); ctx.moveTo(x, 1); ctx.lineTo(x, height - 1); ctx.stroke();
    }
    const next = periods[(periods.indexOf(model.hours) + 1) % periods.length];
    const label = `${model.hours} hour switch history; ${model.events.length} transitions; green On, black Off; click for ${next} hours`;
    plot.setAttribute('aria-label', label);
    plot.title = `${model.hours}hr · Click for ${next}hr. Hover for event times; arrow keys inspect events.`;
    plot.dataset.hours = String(model.hours);
    plot.dataset.eventCount = String(model.events.length);
    model.panel.querySelector('.switch-history-start').textContent = `${model.hours}hr`;
    model.panel.querySelector('.switch-history-start').title = timestamp(start);
    const ticks = model.panel.querySelector('.switch-history-ticks');
    ticks.replaceChildren();
    for (const at of [start, (start + now) / 2, now]) {
      const tick = document.createElement('span');
      const date = document.createElement('span');
      const time = document.createElement('span');
      const tickDate = new Date(at);
      date.textContent = tickDate.toLocaleDateString('en-US', { month: 'short' }) + String(tickDate.getDate()).padStart(2, '0');
      time.textContent = new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', hour12: true });
      tick.title = timestamp(at);
      tick.append(date, time);
      ticks.appendChild(tick);
    }
    model.detail.textContent = model.latest ? eventText(model.latest)
      : model.error ? 'History refresh failed; no recorded state available' : 'No recorded state';
    model.detail.title = model.error ? 'History refresh failed; last recorded state shown' : '';
  }

  async function load(model) {
    model.loading = true;
    model.loadedAt = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      // Fetch one full day; switching shorter windows only filters local data.
      const params = new URLSearchParams({ switch_key: model.list.dataset.switchKey, hours: '24' });
      const response = await fetch(`/switch/history?${params}`, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.events)) throw new Error('Invalid switch history');
      model.history = data.events.map(event => ({ at: Number(event.epoch) * 1000, on: event.state === true, origin: originLabel(event.source) }))
        .filter(event => Number.isFinite(event.at));
      model.error = false;
    } catch (_) {
      model.error = true;
    } finally {
      clearTimeout(timeout);
      model.loading = false;
      paint(model);
    }
  }

  /** Render every transition in the selected 24, 6, or 1 hour window. */
  function render(list) {
    const panel = list.closest('.switch-events');
    if (!panel) return;
    let model = models.get(list);
    if (!model) {
      const plot = panel.querySelector('.switch-history-plot');
      model = { list, panel, plot, canvas: plot.querySelector('canvas'), detail: panel.querySelector('.switch-history-detail'),
        hours: 24, history: [], events: [], loadedAt: null, loading: false, error: false, selectedAt: null };
      models.set(list, model);
      const cycle = () => {
        model.hours = periods[(periods.indexOf(model.hours) + 1) % periods.length];
        model.selectedAt = null;
        paint(model);
      };
      panel.onclick = cycle;
      plot.onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); cycle(); }
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault();
          const index = model.events.findIndex(item => item.at === model.selectedAt);
          const next = Math.max(0, Math.min(model.events.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
          const selected = model.events[next];
          model.selectedAt = selected?.at;
          if (selected) plot.title = eventText(selected);
        }
      };
      plot.onmousemove = event => {
        if (!model.events.length) return;
        const bounds = plot.getBoundingClientRect();
        const target = Date.now() - model.hours * 3600000 * (1 - (event.clientX - bounds.left) / bounds.width);
        const nearest = model.events.reduce((a, b) => Math.abs(a.at - target) <= Math.abs(b.at - target) ? a : b);
        model.selectedAt = nearest.at;
        plot.title = eventText(nearest);
      };
    }
    paint(model);
    if (!model.loading && (model.loadedAt === null || Date.now() - model.loadedAt >= 60000)) load(model);
  }

  window.SwitchHistory = { render };
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('.switch-events-list').forEach(render));
  setInterval(() => {
    if (!document.hidden) document.querySelectorAll('.switch-events-list').forEach(render);
  }, 60000);
  window.addEventListener('resize', () => document.querySelectorAll('.switch-events-list').forEach(render));
})();
