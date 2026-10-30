import { test, expect } from '@playwright/test';

async function prepare(page) {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/*json_only=true*', route => route.fulfill({ json: { available: [], values: {}, stats: {}, timestamps: {} } }));
  await page.route('**/switch-status-update', route => route.fulfill({ json: {} }));
}

test('sensor details wait for their panes, recover from failure, and preserve calibration selection', async ({ page }) => {
  await prepare(page);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let statisticsCalls = 0;
  let calibrationCalls = 0;
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  await page.route('**/sensor-settings/statistics?*', async route => {
    statisticsCalls++;
    if (statisticsCalls === 1) return route.fulfill({ status: 503, json: { ok: false } });
    await wait;
    return route.fulfill({ json: { ok: true, data_packets_received: 1234, offline_events_24h: 2 } });
  });
  await page.route('**/sensor-settings/calibration-candidates', route => {
    calibrationCalls++;
    return route.fulfill({ json: { ok: true, sensor_ids: ['aht-pr-check', 'reference'] } });
  });
  await page.goto('/');
  const request = page.waitForRequest('**/edit-sensor?*');
  await page.evaluate(() => window.editSensorSettings('aht-pr-check'));
  expect((await request).url()).toContain('defer_details=1');
  const root = page.locator('#sensorSettingsModal');
  await expect(root).toBeVisible();
  expect(statisticsCalls).toBe(0);
  expect(calibrationCalls).toBe(0);
  const info = root.locator('[data-stat-panel]');
  await expect(info).toHaveAttribute('data-stats-loaded', '0');
  await root.locator('[data-pane-target="sensor-statistics-pane"]').click();
  await expect(info.locator('[data-stats-message]')).toContainText('Could not load statistics');
  await expect(info.locator('[data-stat-reset]')).toBeDisabled();
  await root.locator('[data-pane-target="sensor-settings-pane"]').click();
  await root.locator('[data-pane-target="sensor-statistics-pane"]').click();
  await expect.poll(() => statisticsCalls).toBe(2);
  await root.locator('[data-pane-target="sensor-settings-pane"]').click();
  await root.locator('[data-pane-target="sensor-statistics-pane"]').click();
  expect(statisticsCalls).toBe(2);
  release();
  await expect(info).toHaveAttribute('data-stats-loaded', '1');
  await expect(info.locator('[data-stat-value="packets"]')).toHaveText('1234');
  await expect(info.locator('[data-stat-reset]')).toBeEnabled();
  await root.locator('[data-pane-target="system-calibration-pane"]').click();
  await expect(root.locator('#sysCalRefSensor')).toHaveValue('aht-pr-check');
  await expect(root.locator('#sysCalSensorsTable .sysCalSensorCheck')).toHaveCount(2);
  await expect(root.locator('.sysCalSensorCheck[value="reference"]')).toBeChecked();
  await expect(root.locator('.sysCalSensorCheck[value="aht-pr-check"]')).not.toBeChecked();
  await expect(root.locator('#sysCalPreviewBtn')).toBeEnabled();
  await root.locator('#sysCalRefSensor').selectOption('reference');
  await root.locator('[data-pane-target="sensor-settings-pane"]').click();
  await root.locator('[data-pane-target="system-calibration-pane"]').click();
  await expect(root.locator('#sysCalRefSensor')).toHaveValue('reference');
  expect(calibrationCalls).toBe(1);
  await root.getByRole('button', { name: 'Close Sensor Settings' }).click();
  await expect(root).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('unchanged Display saves keep the page; a changed metric set reloads', async ({ page }) => {
  await prepare(page);
  await page.route('**/submit-pi-setup', route => route.fulfill({ json: { ok: true, message: 'Saved.' } }));
  await page.goto('/');
  await page.evaluate(() => { window.__settingsTestDocument = true; });
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  const root = page.locator('#setupPiModal');
  const display = root.locator('[data-runtime-section="system-display"]');
  await display.locator(':scope > summary').click();
  await display.locator('.btn-system-save').click();
  await expect(root.locator('#system-status')).toHaveText('Saved.');
  await page.waitForTimeout(700);
  expect(await page.evaluate(() => window.__settingsTestDocument)).toBe(true);
  await expect(root).toBeVisible();
  const current = await display.locator('#metric_set').inputValue();
  await display.locator('#metric_set').selectOption(current === 'All' ? 'Pick 6' : 'All');
  await Promise.all([
    page.waitForEvent('domcontentloaded'),
    display.locator('.btn-system-save').click(),
  ]);
  expect(await page.evaluate(() => window.__settingsTestDocument === true)).toBe(false);
});

test('switch finish saves update cards and titles without navigating', async ({ page }) => {
  await prepare(page);
  await page.goto('/?switches=true');
  await page.evaluate(() => { window.__settingsTestDocument = true; });
  const card = page.locator('.switch-metric-container');
  const location = await card.getAttribute('data-switch-location');
  await page.route('**/submit-switch-settings', route => route.fulfill({ json: { ok: true, switch_tile_style_map: { [location]: 'Brass' } } }));
  await page.evaluate(() => window.editSwitchSettings('switch-pr-check'));
  const root = page.locator('#switchSettingsModal');
  await root.getByLabel('Style for all switches at this location').selectOption('Brass');
  await root.locator('#switchSettingsSaveBtn').click();
  await expect(card).toHaveAttribute('data-tile-style', 'Brass');
  await expect(card.locator('.switch-card-title-row')).toHaveAttribute('data-tile-style', 'Brass');
  await expect(page.locator('#group_switches > .sensor-group-header')).toHaveAttribute('data-tile-style', 'Brass');
  await page.waitForTimeout(700);
  expect(await page.evaluate(() => window.__settingsTestDocument)).toBe(true);
  await page.evaluate(() => {
    const card = document.querySelector('.switch-metric-container');
    const second = card.cloneNode(true);
    second.dataset.switchLocation = 'garden';
    card.parentNode.appendChild(second);
    window.applySwitchTileStyles({ garden: 'Chrome' });
  });
  await expect(page.locator(`.switch-metric-container[data-switch-location="${location}"]`)).toHaveAttribute('data-tile-style', 'Brass');
  await expect(page.locator('.switch-metric-container[data-switch-location="garden"] .switch-card-title-row')).toHaveAttribute('data-tile-style', 'Chrome');
  await expect(page.locator('#group_switches > .sensor-group-header')).toHaveAttribute('data-tile-style', 'Theme');
});

for (const style of ['Theme', 'Chrome', 'Brass', 'Mahogany & Brass']) {
  test(`row titles follow ${style}, including live sensor updates`, async ({ page }, testInfo) => {
    await prepare(page);
    await page.goto(`/?switches=true&tile_style=${encodeURIComponent(style)}`);
    const header = page.locator('#group_aht-pr-check > .sensor-group-header');
    await expect(header).toHaveAttribute('data-tile-style', style);
    if (style !== 'Theme') expect(await header.evaluate(el => getComputedStyle(el).backgroundImage)).toContain('gradient');
    await page.evaluate(style => window.applySwitchTileStyles(Object.fromEntries([...document.querySelectorAll('.switch-metric-container')].map(card => [card.dataset.switchLocation, style]))), style);
    await expect(page.locator('#group_switches > .sensor-group-header')).toHaveAttribute('data-tile-style', style);
    await header.screenshot({ path: testInfo.outputPath('sensor-row-title.png') });
    await page.locator('#group_switches > .sensor-group-header').screenshot({ path: testInfo.outputPath('switch-row-title.png') });
    const next = style === 'Chrome' ? 'Brass' : 'Chrome';
    const metrics = await page.locator('#row_aht-pr-check .metric-container').evaluateAll(tiles => tiles.map(tile => tile.dataset.metric));
    await page.route('**/*json_only=true*', route => route.fulfill({ json: {
      available: ['aht-pr-check'], expected_gauge_map: { 'aht-pr-check': metrics },
      metric_tile_style_map: { 'aht-pr-check': next }, values: {}, stats: {}, timestamps: {},
      renderable_switches_view: ['switch-pr-check'],
    } }));
    await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
    await expect(header).toHaveAttribute('data-tile-style', next);
    await page.screenshot({ path: testInfo.outputPath('styled-titles.png') });
  });
}
