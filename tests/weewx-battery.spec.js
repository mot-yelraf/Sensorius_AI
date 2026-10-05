import { test, expect } from '@playwright/test';

for (const sid of ['weewx-station', 'custom-weather-station']) {
  test(`WeeWX title battery renders and refreshes for ${sid}`, async ({ page }, testInfo) => {
    let currentState = 'OK';
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
    await page.route('**/*json_only=true*', route => route.fulfill({ json: {
      available: [sid], values: { [sid]: { Temperature_F: 70 } }, timestamps: {}, stats: {},
      statuses: { [sid]: 'online' }, battery_statuses: { [sid]: currentState },
    } }));
    await page.goto(`/?station=${sid}&battery=OK`);
    const icon = page.locator(`#${sid}_battery`);
    await expect(icon).toHaveAttribute('alt', 'Battery Status: OK');
    expect(await icon.evaluate(el => el.previousElementSibling.id)).toBe(`${sid}_statusdot`);
    for (const state of ['LOW', 'UNKNOWN', 'OK', 'invalid']) {
      currentState = state;
      const expected = state === 'invalid' ? 'UNKNOWN' : state;
      await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
      await expect(icon).toHaveAttribute('src', `/ui_static/icons/battery-${expected.toLowerCase()}.svg`);
      await expect(icon).toHaveAttribute('alt', `Battery Status: ${expected}`);
      expect(await icon.evaluate(el => el.complete && el.naturalWidth > 0)).toBe(true);
    }
    await page.screenshot({ path: testInfo.outputPath('weewx-battery.png') });
  });
}
