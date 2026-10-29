import { test, expect } from '@playwright/test';

for (const width of [1440, 390]) {
  test(`AcuRite setup saves console selection and reports errors at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    await page.route('**/ecowitt/status', route => route.fulfill({ json: { enabled: false, inventory: [] } }));
    await page.route('**/acurite/status', route => route.fulfill({ json: {
      model: '01536', enabled: false, state: 'disabled', label: 'AcuRite USB', poll_interval_sec: 60,
    } }));
    let saved;
    let rejected = false;
    await page.route('**/acurite/save', route => {
      saved = route.request().postDataJSON();
      return route.fulfill({ status: rejected ? 400 : 200, json: rejected
        ? { ok: false, error: 'Data retrieval interval must be 60–3600 seconds.' }
        : { ok: true, enabled: true } });
    });
    await page.route('**/acurite/disable', route => route.fulfill({ json: { ok: true, enabled: false } }));
    await page.goto('/');
    await page.getByRole('link', { name: 'Open General Settings' }).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', { name: 'Add Device', exact: true }).click();
    await modal.locator('#add-acurite-section summary').click();
    await expect(modal.locator('#acurite-mode-help')).toContainText('automatically');
    await modal.locator('#acurite-model').selectOption('02032');
    await expect(modal.locator('#acurite-mode-help')).toContainText('MODE 4');
    await modal.locator('#acurite-usb-serial').fill('console-123');
    await modal.locator('#acurite-poll-interval').fill('300');
    await modal.locator('#btn-acurite-save').click();
    await expect(modal.locator('#acurite-status')).toContainText('Console saved');
    expect(saved).toEqual({ model: '02032', usb_serial: 'console-123', poll_interval_sec: '300' });
    await modal.locator('#btn-acurite-disable').click();
    await expect(modal.locator('#acurite-status')).toContainText('historical readings retained');
    rejected = true;
    await modal.locator('#btn-acurite-save').click();
    await expect(modal.locator('#acurite-status')).toContainText('60–3600');
    await expect(modal.locator('#btn-acurite-save')).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath(`acurite-setup-${width}.png`), fullPage: true });
  });
}
