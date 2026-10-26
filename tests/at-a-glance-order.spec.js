import { replaceDashboardConfig } from './helpers/dashboard-config.js';
// @ts-check
import { test, expect } from '@playwright/test';

const defaultOrder = ['bioBox', 'moonBox', 'displayDeviceBox', 'sunBox', 'weatherForecastBox'];
const tileSelector = '.dash-top-row > :is(#bioBox, #moonBox, #displayDeviceBox, #sunBox, #weatherForecastBox)';
const order = page => page.locator(tileSelector).evaluateAll(tiles => tiles.map(tile => tile.id));

test.beforeEach(async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({
    contentType: 'application/javascript', body: '',
  }));
});

test('moves tiles both ways, preserves order on reload, and isolates tile actions', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/', { waitUntil: 'load' });
  await expect(page.locator('.at-a-glance-order-controls')).toHaveCount(5);
  expect(await order(page)).toEqual(defaultOrder);
  await expect(page.getByRole('button', { name: 'Move Biodynamic Calendar left', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Move Weather Forecast right', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Move Sun Position left', exact: true })).toHaveCSS('color', 'rgb(39, 49, 58)');
  await expect(page.getByRole('button', { name: 'Move Sun Position left', exact: true })).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(page.getByRole('button', { name: 'Move Sun Position left', exact: true })).toHaveCSS('margin', '0px');
  const arrowBoxes = await page.locator('#sunBox .at-a-glance-order-controls button').evaluateAll(buttons =>
    buttons.map(button => ({ top: button.getBoundingClientRect().top, bottom: button.getBoundingClientRect().bottom })),
  );
  expect(arrowBoxes[0].bottom - arrowBoxes[0].top).toBe(7);
  expect(arrowBoxes[1].top - arrowBoxes[0].bottom).toBe(1);
  const titleBefore = await page.locator('#sunBox .astro-title').first().boundingBox();
  await page.getByRole('button', { name: 'Move Sun Position left', exact: true }).click();
  expect(await order(page)).toEqual(['bioBox', 'moonBox', 'sunBox', 'displayDeviceBox', 'weatherForecastBox']);
  await expect(page.locator('#sunMoon29Overlay')).toHaveAttribute('aria-hidden', 'true');
  const titleAfter = await page.locator('#sunBox .astro-title').first().boundingBox();
  expect(titleAfter?.y).toBe(titleBefore?.y);
  await page.getByRole('button', { name: 'Move Sun Position right', exact: true }).focus();
  await page.keyboard.press('Enter');
  expect(await order(page)).toEqual(defaultOrder);
  await expect(page.locator('#sunMoon29Overlay')).toHaveAttribute('aria-hidden', 'true');
  await page.getByRole('button', { name: 'Move Display Device By right', exact: true }).click();
  expect(await order(page)).toEqual(['bioBox', 'moonBox', 'sunBox', 'displayDeviceBox', 'weatherForecastBox']);
  await page.reload({ waitUntil: 'load' });
  expect(await order(page)).toEqual(['bioBox', 'moonBox', 'sunBox', 'displayDeviceBox', 'weatherForecastBox']);
  await expect(page.locator('.dashboard-brand-refresh #refresh_link')).toBeVisible();
  await expect(page.locator('#displayDeviceBox #refresh_link')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/at-a-glance-desktop.png', fullPage: false });
  await page.setViewportSize({ width: 390, height: 844 });
  const boxes = await page.locator(tileSelector).evaluateAll(tiles => tiles.map(tile => {
    const rect = tile.getBoundingClientRect();
    return { x: rect.x, y: rect.y, right: rect.right };
  }));
  boxes.forEach((box, i) => {
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(390);
    if (i) expect(box.y).toBeGreaterThan(boxes[i - 1].y);
  });
  await page.screenshot({ path: 'test-results/at-a-glance-mobile.png', fullPage: true });
});

test('ignores invalid storage and tolerates unavailable browser storage', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('sensorius.atAGlanceOrder', '{invalid'));
  await page.goto('/', { waitUntil: 'load' });
  expect(await order(page)).toEqual(defaultOrder);
  await page.evaluate(() => Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } }));
  await page.getByRole('button', { name: 'Move Biodynamic Calendar right', exact: true }).click();
  expect(await order(page)).toEqual(['moonBox', 'bioBox', 'displayDeviceBox', 'sunBox', 'weatherForecastBox']);
});

test('transparent arrows contrast with changing forecast and biodynamic backgrounds', async ({ page }) => {
  await page.goto('/', { waitUntil: 'load' });
  const weather = page.locator('#weatherForecastBox');
  const weatherArrow = weather.locator('[data-direction="left"]');
  for (const [scene, ink] of [
    ['clear-night', 'rgb(244, 248, 255)'],
    ['cloudy-day', 'rgb(20, 37, 45)'],
    ['rain-night', 'rgb(240, 247, 250)'],
  ]) {
    await weather.evaluate((tile, scene) => { tile.className = `astro-box forecast-scene-${scene}`; }, scene);
    await expect(weatherArrow).toHaveCSS('color', ink);
    await expect(weatherArrow).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  }
  await expect(page.locator('#displayDeviceBox [data-direction="left"]')).toHaveCSS('color', 'rgb(255, 255, 255)');
  const bioArrow = page.locator('#bioBox [data-direction="right"]');
  for (const [accent, ink] of [['#111111', 'rgb(255, 255, 255)'], ['#ffffe0', 'rgb(39, 49, 58)']]) {
    const now = new Date();
    const payload = {
      ok: true, tz: 'UTC',
      current: { sign: 'Test', timestamp: now.toISOString(), element: 'Earth', plant_part: 'Root' },
      calendar: [-1, 0, 1].map(offset => ({
        date: new Date(now.getTime() + offset * 86400000).toISOString().slice(0, 10),
        segments: [
          { sign: 'Test', start: '00:00', end: '12:00', accent: '#ffffff', color: '#ffffff' },
          { sign: 'Test', start: '12:00', end: '24:00', accent, color: accent },
        ],
      })),
    };
    await page.route('**/', async route => {
      const response = await route.fetch();
      const html = replaceDashboardConfig(await response.text(), { biodynamicData: payload });
      await route.fulfill({ response, body: html });
    });
    await page.reload({ waitUntil: 'load' });
    await expect(bioArrow).toHaveCSS('color', ink);
    await expect(bioArrow).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  }
});
