import {test, expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

for (const [theme, url, color] of [['leaf', '/', 'rgb(223, 245, 232)'], ['fruit', '/?documentation=true', 'rgb(111, 94, 72)']]) {
test(`${theme} dashboard paints its styled viewport while chart libraries are still downloading`, async ({page}) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const libraries = [
    ['gaugeJS', 'gaugeJS/dist/gauge.min.js'],
    ['chart.js@', 'chart.js/dist/chart.umd.js'],
    ['chartjs-adapter', 'chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js'],
    ['chartjs-plugin-annotation', 'chartjs-plugin-annotation/dist/chartjs-plugin-annotation.min.js'],
  ];
  let release;
  let requested = 0;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('https://cdn.jsdelivr.net/**', async route => {
    const entry = libraries.find(([key]) => route.request().url().includes(key));
    expect(entry).toBeTruthy();
    requested++;
    await pending;
    await route.fulfill({contentType: 'application/javascript', body: readFileSync(`node_modules/${entry[1]}`)});
  });
  try {
    await page.goto(url, {waitUntil: 'commit'});
    await expect.poll(() => requested).toBe(4);
    await expect(page.locator('.dashboard')).toBeVisible();
    await expect(page.locator('body')).toHaveCSS('background-color', color);
    await expect.poll(() => page.evaluate(() => performance.getEntriesByType('paint').some(entry => entry.name === 'first-contentful-paint'))).toBe(true);
    expect(await page.evaluate(() => typeof window.Gauge)).toBe('undefined');
    const timing = await page.evaluate(() => ({paints: performance.getEntriesByType('paint').map(entry => ({name: entry.name, startTime: entry.startTime})), chartDownloadsReleasedAfter: performance.now()}));
    await test.info().attach('startup-paint-timing', {body: JSON.stringify(timing), contentType: 'application/json'});
  } finally {
    release();
  }
  await page.waitForLoadState('load');
  await expect.poll(() => page.evaluate(() => [...document.querySelectorAll('.metric-container[data-metric="Temperature"]')].some(node => !!window[node.dataset.sensor + '_Temperature_gauge']?.render))).toBe(true);
  await expect.poll(() => page.evaluate(() => typeof window.Chart)).toBe('function');
  await expect(page.getByRole('link', {name: 'Open General Settings'})).toBeVisible();
  await page.getByRole('link', {name: 'Open General Settings'}).click();
  await expect(page.locator('#setupPiModal')).toBeVisible();
  expect(errors).toEqual([]);
});
}
