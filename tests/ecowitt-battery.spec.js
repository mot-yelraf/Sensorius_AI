import { test, expect } from '@playwright/test';

const sid = 'ecowitt-pr-check';

test('array battery updates beside Online and in Sensor Info', async ({ page }, testInfo) => {
  let currentState = 'OK';
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/*json_only=true*', route => route.fulfill({ json: {
    available: [sid], values: { [sid]: { Temperature: 20 } }, timestamps: {}, stats: {},
    statuses: { [sid]: 'online' }, battery_statuses: { [sid]: currentState },
  } }));
  await page.route('**/sensor-settings/statistics?*', route => route.fulfill({ json: {
    ok: true, is_ecowitt: true, battery_status: 'LOW', gateway_ip: '192.0.2.11',
  } }));
  await page.goto('/?battery=OK');
  const icon = page.locator(`#${sid}_battery`);
  await expect(icon).toHaveAttribute('alt', 'Battery Status: OK');
  expect(await icon.evaluate(el => el.previousElementSibling.id)).toBe(`${sid}_statusdot`);
  for (const state of ['LOW', 'UNKNOWN', 'OK']) {
    currentState = state;
    await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
    await expect(icon).toHaveAttribute('src', `/ui_static/icons/battery-${state.toLowerCase()}.svg`);
    await expect(icon).toHaveAttribute('alt', `Battery Status: ${state}`);
  }
  await page.locator(`#${sid}_header a`).click();
  const modal = page.locator('#sensorSettingsModal');
  await modal.getByRole('button', { name: 'Sensor Info', exact: true }).click();
  await expect(modal.locator('[data-stat-value="battery-status"]')).toHaveText('LOW');
  await expect(modal.locator('[data-stat-value="gateway-ip"]')).toHaveText('192.0.2.11');
  await page.screenshot({ path: testInfo.outputPath('battery-sensor-info.png') });
});

for (const width of [1440, 390]) {
  test(`Caelus battery footer displays all states at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    let payload = {};
    await page.route('**/api/weather-forecast-app/current-readings', route => route.fulfill({ json: payload }));
    for (const state of ['OK', 'LOW', 'UNKNOWN']) {
      payload = { ok: true, is_ecowitt: true, sensor_id: sid, location: 'Weather Station', battery_status: state, display_metrics: [{ name: 'Temperature', value: 9.89, unit: '°C' }] };
      await page.goto(`/weather-forecast?battery=${state}`);
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await expect(page.locator("[data-readings-primary-value]")).toHaveText("9.89");
      const footer = page.locator('[data-readings-footer]');
      const battery = footer.locator('[data-readings-battery]');
      await expect(battery).toBeVisible();
      await expect(battery.locator('img')).toHaveAttribute('alt', `Battery Status: ${state}`);
      const bounds = await footer.boundingBox();
      const icon = await battery.boundingBox();
      expect(icon.x + icon.width).toBeLessThanOrEqual(bounds.x + bounds.width + 1);
      expect(await battery.locator('img').evaluate(el => el.complete && el.naturalWidth > 0)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`battery-${state.toLowerCase()}-${width}.png`), fullPage: true });
    }
    payload = { ok: true, sensor_id: 'aht-pr-check', display_metrics: [] };
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(page.locator('[data-readings-battery]')).toBeHidden();
  });
}
