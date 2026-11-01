import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

const probe = readFileSync('testApparatus/webui_scroll_probe.js', 'utf8');

test('scroll profiler traverses the dashboard and restores chart events and scroll position', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.goto('/');
  const result = await page.evaluate(async source => {
    window.__sensScrollDurationMs = 500;
    window.__sensScrollPointerMove = true;
    const calls = [];
    const plugin = { id: 'micrographPointerPosition', beforeEvent() {} };
    const chart = {
      canvas: document.createElement('canvas'), options: { events: ['mousemove', 'mouseout'] },
      config: { plugins: [plugin] }, data: { datasets: [{ data: [1, 2, 3] }] },
      update() { calls.push(this.options.events.slice()); },
    };
    chart.canvas.classList.add('micrograph-canvas');
    chart.canvas.style.cssText = 'position:fixed;left:20%;top:50%;width:30%;height:30%;z-index:99999';
    document.body.append(chart.canvas);
    chart.canvas.addEventListener('mousemove', () => {
      if (chart.options.events.includes('mousemove')) plugin.beforeEvent();
    });
    window.Chart = { instances: { one: chart } };
    const hook = plugin.beforeEvent;
    const before = document.scrollingElement.scrollTop;
    const sample = await eval(source);
    return {
      sample, before, after: document.scrollingElement.scrollTop,
      events: chart.options.events, hookRestored: plugin.beforeEvent === hook,
      disabled: calls.some(events => events.length === 0),
    };
  }, probe);
  expect(result.sample.ok).toBe(true);
  expect(result.sample.passes).toHaveLength(3);
  for (const pass of result.sample.passes) {
    expect(pass.scroll_distance_px).toBeGreaterThan(0);
    expect(pass.frame_count).toBeGreaterThan(0);
  }
  expect(result.after).toBe(result.before);
  expect(result.events).toEqual(['mousemove', 'mouseout']);
  expect(result.hookRestored).toBe(true);
  expect(result.disabled).toBe(true);
  expect(result.sample.passes.find(pass => pass.mode === 'normal').hover_plugin_calls).toBeGreaterThan(0);
  expect(result.sample.passes.find(pass => pass.mode === 'hover_disabled').hover_plugin_calls).toBe(0);
});
