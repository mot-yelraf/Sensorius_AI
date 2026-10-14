import { test, expect } from '@playwright/test';

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`halt and removal require selection and acknowledgement at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
    await page.route('**/halt-device-list', route => route.fulfill({ json: { devices: ['nodus-capable'] } }));
    await page.route('**/remove-device-list', route => route.fulfill({ json: { devices: ['nodus-capable', 'nodus-legacy'], device_details: {} } }));
    const haltRequests = [];
    await page.route('**/halt-device', async route => {
      haltRequests.push(route.request().postDataJSON());
      await route.fulfill({ json: { results: [{ device_id: 'nodus-capable', halted: true, relays: 'held' }] } });
    });
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: 'Open General Settings' }).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', { name: 'Halt or Remove Device', exact: true }).click();
    const halt = modal.locator('[data-runtime-section="device-halt"]');
    const remove = modal.locator('[data-runtime-section="device-remove"]');
    await expect(halt).not.toHaveAttribute('open');
    await expect(remove).not.toHaveAttribute('open');
    await expect(modal.locator('#halt-device-confirm')).toBeHidden();
    await expect(modal.locator('#remove-device-run')).toBeHidden();
    await halt.locator('summary').click();
    await expect(modal.locator('#halt-device-list input')).toHaveCount(1);
    await expect(halt).not.toContainText('nodus-legacy');
    const haltButton = modal.locator('#halt-device-run');
    await expect(haltButton).toBeDisabled();
    await modal.locator('#halt-device-confirm').check();
    await expect(haltButton).toBeDisabled();
    await modal.locator('#halt-device-list input').check();
    await expect(haltButton).toBeEnabled();
    await modal.locator('#halt-device-confirm').uncheck();
    await expect(haltButton).toBeDisabled();
    await modal.locator('#halt-device-confirm').check();
    await haltButton.click();
    await expect(modal.locator('#halt-device-status')).toContainText('halt confirmed; ready to unplug');
    expect(haltRequests).toEqual([{ device_ids: ['nodus-capable'], acknowledged: true }]);
    await expect(modal.locator('#halt-device-confirm')).not.toBeChecked();
    await expect(haltButton).toBeDisabled();
    if (viewport.width < 700) {
      await modal.getByRole('button', { name: '< Back', exact: true }).click();
    } else {
      await halt.locator('summary').click();
    }
    await remove.locator('summary').click();
    await expect(modal.locator('#remove-device-list input')).toHaveCount(2);
    const removeButton = modal.locator('#remove-device-run');
    await modal.locator('#remove-device-list input').first().check();
    await expect(removeButton).toBeDisabled();
    await modal.locator('#remove-device-confirm').check();
    await expect(removeButton).toBeEnabled();
    await modal.locator('#remove-device-confirm').uncheck();
    await expect(removeButton).toBeDisabled();
    await page.screenshot({ path: `/private/tmp/halt-remove-${viewport.width}.png` });
  });
}
