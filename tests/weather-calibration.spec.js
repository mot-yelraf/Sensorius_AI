import { test, expect } from '@playwright/test';

const stations = [
  ['ecowitt-f85b1b51804c', 'ECOWITT-51804C'],
  ['acurite-b6a529ffb882c4f', 'ACURITE-882C4F'],
];

for (const width of [1440, 390]) {
  for (const [sid, label] of stations) {
    test(`weather header shortens only visible ID for ${sid} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
      await page.route('**/*json_only=true*', route => route.fulfill({ json: {
        available: [sid], values: { [sid]: { Temperature: 21 } }, stats: {}, timestamps: {},
        statuses: { [sid]: 'online' }, battery_statuses: { [sid]: 'OK' },
      } }));
      await page.goto(`/?station=${sid}`);
      const header = page.locator(`#${sid}_header`);
      await expect(header).toContainText(label);
      await expect(header).not.toContainText(sid.toUpperCase());
      await expect(page.locator(`#group_${sid}`)).toHaveAttribute('data-sensor-id', sid);
      await header.locator('a').click();
      await expect(page.locator('#sensorSettingsModal')).toBeVisible();
      await expect(page.locator('#sensorSettingsModal')).toHaveAttribute('data-sensor-id', sid);
      await page.screenshot({ path: testInfo.outputPath(`weather-id-${width}.png`), fullPage: true });
    });
  }
}

test('newly discovered weather rows use short labels and Station Pressure while keeping metric identity', async ({ page }) => {
  const sid = stations[1][0];
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/*json_only=true*', route => route.fulfill({ json: {
    available: [sid],
    values: { [sid]: { Temperature: 21, 'Console Absolute Baro-Pressure': 826 } },
    expected_gauge_map: { [sid]: ['Temperature', 'Console Absolute Baro-Pressure'] },
    stats: {}, timestamps: {}, statuses: { [sid]: 'online' }, locations: { [sid]: 'Weather Station' },
  } }));
  await page.addInitScript(sid => {
    sessionStorage.setItem('layoutRefreshDoneSig', JSON.stringify({
      sensors: [{ sid, metrics: ['Console Absolute Baro-Pressure', 'Temperature'] }], switches: [],
    }));
  }, sid);
  await page.goto(`/?station=${sid}`);
  await expect.poll(() => page.evaluate(() => window.__updateGaugesFinishedSeq || 0)).toBeGreaterThan(0);
  await page.evaluate(sid => document.getElementById(`group_${sid}`).remove(), sid);
  await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
  await expect(page.locator(`#${sid}_header`)).toContainText(stations[1][1]);
  await expect(page.locator(`#${sid}_header`)).not.toContainText(sid.toUpperCase());
  const pressure = page.locator(`#group_${sid} .metric-container[data-metric="Console Absolute Baro-Pressure"]`);
  await expect(pressure.locator('.metric-title')).toContainText('Station Pressure');
  await expect(pressure).toHaveAttribute('data-sensor', sid);
});

for (const width of [1440, 390]) {
  test(`AcuRite Sensor Calibration has offsets and read-only altitude at ${width}px`, async ({ page }, testInfo) => {
    const sid = stations[1][0];
    let saved;
    await page.setViewportSize({ width, height: 1000 });
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    await page.route('**/calibration/device/apply', route => {
      saved = route.request().postDataJSON();
      return route.fulfill({ json: { status: 'success' } });
    });
    await page.goto('/');
    await page.evaluate(sid => window.editSensorSettings(sid), sid);
    const modal = page.locator('#sensorSettingsModal');
    await modal.getByRole('button', { name: 'Sensor Calibration', exact: true }).click();
    const temp = modal.locator('[data-key="Calibration.Device.TEMP_OFFSET"]');
    const rh = modal.locator('[data-key="Calibration.Device.RH_OFFSET"]');
    const altitude = modal.locator('[data-key="Calibration.Device.ALTITUDE_METERS"]');
    await expect(temp).toHaveAttribute('data-input-unit', '°F');
    await expect(rh).toBeVisible();
    await expect(altitude).toHaveAttribute('readonly', '');
    await expect(altitude).toHaveAttribute('data-input-unit', 'ft');
    await expect(altitude).toHaveAttribute('data-force-send', '1');
    await temp.fill('3.6');
    await rh.fill('-5');
    await modal.locator('#devCalApplyBtn').click();
    await expect.poll(() => saved?.sensor_id).toBe(sid);
    expect(saved.device_kind).toBe('acurite_usb');
    expect(saved.offsets.find(item => item.key.endsWith('TEMP_OFFSET')).value).toBe(3.6);
    expect(saved.offsets.find(item => item.key.endsWith('RH_OFFSET')).value).toBe(-5);
    await page.screenshot({ path: testInfo.outputPath(`acurite-calibration-${width}.png`), fullPage: true });
  });
}
