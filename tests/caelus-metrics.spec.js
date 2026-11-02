import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

for (const finish of ['Theme', 'Chrome', 'Brass', 'Mahogany & Brass']) {
  test(`Caelus ${finish} metrics draw, expand, preserve units and cycle views`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const libraries = [
      ['gaugeJS', 'gaugeJS/dist/gauge.min.js'],
      ['chart.js', 'chart.js/dist/chart.umd.js'],
    ];
    await page.route('https://cdn.jsdelivr.net/**', route => {
      const library = libraries.find(([key]) => route.request().url().includes(key));
      if (!library) return route.abort();
      return route.fulfill({contentType: 'application/javascript', body: readFileSync(`node_modules/${library[1]}`)});
    });
    await page.route('**/embed.windy.com/**', route => route.fulfill({body: ''}));
    await page.route('**/api/weather-forecast-app/current-readings', route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/weather-forecast-app/historical-averages**', route => route.fulfill({json: {status: 'unavailable'}}));
    await page.route('**/api/rain-climate', route => route.fulfill({json: {status: 'unavailable'}}));
    await page.route('**/graph-data?*', route => {
      const query = new URL(route.request().url()).searchParams;
      const sensor = query.get('sensor_id');
      const metric = query.get('metric1');
      const hours = query.get('range') === '6h' ? 6 : 24;
      const ts = Array.from({length: 25}, (_, index) => new Date(Date.now() - (24 - index) * hours * 3600000 / 24).toISOString());
      const key = `${sensor}::${metric}`;
      const series = {[key]: {ts, vals: ts.map((_, index) => metric === 'Wind Direction' ? index * 15 : 15 + index / 4)}};
      if (metric === 'Wind Direction') series[`${sensor}::Wind Speed`] = {ts, vals: ts.map(() => 6)};
      return route.fulfill({json: {series, simple_avg: {[key]: {ts, vals: ts.map(() => 18)}}}});
    });
    const path = `/weather-forecast?metrics=true&tile_style=${encodeURIComponent(finish)}`;
    await page.goto(path);
    const toggle = page.locator('#stationMetricsToggle');
    const cards = page.locator('#stationMetricGrid .metric-container');
    await toggle.scrollIntoViewIfNeeded();
    await expect(cards).toHaveCount(10);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toHaveAttribute('data-tile-style', finish);
    const first = cards.first();
    await expect(first).toHaveCSS('width', '260px');
    await expect(first.locator('.metric-current-value')).toContainText('20 °C');
    await expect(first.locator('.metric-stats')).toContainText('Min');
    await expect(first.locator('.metric-stats')).toContainText('2026/09/09');
    await expect(first.locator('.metric-trend')).toHaveCount(1);
    const painted = canvas => canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some((value, index) => index % 4 === 3 && value > 0);
    await expect.poll(() => first.locator('.gauge-view canvas').evaluate(painted)).toBe(true);
    const collapsed = await cards.evaluateAll(nodes => nodes.filter(node => !node.hidden).length);
    expect(collapsed).toBeGreaterThan(0);
    expect(collapsed).toBeLessThan(10);
    await toggle.click();
    await expect(cards.filter({visible: true})).toHaveCount(10);
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await first.click();
    await expect(first).toHaveAttribute('data-display-style', 'Graph24hr');
    await expect(first.locator('.metric-status')).toHaveText('');
    await expect.poll(() => first.locator('.graph-view canvas').evaluate(canvas => Boolean(window.Chart.getChart(canvas)))).toBe(true);
    await expect.poll(() => first.locator('.graph-view canvas').evaluate(painted)).toBe(true);
    const chart = await first.locator('.graph-view canvas').evaluate(canvas => {
      const chart = window.Chart.getChart(canvas);
      return {zones: chart.options.plugins.micrographZones.length, datasets: chart.data.datasets.length};
    });
    expect(chart.zones).toBeGreaterThan(0);
    expect(chart.datasets).toBe(2);
    await first.click();
    await expect(first).toHaveAttribute('data-display-style', 'Graph6hr');
    await first.click();
    await expect(first).toHaveAttribute('data-display-style', 'Gauge');
    const wind = page.locator('#stationMetricGrid [data-metric="Wind Direction"]');
    await wind.scrollIntoViewIfNeeded();
    await expect(wind.locator('.metric-current-value')).toContainText('9.66 km/h');
    await wind.click();
    await expect(wind.locator('.graph-view canvas')).toHaveAttribute('aria-label', /wind rose/);
    await expect.poll(() => wind.locator('.graph-view canvas').evaluate(painted)).toBe(true);
    await page.reload();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await toggle.click();
    await page.setViewportSize({width: 390, height: 844});
    await expect(cards.filter({visible: true})).toHaveCount(1);
    await expect(errors).toEqual([]);
  });
}
