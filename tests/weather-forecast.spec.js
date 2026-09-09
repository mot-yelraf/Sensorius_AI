import { test, expect } from '@playwright/test';

// Heights measured on the existing card before adding the RH and wind rows.
const cardBudgets = [{ width: 1440, height: 546 }, { width: 1024, height: 580 }, { width: 390, height: 779 }];
for (const budget of cardBudgets) {
  test(`Caelus daily ranges fit the existing card at ${budget.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: budget.width, height: 1000 });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      return url.hostname === '127.0.0.1' || url.hostname === 'localhost' ? route.continue() : route.abort();
    });
    for (const units of ['Metric', 'Imperial']) {
      const response = await page.goto(`/weather-forecast?units=${units}`, { waitUntil: 'domcontentloaded' });
      expect(response.ok()).toBe(true);
      const card = page.locator('.forecast-panel');
      await expect(card).not.toContainText('Updated from');
      await expect(card.getByRole('button', { name: '6-day details' })).toHaveCount(0);
      const days = card.locator('.forecast-day');
      await expect(days).toHaveCount(6);
      for (const day of await days.all()) {
        await expect(day.locator('time')).toBeVisible();
        await expect(day.locator('svg')).toBeVisible();
        await expect(day).toContainText(units === 'Metric' ? '17.8-30.0°C' : '64-86°F');
        await expect(day).toContainText('RH 35-85%');
        await expect(day).toContainText(units === 'Metric' ? 'Wind 4-29 km/h' : 'Wind 2-18 mph');
        await expect(day).toContainText('Rain 59%');
        expect(await day.evaluate(element => Array.from(element.querySelectorAll('time, strong, small')).every(row => row.scrollWidth <= row.clientWidth + 1))).toBe(true);
      }
      expect((await card.boundingBox()).height).toBeLessThanOrEqual(budget.height);
      await card.screenshot({ path: testInfo.outputPath(`forecast-${units}.png`) });
      await card.getByRole('button', { name: 'Show next forecast hour' }).click();
      await expect(card.locator('[data-hourly-status]')).toHaveText('Hours 2–9 of 24');
    }
  });
}
