import { test, expect } from '@playwright/test';

async function expectCalibrationRows(section, widths) {
  const rows = section.locator(':scope > .calibration-grid');
  await expect(rows).toHaveCount(widths.length);
  for (const [index, width] of widths.entries()) {
    const row = rows.nth(index);
    await expect(row.locator(':scope > .calibration-field')).toHaveCount(width);
    const layout = await row.evaluate(grid => ({
      columns: getComputedStyle(grid).gridTemplateColumns.split(/\s+/).length,
      positions: Array.from(grid.children, field => field.getBoundingClientRect().top),
      fits: grid.scrollWidth <= grid.clientWidth + 1,
      stacked: Array.from(grid.children).every(field => (
        field.lastElementChild.getBoundingClientRect().top >= field.firstElementChild.getBoundingClientRect().bottom
      )),
    }));
    expect(layout.columns).toBe(width);
    expect(new Set(layout.positions).size).toBe(1);
    expect(layout.fits).toBe(true);
    expect(layout.stacked).toBe(true);
  }
}

for (const width of [1440, 390]) {
  test(`soil calibration saves one probe and shows the device values at ${width}px`, async ({ page }) => {
    let submitted;
    await page.setViewportSize({ width, height: 950 });
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    await page.route('**/calibration/soil/derived', route => {
      submitted = route.request().postDataJSON();
      return route.fulfill({ json: {
        status: 'success', message: 'Saved to Nodus.',
        values: { SPD_WET_THRESHOLD_PCT: 42.25, SPD_DRY_THRESHOLD_PCT: 18 },
      } });
    });
    await page.goto('/?soil=true');
    await page.evaluate(() => window.editSensorSettings('soil-pr-check'));
    const modal = page.locator('#sensorSettingsModal');
    await modal.getByRole('button', { name: 'Sensor Calibration', exact: true }).click();
    const offsets = modal.locator('.section').filter({ has: page.getByRole('heading', { name: 'Soil Offsets' }) });
    await expectCalibrationRows(offsets, [4]);
    await expect(offsets.getByLabel('Soil pH (pH)')).toHaveAttribute('id', 'soilPhOffsetInput');
    const temperatureLabel = offsets.locator('label').filter({ hasText: 'Soil Temperature' });
    expect(await temperatureLabel.evaluate(label => {
      const word = 'Temperature';
      const text = label.firstChild;
      const start = text.textContent.indexOf(word);
      const range = document.createRange();
      range.setStart(text, start);
      range.setEnd(text, start + word.length);
      return range.getClientRects().length;
    })).toBe(1);
    const channel1 = modal.locator('[data-channel="CH1"][data-soil-derived-group="SoilDeficit"]');
    const channel2 = modal.locator('[data-channel="CH2"]');
    await channel1.locator('summary').click();
    await expectCalibrationRows(channel1, [2, 2]);
    await expect(channel1).toContainText('26.5 %');
    await expect(channel1).not.toContainText('32 %');
    await expect(modal.locator('[data-channel="CH2"][data-soil-derived-group="NPK"]')).toHaveCount(0);
    await channel1.getByLabel('Wet threshold (%)').fill('42.2');
    await channel1.getByRole('button', { name: 'Save Soil Moisture Deficit' }).click();
    await expect(channel1.locator('[data-soil-status]')).toHaveText('Saved to Nodus.');
    await expect(page.locator('.calibration-toast.ok')).toHaveText('Saved to Nodus.');
    await expect(channel1.getByLabel('Wet threshold (%)')).toHaveValue('42.25');
    expect(submitted).toEqual({
      sensor_id: 'soil-pr-check', channel: 'CH1', group: 'SoilDeficit', temperature_unit: '°F',
      values: { SPD_WET_THRESHOLD_PCT: 42.2, SPD_DRY_THRESHOLD_PCT: 18 },
    });
    await channel2.locator('summary').click();
    await expect(channel2.getByLabel('Wet threshold (%)')).toHaveValue('44');
    await expect(channel2).toContainText('32 %');
    const stress = modal.locator('[data-channel="CH1"][data-soil-derived-group="SoilStress"]');
    await stress.locator('summary').click();
    await expectCalibrationRows(stress, [3, 4, 2]);
    await expect(stress.getByLabel('High critical temperature (°F)')).toHaveValue('86.0');

    const fertility = modal.locator('[data-channel="CH1"][data-soil-derived-group="NPK"]');
    await fertility.locator('summary').click();
    await expectCalibrationRows(fertility, [4, 4]);
    await expect(fertility.locator('output')).toHaveText('50%');
    await fertility.getByLabel('Lowest nutrient score weight (%)').fill('75');
    await expect(fertility.locator('output')).toHaveText('25%');
  });
}

test('soil calibration shows the device error and a pending confirmation toast', async ({ page }) => {
  let status = 409;
  let reply = {
    status: 'error', device_error: 'calibration_handler_pystack',
    message: 'calibration_handler_pystack. 1 of 2 changed fields were confirmed.',
  };
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/calibration/soil/derived', route => route.fulfill({ status, json: reply }));
  await page.goto('/?soil=true');
  await page.evaluate(() => window.editSensorSettings('soil-pr-check'));
  const modal = page.locator('#sensorSettingsModal');
  await modal.getByRole('button', { name: 'Sensor Calibration', exact: true }).click();
  const group = modal.locator('[data-channel="CH1"][data-soil-derived-group="SoilDeficit"]');
  await group.locator('summary').click();
  const save = group.getByRole('button', { name: 'Save Soil Moisture Deficit' });
  await save.click();
  const reason = 'Nodus ran out of Python stack space while saving. 1 of 2 changed fields were confirmed.';
  await expect(group.locator('[data-soil-status]')).toHaveText(reason);
  await expect(page.locator('.calibration-toast.error')).toHaveText(reason);

  status = 202;
  reply = { status: 'pending', message: 'Nodus saved the settings. Waiting for the device update.' };
  await save.click();
  await expect(page.locator('.calibration-toast')).toHaveText(reply.message);
  await expect(page.locator('.calibration-toast.ok, .calibration-toast.error')).toHaveCount(0);
});

test('standard calibration offsets use two and three columns with labels above inputs', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  for (const [sensorId, columns] of [['aht-pr-check', 2], ['acurite-b6a529ffb882c4f', 3]]) {
    await page.goto('/');
    await page.evaluate(id => window.editSensorSettings(id), sensorId);
    const modal = page.locator('#sensorSettingsModal');
    await modal.getByRole('button', { name: 'Sensor Calibration', exact: true }).click();
    const offsets = modal.locator('.section').filter({ has: page.getByRole('heading', { name: 'Offsets', exact: true }) });
    await expectCalibrationRows(offsets, [columns]);
    await expect(offsets.getByLabel('Temperature (°F)')).toHaveValue('1.8');
  }
});

test('soil offsets remain editable when Nodus has no derived calibration capability', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.goto('/?soil=true');
  await page.evaluate(() => window.editSensorSettings('soil-no-capability-pr-check'));
  const modal = page.locator('#sensorSettingsModal');
  await modal.getByRole('button', { name: 'Sensor Calibration', exact: true }).click();
  await expect(modal.getByText('Waiting for Nodus to publish per-probe calibration settings.')).toBeVisible();
  await expect(modal.locator('[data-soil-derived-group]')).toHaveCount(0);
  await expect(modal.locator('[data-soil-save]')).toHaveCount(0);
  await expect(modal.getByLabel('Soil pH (pH)')).toBeEditable();
  await expect(modal.locator('#devCalApplyBtn')).toBeEnabled();
});
