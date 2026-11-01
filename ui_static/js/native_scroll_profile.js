(() => {
  window.__sensNativeScroll?.stop();
  const gaps = [];
  const hooks = new Map();
  const counters = { wheel_events: 0, wheel_delta_y: 0, scroll_events: 0, pointer_moves: 0, hover_calls: 0, hover_ms: 0 };
  let active = true;
  let previous = null;
  let previousTop = (document.scrollingElement || document.documentElement).scrollTop;
  let raf = 0;
  let sending = false;
  const duration = Math.max(1000, Math.min(600000, Number(window.__sensNativeScrollDurationMs) || 120000));
  const onWheel = event => { counters.wheel_events++; counters.wheel_delta_y += Number(event.deltaY) || 0; };
  const onScroll = () => { counters.scroll_events++; };
  const onMove = () => { counters.pointer_moves++; };
  const tick = now => {
    if (!active) return;
    if (previous !== null && gaps.length < 600) gaps.push(now - previous);
    previous = now;
    raf = requestAnimationFrame(tick);
  };
  const syncHooks = () => {
    for (const chart of Object.values(window.Chart?.instances || {})) {
      for (const plugin of chart.config?.plugins || []) {
        if (plugin.id !== 'micrographPointerPosition' || hooks.has(plugin)) continue;
        const original = plugin.beforeEvent;
        if (typeof original !== 'function') continue;
        const wrapper = function(...args) {
          const started = performance.now();
          counters.hover_calls++;
          try { return original.apply(this, args); }
          finally { counters.hover_ms += performance.now() - started; }
        };
        hooks.set(plugin, { original, wrapper });
        plugin.beforeEvent = wrapper;
      }
    }
  };
  const percentile = (ordered, fraction) => ordered.length ? ordered[Math.max(0, Math.ceil(ordered.length * fraction) - 1)] : null;
  const state = {
    sample() {
      syncHooks();
      const root = document.scrollingElement || document.documentElement;
      const ordered = gaps.splice(0).sort((a, b) => a - b);
      const row = {
        browser_ms: performance.now(), visibility: document.visibilityState, focused: document.hasFocus(),
        scroll_top: root.scrollTop, scroll_delta: root.scrollTop - previousTop,
        document_height: root.scrollHeight, viewport_width: innerWidth, viewport_height: innerHeight,
        frame_count: ordered.length, raf_p50_ms: percentile(ordered, .5), raf_p95_ms: percentile(ordered, .95),
        raf_max_ms: ordered.length ? ordered[ordered.length - 1] : null,
        frames_over_50ms: ordered.filter(value => value > 50).length,
        pending_micrographs: document.querySelectorAll('[data-micrograph-loading="1"]').length,
        chart_count: Object.keys(window.Chart?.instances || {}).length,
        ...counters,
      };
      previousTop = root.scrollTop;
      for (const key of Object.keys(counters)) counters[key] = 0;
      return row;
    },
    stop() {
      if (!active) return;
      active = false;
      cancelAnimationFrame(raf);
      clearInterval(interval);
      clearTimeout(deadline);
      window.removeEventListener('wheel', onWheel, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('mousemove', onMove, true);
      for (const [plugin, { original, wrapper }] of hooks) {
        if (plugin.beforeEvent === wrapper) plugin.beforeEvent = original;
      }
      hooks.clear();
    },
  };
  window.__sensNativeScroll = state;
  window.addEventListener('wheel', onWheel, { passive: true, capture: true });
  window.addEventListener('scroll', onScroll, { passive: true, capture: true });
  window.addEventListener('mousemove', onMove, { passive: true, capture: true });
  syncHooks();
  raf = requestAnimationFrame(tick);
  const interval = setInterval(() => {
    const receiver = window.pywebview?.api?.receive_native_scroll_sample;
    if (receiver && !sending) {
      sending = true;
      Promise.resolve(receiver(state.sample())).then(accepted => {
        if (accepted === false) state.stop();
      }).catch(() => {}).finally(() => { sending = false; });
    }
  }, 1000);
  const deadline = setTimeout(() => state.stop(), duration);
  return true;
})()
