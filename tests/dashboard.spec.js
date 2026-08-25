// @ts-check
import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', async (route) => {
    await route.fulfill({ contentType: 'application/javascript', body: '' });
  });
  await page.addInitScript(() => {
    window.Gauge = class {
      setOptions() { return this; }
      set() {}
    };
    window.Chart = class {
      static getChart() { return null; }
      constructor() {}
      destroy() {}
    };
  });
});

test('renders the Sensorius dashboard and expands additional metrics', async ({ page }) => {
  const response = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(response?.ok()).toBeTruthy();
  await expect(page).toHaveTitle(/Sensorius/i);

  const sensorGroup = page.locator(".sensor-group[data-sensor-id='aht-pr-check']");
  await expect(sensorGroup).toBeVisible();
  await expect(sensorGroup.locator('.metric-container')).toHaveCount(8);

  const toggle = sensorGroup.locator('.sensor-collapse-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(sensorGroup.locator('.metric-container').last()).toBeVisible();
});

test('shares and persists the Lunar Calendar view mode', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.removeItem('sensorius.moonViewMode'));
  await page.reload({ waitUntil: 'domcontentloaded' });

  await page.locator('#moonCalendarBtn').click();
  await expect(page.locator('#caelusMoonViewLocal')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => {
    window.CaelusMoon.renderMoonDisk(canvas, {
      name: 'First quarter',
      illumination: 50,
      bright_limb_angle: 135,
      disk_rotation: 42,
    });
  });
  const localDisk = await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => canvas.toDataURL());

  await page.locator('#caelusMoonViewReference').click();
  await expect(page.locator('#caelusMoonViewReference')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('sensorius.moonViewMode'))).toBe('reference');
  const referenceDisk = await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => canvas.toDataURL());
  expect(referenceDisk).not.toBe(localDisk);
  await expect(page.locator('#caelusCurrentMoonDisk')).toHaveAttribute('aria-label', /Reference north-up view/);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#moonViewReference')).toHaveAttribute('aria-pressed', 'true');
});

test('opens the 29 day Sun/Moon graph from an aligned tile button', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });

  const graphButton = page.locator('#sunMoon29Btn');
  const lunarButton = page.locator('#moonCalendarBtn');
  await expect(graphButton).toBeVisible();
  await expect(graphButton).toHaveText('29 Day Graph');

  const chartBox = await page.locator('#sunPathCanvas').boundingBox();
  expect(chartBox?.height).toBeLessThanOrEqual(72);

  const graphButtonBox = await graphButton.boundingBox();
  const lunarButtonBox = await lunarButton.boundingBox();
  expect(Math.abs((graphButtonBox?.y || 0) - (lunarButtonBox?.y || 0))).toBeLessThanOrEqual(2);

  await graphButton.click();
  await expect(page.locator('#sunMoon29Overlay')).toBeVisible();
  await expect(page.locator('#sunMoon29Overlay')).toHaveAttribute('aria-hidden', 'false');
});
