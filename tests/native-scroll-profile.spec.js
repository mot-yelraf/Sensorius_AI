import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { parse } from 'acorn';

const script = readFileSync('ui_static/js/native_scroll_profile.js', 'utf8');

test('native diagnostic records real wheel input and restores its passive hooks', async ({ page }) => {
  const comments = [];
  parse(script, { ecmaVersion: 'latest', onComment: comments });
  expect(comments).toEqual([]);
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.goto('/');
  await page.evaluate(() => {
    window.nativeSamples = [];
    window.__sensNativeScrollDurationMs = 10000;
    window.pywebview = { api: { receive_native_scroll_sample: row => { window.nativeSamples.push(row); return true; } } };
    window.nativeOriginalHook = () => 'unchanged';
    window.nativePlugin = { id: 'micrographPointerPosition', beforeEvent: window.nativeOriginalHook };
    window.Chart = { instances: { native: { config: { plugins: [window.nativePlugin] } } } };
  });
  await page.evaluate(source => eval(source), script);
  expect(await page.evaluate(() => window.nativePlugin.beforeEvent())).toBe('unchanged');
  await page.mouse.move(10, 600);
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.nativeSamples.some(row => row.wheel_events > 0 && row.scroll_events > 0 && row.scroll_delta > 0))).toBe(true);
  await page.evaluate(() => {
    window.__sensNativeScroll.stop();
    window.nativeSamples = [];
    window.__sensNativeScrollDurationMs = 1200;
  });
  expect(await page.evaluate(() => window.nativePlugin.beforeEvent === window.nativeOriginalHook)).toBe(true);
  await page.evaluate(source => eval(source), script);
  await page.evaluate(() => { document.scrollingElement.scrollTop += 200; });
  await expect.poll(() => page.evaluate(() => window.nativeSamples.some(row => row.scroll_events > 0 && row.scroll_delta > 0 && row.wheel_events === 0))).toBe(true);
  await expect.poll(() => page.evaluate(() => window.nativePlugin.beforeEvent === window.nativeOriginalHook)).toBe(true);
});
