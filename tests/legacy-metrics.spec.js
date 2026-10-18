// @ts-check
import { test, expect } from '@playwright/test';

const temperatures = ['Temperature_F', 'Dew Point_F', 'Plant Temperature_F', 'Plant Dew Point_F', 'Soil Temp_F'];

for (const [system, unit, initial, refreshed] of [
  ['Imperial', '°F', '68', '77'],
  ['Metric', '°C', '20', '25'],
]) {
  test(`all legacy metric cards render and refresh in ${system}`, async ({ page }) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({
      contentType: 'application/javascript', body: '',
    }));
    // Initial values exercise the real renderer's Celsius -> Fahrenheit fallback.
    // Refresh values model the production JSON route's native-unit metric views.
    await page.route('**/*json_only=true*', route => route.fulfill({ json: {
      available: [],
      values: { 'legacy-pr-check': {
        ...Object.fromEntries(temperatures.map(name => [name, 77])),
        'Soil Moisture Deficit': 12, 'Soil Stress Index': 30, 'Baro-Pressure': 1013.2,
      } },
      timestamps: { 'legacy-pr-check': 'updated' }, stats: {},
    } }));
    await page.goto(`/?legacy=true&unit_system=${system}`, { waitUntil: 'load' });
    for (const name of temperatures) {
      const card = page.locator(`.metric-container[data-metric="${name}"]`);
      await expect(card).toHaveCount(1);
      await expect(card.locator('.metric-title')).toHaveText(`${name} (${unit})`);
      await expect(card.locator('[id$="_val"]')).toContainText(new RegExp(`(${initial}|${refreshed})(\\.0)? ${unit}`));
    }
    await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
    for (const name of temperatures) {
      await expect(page.locator(`.metric-container[data-metric="${name}"] [id$="_val"]`)).toContainText(`${refreshed} ${unit}`);
    }
    for (const name of ['Soil Moisture Deficit', 'Soil Stress Index', 'Baro-Pressure']) {
      const card = page.locator(`.metric-container[data-metric="${name}"]`);
      await expect(card).toHaveCount(1);
      await expect(card.locator('[id$="_val"]')).not.toContainText('--');
    }
  });
}
