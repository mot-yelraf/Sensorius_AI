// @ts-check
import { test, expect } from '@playwright/test';

for (const [system, unit, initial, refreshed] of [
  ['Imperial', '°F', '58.6', '60.8'],
  ['Metric', '°C', '14.8', '16'],
]) {
  test(`legacy soil card renders and refreshes in ${system}`, async ({ page }) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({
      contentType: 'application/javascript', body: '',
    }));
    await page.route('**/*json_only=true*', route => route.fulfill({ json: {
      available: [], values: { 'soil-pr-check': { 'Soil Temperature': 16 } },
      timestamps: { 'soil-pr-check': 'updated' }, stats: {
        'soil-pr-check': { 'Soil Temperature': {
          min: 10, avg: 12, max: 16,
          min_ts: '2026-10-18T08:00:00-06:00', max_ts: '2026-10-18T08:30:00-06:00',
        } },
      },
    } }));
    await page.goto(`/?soil=true&unit_system=${system}`, { waitUntil: 'load' });
    const card = page.locator('.metric-container[data-metric="Soil Temperature"]');
    await expect(card).toHaveCount(1);
    await expect(card).toContainText(`Soil Temperature (${unit})`);
    // Initial gauge setup and the first automatic refresh can race.
    await expect(card.locator('[id$="_val"]')).toContainText(new RegExp(`${initial}|${refreshed}`));
    await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
    await expect(card.locator('[id$="_val"]')).toContainText(`${refreshed} ${unit}`);
    await expect(card.locator('[id$="_stats"]')).not.toContainText('--');
  });
}
