import { expect, test } from '@playwright/test';

test('Duplicate opens a disabled draft from the saved rule and saves it separately', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const conditions = [
    { type: 'sensor', sensor: 'nodus-sensor', metric: 'Temperature', op: '>', value: 25.123456789, hyst: 0.5 },
    { type: 'or' },
    { type: 'time', start: '07:30', end: '17:00', days: [0, 4] },
  ];
  const actions = [
    { type: 'switch', switch_key: 'nodus-switch::relay1', set: true, revert_action: 'previous_state', delay_s: 15 },
    { type: 'switch', switch_key: 'absent-switch::relay1', set: false, revert_action: 'do_nothing', delay_s: 5 },
    { type: 'notify', to: 'operator@example.com', executor_switch_id: '__system__' },
  ];
  const original = { name: 'Greenhouse cooling', enabled: true, conditions, actions };
  const rules = [{ rule_id: 'original-rule', enabled: true, script_json: JSON.stringify(original) }];
  const saves = [];

  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/sensor-directory', route => route.fulfill({ json: [{ id: 'nodus-sensor' }] }));
  await page.route('**/sensor-metrics?*', route => route.fulfill({ json: {
    Temperature: { native_unit: '°C', unit: '°F', factor: 1.8, offset: 32 },
  } }));
  await page.route('**/automation-context', route => route.fulfill({ json: {
    actors: [{ switch_id: 'nodus-switch', value: 'nodus-switch::relay1', display: 'Fan' }],
    email_enabled: true, executor_switch_id: '__system__',
  } }));
  await page.route('**/advanced/automations?*', route => route.fulfill({ json: { items: rules } }));
  await page.route('**/submit-advanced-trigger', route => {
    const { rule_id, enabled, script_json } = route.request().postDataJSON();
    saves.push({ rule_id, enabled, script: JSON.parse(script_json) });
    rules.push({ rule_id, enabled: enabled === 'true', script_json });
    return route.fulfill({ json: { ok: true, rule_id } });
  });

  await page.goto('/');
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', { name: 'Automations', exact: true }).click();
  await modal.getByText(original.name, { exact: true }).click();
  const duplicate = modal.getByRole('button', { name: 'Duplicate' });
  await expect(modal.locator('#automationEditorWrap .pane-footer button')).toHaveText(['Saved Automations', 'Duplicate', 'New', 'Remove']);
  const name = modal.getByLabel('Automation Name');
  const enabled = modal.locator('#automationEditorWrap').getByLabel('Enable', { exact: true });
  await expect(enabled).toHaveValue('true');
  await name.fill('Unsaved edit');
  await modal.locator('.sensor-bottom input[type=number]').first().fill('99');
  await duplicate.click();

  await expect(name).toHaveValue('');
  await expect(enabled).toHaveValue('false');
  await expect(duplicate).toBeHidden();
  await expect(modal.getByRole('button', { name: 'Remove', exact: true })).toBeDisabled();
  await expect(modal.locator('.action-actor').nth(1)).toHaveValue('absent-switch::relay1');
  expect(saves).toHaveLength(0);

  await name.fill('Greenhouse cooling copy');
  await modal.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(duplicate).toBeVisible();
  expect(saves).toHaveLength(1);
  expect(saves[0].rule_id).toMatch(/^auto-/);
  expect(saves[0].enabled).toBe('false');
  expect(saves[0].script).toEqual({ ...original, name: 'Greenhouse cooling copy', enabled: false });

  await modal.getByRole('button', { name: 'Saved Automations' }).click();
  await expect(modal.getByText('Greenhouse cooling copy', { exact: true })).toBeVisible();
  await modal.getByText(original.name, { exact: true }).click();
  await expect(name).toHaveValue(original.name);
  await expect(enabled).toHaveValue('true');
});
