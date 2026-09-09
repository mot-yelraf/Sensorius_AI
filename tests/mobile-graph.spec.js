import { test, expect } from '@playwright/test';

async function graphFixture(page) {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/device-locations', route => route.fulfill({ json: [] }));
  await page.route('**/sensor-ids', route => route.fulfill({ json: ['aht-pr-check'] }));
  await page.route('**/sensor-metrics?*', route => route.fulfill({ json: ['Temperature', 'Rel-Humidity', 'Ambient VPD', 'Dew Point'] }));
  await page.route('**/graph-data?*', route => route.fulfill({ json: { no_data: true } }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
}

async function assertMobileGraph(page) {
  const geometry = await page.locator('#fullscreen_graph_container').evaluate(root => {
    const box = selector => root.querySelector(selector).getBoundingClientRect().toJSON();
    const controls = root.querySelector('.fullscreen-graph-controls');
    return {
      root: root.getBoundingClientRect().toJSON(), chart: box('#fullscreen_data_panel'),
      main: box('.fullscreen-graph-main'), controls: box('.fullscreen-graph-controls'),
      canvas: box('#fullscreen_graph'), time: box('.fullscreen-time-grid'),
      metrics: box('#fullscreenSensorOptions'), clipped: controls.scrollHeight > controls.clientHeight + 1,
      overflow: root.querySelector('.fullscreen-graph-body').scrollWidth > root.clientWidth + 1,
    };
  });
  expect(geometry.main.bottom).toBeLessThanOrEqual(geometry.controls.top + 1);
  expect(geometry.time.bottom).toBeLessThan(geometry.metrics.top);
  expect(geometry.chart.width).toBeGreaterThan(geometry.root.width - 24);
  expect(geometry.canvas.bottom).toBeLessThanOrEqual(geometry.chart.bottom);
  expect(geometry.chart.height).toBeGreaterThanOrEqual(240);
  expect(geometry.clipped).toBe(false);
  expect(geometry.overflow).toBe(false);
  const labels = page.locator('.fullscreen-time-grid label');
  await expect(labels).toHaveCount(12);
  for (const label of await labels.all()) {
    await label.tap();
    await expect(label.locator('input')).toBeChecked();
  }
  await expect(page.locator('#start_time')).toBeVisible();
  const metric = page.locator('.fullscreen-metric-checkbox').last();
  await metric.check();
  await expect(metric).toBeChecked();
  await metric.uncheck();
}

for (const viewport of [{ width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`phone Graphum shows chart then every selector at ${viewport.width}x${viewport.height}`, async ({ browser, baseURL }, testInfo) => {
    const page = await browser.newPage({ baseURL, viewport, isMobile: true, hasTouch: true });
    try {
      await graphFixture(page);
      const dashboardWidth = await page.evaluate(() => innerWidth);
      await page.getByRole('link', { name: 'Full Screen Graphs' }).tap();
      await expect(page.locator('.fullscreen-metric-checkbox')).toHaveCount(4);
      await assertMobileGraph(page);
      await page.locator('.fullscreen-time-grid label').filter({ hasText: /^24Hr$/ }).tap();
      await page.locator('.fullscreen-graph-body').evaluate(element => { element.scrollTop = 0; });
      await page.screenshot({ path: testInfo.outputPath('mobile-graph.png') });
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      await assertMobileGraph(page);
      await page.getByRole('button', { name: 'Return to dashboard' }).tap();
      await page.setViewportSize(viewport);
      await expect.poll(() => page.evaluate(() => innerWidth)).toBe(dashboardWidth);
    } finally {
      await page.close();
    }
  });
}

test('desktop Graphum retains its left selectors and right chart', async ({ page }) => {
  await graphFixture(page);
  await page.getByRole('link', { name: 'Full Screen Graphs' }).click();
  await expect(page.locator('.fullscreen-metric-checkbox')).toHaveCount(4);
  const controls = await page.locator('.fullscreen-graph-controls').boundingBox();
  const graph = await page.locator('.fullscreen-graph-main').boundingBox();
  expect(controls.x + controls.width).toBeLessThanOrEqual(graph.x);
  expect(controls.y).toBe(graph.y);
});
