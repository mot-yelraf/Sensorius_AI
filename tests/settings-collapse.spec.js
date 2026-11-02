import { test, expect } from '@playwright/test';

test('all settings sections start collapsed and reset when the runtime changes', async ({ page }) => {
  let runtime = 'before-restart';
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/advanced/status', route => route.fulfill({ json: {
    runtime_instance_id: runtime, restart_enabled: false,
  } }));
  await page.goto('/');
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  const modal = page.locator('#setupPiModal');
  const openSections = modal.locator('details[open]');
  await expect(modal.locator('details')).toHaveCount(24);
  await expect(openSections).toHaveCount(0);
  const advanced = modal.getByRole('button', { name: 'Advanced', exact: true });
  await advanced.click();
  await expect.poll(() => page.evaluate(() => window.__sensoriusExpandableSectionState.runtimeInstanceId)).toBe(runtime);
  await modal.getByRole('button', { name: 'General Settings', exact: true }).click();
  await modal.locator('[data-runtime-section="system-display"] > summary').click();
  await modal.locator('[data-runtime-section="display-dashboard-theme"] > summary').click();
  await modal.getByRole('button', { name: 'Add Device', exact: true }).click();
  for (const id of ['add-nodus-section', 'add-weather-section', 'add-acurite-section']) {
    await modal.locator(`#${id} > summary`).click();
  }
  await expect(openSections).toHaveCount(5);
  await advanced.click();
  await expect(openSections).toHaveCount(5);
  runtime = 'after-restart';
  await advanced.click();
  await expect.poll(() => page.evaluate(() => window.__sensoriusExpandableSectionState.runtimeInstanceId)).toBe(runtime);
  await expect(openSections).toHaveCount(0);
  await page.reload();
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  await expect(page.locator('#setupPiModal details[open]')).toHaveCount(0);
});
