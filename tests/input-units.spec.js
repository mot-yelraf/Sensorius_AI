import {test, expect} from '@playwright/test';

test.beforeEach(async ({page}) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({contentType: 'application/javascript', body: ''}));
});

for (const metric of ['Temperature', 'Temperature_F', 'Plant Temperature', 'Plant Temperature_F']) {
  test(`automation ${metric} preserves native precision and sends explicit edit units`, async ({page}) => {
    const fahrenheit = metric.endsWith('_F');
    const meta = fahrenheit
      ? {native_unit: '°F', unit: '°C', factor: 5/9, offset: -32*5/9}
      : {native_unit: '°C', unit: '°F', factor: 1.8, offset: 32};
    const condition = {type: 'sensor', sensor: 'test', metric, op: '>', value: 25.123456789, hyst: .123456789};
    const script = {name: 'Precision rule', enabled: true, conditions: [condition], actions: [{type: 'none', executor_switch_id: '__system__'}]};
    await page.route('**/sensor-directory', route => route.fulfill({json: [{id: 'test', location: 'Greenhouse'}]}));
    let releaseUnits;
    const unitsReady = new Promise(resolve => { releaseUnits = resolve; });
    await page.route('**/sensor-metrics?*', async route => {
      await unitsReady;
      await route.fulfill({json: {[metric]: meta}});
    });
    await page.route('**/automation-context', route => route.fulfill({json: {actors: [], email_enabled: false, executor_switch_id: '__system__'}}));
    await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: [{rule_id: 'precision', enabled: true, script_json: JSON.stringify(script)}]}}));
    const saves = [];
    await page.route('**/submit-advanced-trigger', async route => {
      saves.push(JSON.parse(route.request().postDataJSON().script_json));
      await route.fulfill({json: {ok: true, rule_id: 'precision'}});
    });
    await page.goto('/');
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', {name: 'Automations', exact: true}).click();
    await modal.getByText('Precision rule', {exact: true}).click();
    const values = modal.locator('.sensor-bottom input[type=number]');
    await expect(values.nth(0)).toBeDisabled();
    releaseUnits();
    await expect(values.nth(0)).toBeEnabled();
    await expect(modal.locator('.sensor-bottom')).toContainText(`Threshold (${meta.unit})`);
    expect(Number(await values.nth(0).inputValue())).toBeCloseTo(condition.value * meta.factor + meta.offset, 8);
    expect(Number(await values.nth(1).inputValue())).toBeCloseTo(condition.hyst * meta.factor, 8);
    await values.nth(0).focus();
    await values.nth(1).focus();
    await modal.locator('#btnSetAutomation').click();
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0].conditions[0]).toEqual(condition);
    await expect(modal.locator('#btnSetAutomation')).toBeEnabled();
    await values.nth(0).fill('77');
    await values.nth(1).fill('1.8');
    await modal.locator('#btnSetAutomation').click();
    await expect.poll(() => saves.length).toBe(2);
    expect(saves[1].conditions[0]).toEqual({...condition, value: 77, hyst: 1.8, value_unit: meta.unit, hyst_unit: meta.unit});
  });
}

test('calibration displays Fahrenheit deltas but preserves canonical computed results', async ({page}) => {
  const writes = [];
  await page.route('**/calibration/device/apply', async route => {
    writes.push(route.request().postDataJSON());
    await route.fulfill({json: {status: 'success'}});
  });
  const preview = {ok: true, reference_id: 'reference', sensors: [{sensor_id: 'aht-pr-check', raw_temp: 20, adj_temp: 21, temp_offset: 1, temp_sigma: .1, rh_offset: 0, n_pairs: 50}]};
  await page.route('**/system-calibration/preview', route => route.fulfill({json: preview}));
  let applied;
  await page.route('**/system-calibration/apply', async route => {
    applied = route.request().postDataJSON();
    await route.fulfill({json: {ok: true, applied: ['aht-pr-check']}});
  });
  await page.goto('/');
  await page.evaluate(() => window.editSensorSettings('aht-pr-check'));
  const modal = page.locator('#sensorSettingsModal');
  await modal.getByRole('button', {name: 'Sensor Calibration', exact: true}).click();
  const input = modal.locator('.devCalInput').first();
  await expect(input).toHaveValue('1.8');
  await expect(input).toHaveAttribute('data-input-unit', '°F');
  page.once('dialog', dialog => dialog.accept());
  await modal.locator('#devCalApplyBtn').click();
  expect(writes).toHaveLength(0);
  await input.fill('3.6');
  await modal.locator('#devCalApplyBtn').click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].offsets).toEqual([{key: 'Calibration.Device.TEMP_OFFSET', value: 3.6, input_unit: '°F'}]);
  await expect(modal.locator('#devCalApplyBtn')).toBeEnabled();
  page.once('dialog', dialog => dialog.accept());
  await modal.locator('#devCalApplyBtn').click();
  expect(writes).toHaveLength(1);
  await modal.getByRole('button', {name: 'System Calibration', exact: true}).click();
  await modal.locator('#sysCalRefSensor').selectOption('reference');
  await modal.locator('.sysCalSensorCheck[value="aht-pr-check"]').check();
  await modal.locator('#sysCalPreviewBtn').click();
  await expect(modal.locator('.sysCalDeltaTemp').first()).toHaveText('1.800');
  await expect(modal.locator('#sysCalStatus')).toContainText('Temp (°F): 68.00 → 69.80');
  await modal.locator('#sysCalApplyBtn').click();
  await expect.poll(() => applied).toBeTruthy();
  expect(applied.sensors[0].temp_offset).toBe(1);
});

test('altitude form submits the units printed beside the field', async ({page}) => {
  let saved;
  await page.route('**/submit-pi-setup', async route => {
    saved = new URLSearchParams(route.request().postData());
    await route.fulfill({json: {status: 'ok'}});
  });
  await page.goto('/');
  await page.getByRole('link', {name: 'Open General Settings'}).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', {name: 'General Settings', exact: true}).click();
  await modal.locator('summary').filter({hasText: /^Astral$/}).click();
  await expect(modal.locator('label[for="astral_altitude"]')).toHaveText('Altitude (ft)');
  await expect(modal.locator('#astral_altitude')).toHaveValue('1000');
  await modal.locator('#astral_altitude').fill('2000');
  await modal.locator('[data-runtime-section="system-astral"]').getByRole('button', {name: 'Save', exact: true}).click();
  await expect.poll(() => saved?.get('astral_altitude_unit')).toBe('ft');
  expect(saved.get('astral_altitude')).toBe('2000');
});
