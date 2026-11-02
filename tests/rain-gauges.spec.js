import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

const maxima = { day: 50.8, week: 127, month: 254, year: 1016 };

test('hundredth-inch rain increments remain visible in readings and statistics', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/api/rain-climate', route => route.fulfill({ json: { status: 'ready', maxima_mm: maxima } }));
  await page.goto('/rain-gauges?unit_system=Imperial&value=0.01', { waitUntil: 'domcontentloaded' });
  const card = page.locator('.metric-container[data-metric="Rain"]');
  await expect(card.locator('.metric-current-value')).toContainText('0.01 in');
  for (const stat of await card.locator('.metric-stats > div').all()) {
    await expect(stat).toContainText('0.01');
  }
});

test('a stationary zero rain gauge has no corner pointer after a range update', async ({ page }, testInfo) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({
    contentType: 'application/javascript',
    body: route.request().url().includes('gaugeJS') ? readFileSync('node_modules/gaugeJS/dist/gauge.min.js') : '',
  }));
  let release;
  const history = new Promise(resolve => { release = resolve; });
  await page.route('**/api/rain-climate', async route => {
    await history;
    await route.fulfill({ json: { status: 'ready', maxima_mm: maxima } });
  });
  await page.goto('/rain-gauges?value=0', { waitUntil: 'domcontentloaded' });
  const card = page.locator('.metric-container[data-metric="Rain"]');
  await expect.poll(() => page.evaluate(() => window['rain-pr-check_Rain_gauge'] instanceof window.Gauge)).toBe(true);
  await page.evaluate(() => { window.initialRainGauge = window['rain-pr-check_Rain_gauge']; });
  release();
  await expect(card).toHaveAttribute('data-rain-climate-status', 'ready');
  const result = await card.locator('.gauge-view canvas').evaluate(canvas => {
    const ctx = canvas.getContext('2d');
    const corner = ctx.getImageData(0, 0, 20, 20).data;
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let blackCornerPixels = 0;
    for (let i = 0; i < corner.length; i += 4) {
      if (corner[i + 3] > 0 && corner[i] < 20 && corner[i + 1] < 20 && corner[i + 2] < 20) blackCornerPixels++;
    }
    const gauge = window['rain-pr-check_Rain_gauge'];
    return {
      blackCornerPixels, painted: pixels.some((v, i) => i % 4 === 3 && v > 0),
      sameGauge: gauge === window.initialRainGauge, max: gauge.maxValue, value: gauge.gp[0].displayedValue,
    };
  });
  expect(result).toEqual({ blackCornerPixels: 0, painted: true, sameGauge: true, max: 50.8, value: 0 });
  await card.screenshot({ path: testInfo.outputPath('zero-rain-after-range-update.png') });
});

for (const units of ['Metric', 'Imperial']) {
  test(`rain gauges load historical limits and blue bands in ${units}`, async ({ page }, testInfo) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
    let calls = 0;
    await page.route('**/api/rain-climate', route => route.fulfill({ json: ++calls === 1
      ? { status: 'warming' }
      : { status: 'ready', maxima_mm: maxima } }));
    await page.goto(`/rain-gauges?unit_system=${units}`, { waitUntil: 'domcontentloaded' });
    const cards = page.locator('.metric-container');
    await expect(cards).toHaveCount(6);
    await expect(cards.first()).toHaveAttribute('data-rain-climate-status', 'ready', { timeout: 10000 });
    await expect(page.locator('.rain-climate-note')).toHaveCount(0);
    const scales = await cards.evaluateAll(nodes => nodes.map(node => ({max: Number(node.dataset.rainGaugeMax), period: node.dataset.rainGaugePeriod})));
    for (const config of scales) {
      const expected = maxima[config.period] / (units === 'Metric' ? 1 : 25.4);
      expect(config.max).toBeCloseTo(expected);
    }
    await expect(cards.nth(1).locator('.metric-current-value')).toContainText(units === 'Metric' ? '6.35 mm' : '0.25 in');
    await page.screenshot({ path: testInfo.outputPath(`rain-${units}.png`), fullPage: true });
  });
}

test('rain gauges retain defaults when climate history fails', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/api/rain-climate', route => route.fulfill({ status: 503, body: '' }));
  await page.goto('/rain-gauges', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.metric-container').first()).toHaveAttribute('data-rain-climate-status', 'unavailable');
  await expect(page.locator('.rain-climate-note')).toHaveCount(0);
  await expect(page.locator('.metric-container[data-metric="Rain Day"]')).toHaveAttribute('data-rain-gauge-max', '508');
  await expect(page.locator('.metric-current-value').nth(1)).toContainText('6.35 mm');
});

test('an existing GaugeJS instance receives new limits and labels', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.addInitScript(() => {
    window.rainTestGauges = [];
    window.Gauge = class {
      constructor(canvas) { this.canvas = canvas; window.rainTestGauges.push(this); }
      setOptions(options) { this.options = options; return this; }
      setMinValue(value) { this.minValue = value; }
      set(value) { this.value = value; }
      render() {}
    };
  });
  let calls = 0;
  await page.route('**/api/rain-climate', route => route.fulfill({ json: ++calls === 1
    ? { status: 'warming' } : { status: 'ready', maxima_mm: maxima } }));
  await page.goto('/rain-gauges', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.metric-container').first()).toHaveAttribute('data-rain-climate-status', 'ready', { timeout: 10000 });
  await expect(page.locator('.rain-climate-note')).toHaveCount(0);
  const gauges = await page.evaluate(() => window.rainTestGauges.map(g => ({ max: g.maxValue, labels: g.options.staticLabels.labels, colors: g.options.staticZones.map(z => z.strokeStyle), limited: g.options.limitMax })));
  expect(gauges).toHaveLength(6);
  expect(gauges.map(g => g.max)).toEqual([50.8, 50.8, 50.8, 127, 254, 1016]);
  for (const gauge of gauges) {
    expect(gauge.limited).toBe(true);
    expect(gauge.labels.at(-1)).toBe(gauge.max);
    expect(gauge.colors).toEqual(['#add8e6', '#66b2ff', '#0033cc']);
  }
});
