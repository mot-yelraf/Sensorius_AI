(async () => {
  const root = document.scrollingElement || document.documentElement;
  const originalTop = root.scrollTop;
  const originalBehavior = root.style.scrollBehavior;
  const readinessStarted = performance.now();
  let quietSince = null;
  let pending = 0;
  while (performance.now() - readinessStarted < 15000) {
    pending = document.querySelectorAll('[data-micrograph-loading="1"]').length + (window.__micrographDataInflight?.size || 0);
    if (pending) quietSince = null;
    else if (quietSince === null) quietSince = performance.now();
    if (quietSince !== null && performance.now() - quietSince >= 750 && performance.now() - readinessStarted >= 1500) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const charts = Object.values(window.Chart?.instances || {}).filter(chart => chart.canvas?.classList.contains('micrograph-canvas'));
  const events = charts.map(chart => Array.from(chart.options.events || []));
  const plugins = [...new Set(charts.flatMap(chart => chart.config.plugins || []))].filter(plugin =>
    ['micrographPointerPosition', 'micrographEdgeHover'].includes(plugin.id));
  const hooks = plugins.map(plugin => plugin.beforeEvent);
  let hoverCalls = 0;
  plugins.forEach((plugin, index) => {
    plugin.beforeEvent = function(...args) {
      hoverCalls++;
      return hooks[index].apply(this, args);
    };
  });
  const duration = Math.max(500, Math.min(10000, Number(window.__sensScrollDurationMs) || 3000));
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const percentile = (values, fraction) => {
    const sorted = values.slice().sort((a, b) => a - b);
    return sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] : null;
  };
  const longTaskSupported = !!window.PerformanceObserver?.supportedEntryTypes?.includes('longtask');
  const metadata = {
    user_agent: navigator.userAgent,
    visibility_state: document.visibilityState,
    has_focus: document.hasFocus(),
    pointer_motion: !!window.__sensScrollPointerMove,
    viewport: { width: innerWidth, height: innerHeight, device_pixel_ratio: devicePixelRatio },
    document_height: root.scrollHeight,
    metric_count: document.querySelectorAll('.metric-container').length,
    chart_count: charts.length,
    chart_points: charts.reduce((sum, chart) => sum + (chart.data.datasets[0]?.data.length || 0), 0),
    hover_plugin_ids: plugins.map(plugin => plugin.id),
    asset_version: Array.from(document.scripts).map(script => script.src + ' ' + script.textContent).join(' ').match(/dashboard_runtime\.js\?v=([^'"\s]+)/)?.[1] || '',
    background_attachment: getComputedStyle(document.body).backgroundAttachment,
    long_task_supported: longTaskSupported,
    readiness_wait_ms: performance.now() - readinessStarted,
    pending_micrographs: pending,
    input: 'requestAnimationFrame scrollTo sweep; not a wheel/compositor FPS measurement',
  };
  const passes = [];
  const order = Number(window.__sensProfilerSampleIndex || 1) % 2
    ? ['normal', 'hover_disabled', 'normal_repeat']
    : ['hover_disabled', 'normal', 'normal_repeat'];
  try {
    if (pending) return { ok: false, skipped: false, error: 'Micrographs did not finish loading before the scroll benchmark', metadata };
    if (document.visibilityState !== 'visible') return { ok: false, skipped: true, error: 'Profiling page is not visible', metadata };
    root.style.scrollBehavior = 'auto';
    for (const mode of order) {
      charts.forEach((chart, index) => {
        chart.options.events = mode === 'hover_disabled' ? [] : events[index];
        chart.update('none');
      });
      root.scrollTop = 0;
      await frame();
      await frame();
      const distance = Math.min(2400, Math.max(0, root.scrollHeight - innerHeight));
      if (!distance) return { ok: false, skipped: true, error: 'Dashboard is not vertically scrollable', metadata };
      const gaps = [];
      const longTasks = [];
      let observer = null;
      if (longTaskSupported) {
        observer = new PerformanceObserver(list => longTasks.push(...list.getEntries().map(entry => entry.duration)));
        observer.observe({ type: 'longtask' });
      }
      const initialCalls = hoverCalls;
      let previous = await frame();
      const started = previous;
      let maxTop = 0;
      while (previous - started < duration) {
        const now = await frame();
        gaps.push(now - previous);
        previous = now;
        const fraction = Math.min(1, (now - started) / duration);
        root.scrollTop = Math.round(distance * (fraction <= .5 ? fraction * 2 : (1 - fraction) * 2));
        if (window.__sensScrollPointerMove) {
          const x = innerWidth * .3 + gaps.length % 2;
          const y = innerHeight * .65;
          const hit = document.elementFromPoint(x, y);
          if (hit?.classList.contains('micrograph-canvas')) {
            hit.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, view: window, clientX: x, clientY: y }));
          }
        }
        maxTop = Math.max(maxTop, root.scrollTop);
      }
      await frame();
      if (observer) {
        longTasks.push(...observer.takeRecords().map(entry => entry.duration));
        observer.disconnect();
      }
      passes.push({
        mode, total_ms: previous - started, scroll_distance_px: maxTop,
        frame_count: gaps.length, frame_p50_ms: percentile(gaps, .5),
        frame_p95_ms: percentile(gaps, .95), frame_max_ms: Math.max(...gaps),
        frames_over_50ms: gaps.filter(gap => gap > 50).length,
        long_task_count: longTaskSupported ? longTasks.length : null,
        long_task_total_ms: longTaskSupported ? longTasks.reduce((sum, value) => sum + value, 0) : null,
        hover_plugin_calls: hoverCalls - initialCalls,
        visibility_state: document.visibilityState, has_focus: document.hasFocus(),
      });
    }
    return { ok: passes.every(pass => pass.scroll_distance_px > 0), metadata, passes };
  } finally {
    plugins.forEach((plugin, index) => { plugin.beforeEvent = hooks[index]; });
    charts.forEach((chart, index) => { chart.options.events = events[index]; chart.update('none'); });
    root.style.scrollBehavior = originalBehavior;
    root.scrollTop = originalTop;
  }
})()
