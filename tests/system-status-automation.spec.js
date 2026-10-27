import { test, expect } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`System Status defaults, device selection and saved thresholds work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/automation-context', route => route.fulfill({ json: {
      actors: [], email_enabled: true, executor_switch_id: '__system__',
      system_status_devices: [
        {id: 'soil-node', type: 'sensor', label: 'Soil'},
        {id: 'switch-node', type: 'switch', label: 'Relays'},
      ],
    } }));
    await page.route('**/switch-info?*', route => route.fulfill({json: {labels: {}, astral_status: {ok: true}}}));
    await page.route('**/sensor-directory', route => route.fulfill({json: []}));
    let items = [];
    let saved;
    await page.route('**/advanced/automations?*', route => route.fulfill({json: {items}}));
    await page.route('**/submit-advanced-trigger', async route => {
      const payload = route.request().postDataJSON();
      saved = JSON.parse(payload.script_json);
      items = [{rule_id: payload.rule_id, enabled: true, script_json: payload.script_json}];
      await route.fulfill({json: {ok: true, rule_id: payload.rule_id}});
    });
    await page.goto('/');
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', {name: 'Automations', exact: true}).click();
    await modal.locator('#autoName').fill('Hub status');
    await modal.locator('#conditionsContainer .cond-group select').first().selectOption('system_status');
    await modal.locator('.action-actor').first().selectOption('none');
    const cases = [
      ['disk_free', '1', '0.75', {threshold_gib: .75}],
      ['db_write_errors', '3', '5', {failures: 5}],
      ['sensor_offline', '60', '90', {device_id: 'soil-node', device_type: 'sensor', duration_min: 90}],
      ['sensor_offline', '60', '60', {device_id: '__all__', device_type: 'sensor', duration_min: 60}],
      ['mqtt_disconnected', '15', '20', {device_id: 'switch-node', device_type: 'switch', duration_min: 20}],
      ['mqtt_disconnected', '15', '15', {device_id: '__all__', device_type: 'all', duration_min: 15}],
    ];
    for (const [status, initial, custom, fields] of cases) {
      await modal.locator('.system-status-kind').selectOption(status);
      await expect(modal.locator('.system-status-value')).toHaveValue(initial);
      if (status === 'sensor_offline') {
        await expect(modal.locator('.system-status-device option')).toHaveCount(2);
      }
      if (fields.device_id) {
        await modal.locator('.system-status-device').selectOption(`${fields.device_type}:${fields.device_id}`);
      }
      await expect(modal.locator('.cond-group > .detail')).toHaveCount(0);
      await modal.locator('.system-status-value').fill(custom);
      saved = null;
      await modal.locator('#btnSetAutomation').click();
      await expect.poll(() => saved?.conditions).toEqual([{type: 'system_status', status, ...fields}]);
      await expect(modal.locator('#btnSetAutomation')).toBeEnabled();
      await expect(modal.locator('.system-status-value')).toHaveValue(custom);
      if (fields.device_id) await expect(modal.locator('.system-status-device')).toHaveValue(`${fields.device_type}:${fields.device_id}`);
      const row = modal.locator('.system-status');
      const bounds = await row.boundingBox();
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    }
  });
}
