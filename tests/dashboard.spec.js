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
