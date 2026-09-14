// @ts-check
import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';

const profilerHelper = execFileSync(process.env.SENSORIUS_PYTHON || 'python3', [
  '-c', 'from testApparatus.profile_webui import build_js_helper; print(build_js_helper(1500))',
], { encoding: 'utf8' });

test.beforeEach(async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({
    contentType: 'application/javascript', body: '',
  }));
});

async function loadDashboard(page, response) {
  await page.route('**/*json_only=true*', route => route.fulfill({
    status: response.status,
    json: {
      available: [], values: { 'aht-pr-check': { Temperature: response.value } },
      timestamps: { 'aht-pr-check': String(response.value) }, stats: {},
    },
  }));
  await page.goto('/', { waitUntil: 'load' });
  await expect.poll(() => page.evaluate(() => window.__updateGaugesFinishedSeq)).toBeGreaterThan(0);
}

test('expanding metrics keeps the most recent live value', async ({ page }) => {
  await loadDashboard(page, { status: 200, value: 83.2 });
  const value = page.locator('#aht-pr-check_Temperature_val');
  await expect(value).toContainText('83.2');
  await page.locator('.sensor-collapse-toggle').click();
  await expect(value).toContainText('83.2');
});

test('failed updates show a notice and recover on the next successful response', async ({ page }) => {
  const response = { status: 503, value: 83.2 };
  await loadDashboard(page, response);
  const notice = page.locator('#dashboard_refresh_status');
  await expect(notice).toBeVisible();
  expect(await page.evaluate(() => window.__updateGaugesLastOk)).toBe(false);
  response.status = 200;
  await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
  await expect(notice).toBeHidden();
  await expect(page.locator('#aht-pr-check_Temperature_val')).toContainText('83.2');
});

test('a stalled JSON body times out and releases the refresh lock', async ({ page }) => {
  await loadDashboard(page, { status: 200, value: 83.2 });
  await page.evaluate(() => {
    window.__originalRefreshFetch = window.fetch;
    window.fetch = (url, options) => String(url).includes('json_only=true')
      ? Promise.resolve(new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{')); } })))
      : window.__originalRefreshFetch(url, options);
  });
  await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
  await expect(page.locator('#dashboard_refresh_status')).toBeVisible();
  expect(await page.evaluate(() => window.__updateGaugesInFlight)).toBe(false);
  expect(await page.evaluate(() => window.__updateGaugesLastError)).toContain('timed out');
  await page.evaluate(() => { window.fetch = window.__originalRefreshFetch; });
  await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
  await expect(page.locator('#dashboard_refresh_status')).toBeHidden();
});

test('profiler reports a failed refresh and measures a subsequent successful request', async ({ page }) => {
  const response = { status: 503, value: 83.2 };
  await loadDashboard(page, response);
  await page.evaluate(profilerHelper);
  const failed = await page.evaluate(() => window.__sensProfiler.profileDashboardRefresh());
  expect(failed.ok).toBe(false);
  expect(failed.error).toContain('503');
  response.status = 200;
  const recovered = await page.evaluate(() => window.__sensProfiler.profileDashboardRefresh());
  expect(recovered.ok).toBe(true);
  expect(recovered.finished_seq).toBeGreaterThan(failed.finished_seq);
  expect(recovered.resources.some(url => url.includes('json_only=true'))).toBe(true);
});

test('profiler does not call a skipped refresh successful', async ({ page }) => {
  await loadDashboard(page, { status: 200, value: 83.2 });
  await page.evaluate(profilerHelper);
  await page.evaluate(() => { window.updateGauges = async () => {}; });
  const error = await page.evaluate(async () => {
    try { await window.__sensProfiler.profileDashboardRefresh(); return ''; }
    catch (error) { return String(error); }
  });
  expect(error).toContain('dashboard refresh completion');
});
