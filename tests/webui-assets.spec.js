import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { parse } from 'acorn';

function checkScript(source, sourceType = 'script') {
  const comments = [];
  parse(source, { ecmaVersion: 'latest', sourceType, onComment: comments });
  expect(comments).toEqual([]);
}

test('rendered scripts and extracted application assets contain no JavaScript comments', async ({ request }) => {
  const response = await request.get('/');
  expect(response.ok()).toBeTruthy();
  const html = await response.text();
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const attrs = match[1];
    if (attrs.includes('application/json')) {
      expect(() => JSON.parse(match[2])).not.toThrow();
    } else if (!/\bsrc\s*=/.test(attrs)) {
      checkScript(match[2], attrs.includes('module') ? 'module' : 'script');
    }
  }
  for (const name of ['dashboard_runtime.js', 'graph_workspace.js']) {
    const asset = await request.get(`/ui_static/js/${name}`);
    expect(asset.ok()).toBeTruthy();
    checkScript(await asset.text(), name === 'dashboard_runtime.js' ? 'module' : 'script');
  }
  const comments = [];
  parse('const x = 1; // forbidden\n/* forbidden */ const y = 2;', { ecmaVersion: 'latest', onComment: comments });
  expect(comments.map(item => item.type)).toEqual(['Line', 'Block']);
});

for (const width of [1280, 390]) {
  test(`real Gauge and Chart libraries draw metric cards at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const libraries = [
      ['gaugeJS', 'gaugeJS/dist/gauge.min.js'],
      ['chart.js@', 'chart.js/dist/chart.umd.js'],
      ['chartjs-adapter', 'chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js'],
      ['chartjs-plugin-annotation', 'chartjs-plugin-annotation/dist/chartjs-plugin-annotation.min.js'],
    ];
    await page.route('https://cdn.jsdelivr.net/**', route => {
      const entry = libraries.find(([key]) => route.request().url().includes(key));
      if (!entry) throw new Error('Unexpected library request');
      return route.fulfill({ contentType: 'application/javascript', body: readFileSync(`node_modules/${entry[1]}`) });
    });
    let value = 72.4;
    await page.route('**/*json_only=true*', route => route.fulfill({ json: {
      available: ['aht-pr-check'], values: { 'aht-pr-check': { Temperature: value } },
      timestamps: { 'aht-pr-check': String(value) }, stats: {},
    } }));
    await page.route('**/graph-data?*', route => {
      const query = new URL(route.request().url()).searchParams;
      return route.fulfill({ json: { series: {
        [`${query.get('sensor_id')}::${query.get('metric1')}`]: {
          ts: ['2026-10-26T10:00:00', '2026-10-26T11:00:00', '2026-10-26T12:00:00'], vals: [68, 74, 71],
        },
      } } });
    });
    await page.route('**/device-locations', route => route.fulfill({ json: [] }));
    await page.route('**/sensor-ids', route => route.fulfill({ json: ['aht-pr-check'] }));
    await page.route('**/sensor-metrics?*', route => route.fulfill({ json: ['Temperature'] }));
    await page.route('**/graph-setups', route => route.fulfill({ json: { setups: [] } }));
    await page.goto('/', { waitUntil: 'load' });
    const card = page.locator('#aht-pr-check_Temperature_container');
    await card.scrollIntoViewIfNeeded();
    await expect.poll(() => page.evaluate(() => window['aht-pr-check_Temperature_gauge'] instanceof window.Gauge)).toBe(true);
    const painted = canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      return pixels.some((v, i) => i % 4 === 3 && v > 0);
    };
    await expect.poll(() => card.locator('.gauge-view canvas').evaluate(painted)).toBe(true);
    await card.locator('.gauge-view').click();
    await expect.poll(() => card.locator('.micrograph-canvas').evaluate(canvas => {
      const chart = window.Chart.getChart(canvas);
      return chart?.data.datasets[0].data.length || 0;
    })).toBe(3);
    await expect.poll(() => card.locator('.micrograph-canvas').evaluate(painted)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('metric-cards.png') });
    value = 81.2;
    await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
    await expect(card.locator('[id$="_val"]')).toContainText('81.2');
    await page.getByRole('link', { name: 'Full Screen Graphs' }).click();
    await expect(page.locator('.fullscreen-metric-checkbox')).toHaveCount(1);
    await page.locator('.fullscreen-metric-checkbox').check();
    await expect.poll(() => page.evaluate(() => window.graphChart?.data.datasets[0]?.data.length || 0)).toBe(3);
    await expect.poll(() => page.locator('#fullscreen_graph').evaluate(painted)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath('graph-workspace.png') });
    await page.getByRole('button', { name: 'Return to dashboard' }).click();
    await expect(card).toBeVisible();
    expect(errors).toEqual([]);
  });
}
