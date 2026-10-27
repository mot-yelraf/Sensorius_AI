import { test, expect } from '@playwright/test';

async function openStartup(page, enabled) {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/advanced/status', route => route.fulfill({ json: {
    runtime_instance_id: 'original', restart_enabled: enabled,
    restart_message: enabled ? 'Restart the Sensorius service.' : 'Automatic service required.',
    autostart_enabled: enabled, autostart_scope: 'system',
  } }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  await page.locator('#setupPiModal').getByRole('button', { name: 'Advanced', exact: true }).click();
  await page.locator('#adv-section-startup > summary').click();
  return page.locator('#btn-restart-system');
}

test('manual launch keeps the red restart button disabled despite unsaved auto-start edits', async ({ page }) => {
  const button = await openStartup(page, false);
  await expect(button).toBeVisible();
  await expect(button).toHaveClass(/red/);
  await expect(button).toBeDisabled();
  await expect(page.locator('#adv-restart-note')).toHaveText('Automatic service required.');
  await page.locator('#adv-autostart-enabled').check();
  await expect(button).toBeDisabled();
  const restart = await button.boundingBox();
  const save = await page.locator('#adv-section-startup').getByRole('button', { name: 'Save', exact: true }).boundingBox();
  expect(restart.x).toBeLessThan(save.x);
  expect(Math.abs(restart.y - save.y)).toBeLessThan(2);
});

test('restart waits for a new instance before reloading the dashboard', async ({ page }) => {
  let requests = 0;
  let polls = 0;
  await page.route('**/advanced/restart', route => {
    expect(route.request().method()).toBe('POST');
    requests++;
    return route.fulfill({ status: 202, json: { status: 'restarting', runtime_instance_id: 'original' } });
  });
  await page.route('**/advanced/restart/status', route => {
    polls++;
    return route.fulfill({ json: { runtime_instance_id: polls === 1 ? 'original' : 'replacement', error: '' } });
  });
  const button = await openStartup(page, true);
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toBeDisabled();
  await expect(page.locator('#adv-status')).toContainText('Waiting for the service');
  await page.waitForEvent('load');
  expect(requests).toBe(1);
  expect(polls).toBe(2);
  await expect(page.locator('#setupPiModal')).toBeHidden();
});

test('service manager errors are displayed and allow another attempt', async ({ page }) => {
  await page.route('**/advanced/restart', route => route.fulfill({ status: 202, json: { runtime_instance_id: 'original' } }));
  await page.route('**/advanced/restart/status', route => route.fulfill({ json: { runtime_instance_id: 'original', error: 'Permission denied' } }));
  const button = await openStartup(page, true);
  await button.click();
  await expect(page.locator('#adv-status')).toHaveText('Permission denied');
  await expect(button).toBeEnabled();
});
