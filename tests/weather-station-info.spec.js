import { test, expect } from '@playwright/test';

for (const width of [1440, 390]) {
  for (const kind of ['acurite', 'ecowitt']) {
    test(`${kind} reports connection, uptime and RF signal at ${width}px`, async ({ page }, testInfo) => {
      const sid = `${kind}-b6a529ffb882c4f`;
      const start = Math.floor(Date.now() / 1000) - 3600;
      await page.setViewportSize({ width, height: 1000 });
      await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
      await page.route('**/sensor-settings/statistics?*', route => route.fulfill({ json: {
        ok: true, is_ecowitt: kind === 'ecowitt', has_reporting_uptime: true,
        usb_connection: '24c0:0003\nbus-1-ports-2.3', usb_status: 'Connected',
        station_reception: 'Receiving', signal_strength_label: kind === 'acurite' ? '3/3 (outdoor RF)' : '4/4 (outdoor RF)',
        uptime_started_epoch: start, offline_events_24h: 0, last_offline_epoch: null,
        last_offline_event_label: 'No offline events', battery_status: 'OK',
        last_packet_epoch: Date.now() / 1000, data_packets_received: 3775,
        gateway_ip: '192.0.2.10',
      } }));
      await page.goto('/');
      await page.evaluate(sid => window.editSensorSettings(sid), sid);
      const modal = page.locator('#sensorSettingsModal');
      await modal.getByRole('button', { name: 'Sensor Info', exact: true }).click();
      const pane = modal.locator('[data-stat-panel]');
      await expect(pane.locator('[data-stat-value="battery-status"]')).toHaveText('OK');
      await expect(pane.locator('[data-stat-value="uptime"]')).toContainText('1h');
      await expect(pane.locator('[data-stat-value="offline-events"]')).toHaveText('0');
      await expect(pane.locator('[data-stat-signal-row]')).toBeVisible();
      await expect(pane.locator('[data-stat-value="signal-strength"]')).toContainText('outdoor RF');
      if (kind === 'acurite') {
        await expect(pane).not.toContainText('IP Address:');
        await expect(pane.locator('[data-stat-value="usb-connection"]')).toHaveText('24c0:0003\nbus-1-ports-2.3');
        await expect(pane.locator('[data-stat-value="usb-status"]')).toHaveText('Connected');
        await expect(pane.locator('[data-stat-value="station-reception"]')).toHaveText('Receiving');
      } else {
        await expect(pane).toContainText('IP Address:');
        await expect(pane.locator('[data-stat-value="gateway-ip"]')).toHaveText('192.0.2.10');
      }
      await expect(pane.locator('[data-stat-value="last-offline"]')).toHaveText('No offline events');
      await page.screenshot({ path: testInfo.outputPath(`${kind}-info-${width}.png`), fullPage: true });
      await pane.locator('[data-stat-reset]').click();
      await expect(pane.locator('[data-stat-value="uptime"]')).toHaveText(/^\d+s$/);
    });
  }
}

test('non-weather sensors hide unavailable RF signal', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/sensor-settings/statistics?*', route => route.fulfill({ json: { ok: true } }));
  await page.goto('/');
  await page.evaluate(() => window.editSensorSettings('aht-pr-check'));
  const modal = page.locator('#sensorSettingsModal');
  await modal.getByRole('button', { name: 'Sensor Info', exact: true }).click();
  await expect(modal.locator('[data-stat-signal-row]')).toBeHidden();
});
