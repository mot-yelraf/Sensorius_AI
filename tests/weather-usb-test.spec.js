import {test, expect} from '@playwright/test';

for (const width of [1440, 390]) {
  test(`USB console Test uses draft settings, enable gating and result toasts at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 1000});
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({body: ''}));
    await page.route('**/ecowitt/status', route => route.fulfill({json: {enabled: false, inventory: []}}));
    const tests = [];
    let saveCalls = 0;
    let failure = false;
    let finish;
    let pending;
    for (const kind of ['acurite', 'davis', 'oregon']) {
      await page.route(`**/${kind}/status`, route => route.fulfill({json: {
        enabled: false, label: `${kind} USB`, state: 'disabled', model: kind === 'acurite' ? '01536' : 'WMR100',
        port: '', baud_rate: 19200, poll_interval_sec: 60,
      }}));
      await page.route(`**/${kind}/save`, route => { saveCalls++; return route.fulfill({json: {ok: true}}); });
      await page.route(`**/${kind}/test`, async route => {
        tests.push({kind, payload: route.request().postDataJSON()});
        if (pending) await pending;
        return route.fulfill({status: failure ? 502 : 200, json: failure
          ? {ok: false, error: `${kind} data retrieval failed: no valid live data returned.`}
          : {ok: true, message: `${kind} data retrieval succeeded.`}});
      });
    }
    await page.goto('/');
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', {name: 'Add Device', exact: true}).click();
    await modal.locator('#add-weather-section > summary').click();
    for (const [index, kind] of ['acurite', 'davis', 'oregon'].entries()) {
      await modal.locator(`#add-${kind}-section > summary`).click();
      const section = modal.locator(`#add-${kind}-section`);
      const checkbox = modal.locator(`#${kind}-enabled`);
      const testButton = modal.locator(`#btn-${kind}-test`);
      const saveButton = modal.locator(`#btn-${kind}-save`);
      const status = modal.locator(`#${kind}-status`);
      const footer = section.locator('.weather-console-footer');
      await expect(section.locator('.weather-console-actions button')).toHaveCount(0);
      await expect(footer.getByRole('button')).toHaveText(['Test', 'Save']);
      await expect(testButton).toBeDisabled();
      await expect(saveButton).toBeEnabled();
      if (kind === 'davis') {
        await expect(checkbox).toBeDisabled();
        await modal.locator('#davis-port').fill('   ');
        await expect(checkbox).toBeDisabled();
        await modal.locator('#davis-port').fill('/dev/cu.usbserial-draft');
        await expect(checkbox).toBeEnabled();
        await checkbox.check();
        await expect(testButton).toBeEnabled();
        await modal.locator('#davis-port').fill('');
        await expect(checkbox).not.toBeChecked();
        await expect(checkbox).toBeDisabled();
        await expect(testButton).toBeDisabled();
        await modal.locator('#davis-port').fill('/dev/cu.usbserial-draft');
        await expect(checkbox).toBeEnabled();
        await modal.locator('#davis-baud-rate').selectOption('9600');
      } else {
        await modal.locator(`#${kind}-model`).selectOption(kind === 'acurite' ? '02032' : 'WMR88A');
        await modal.locator(`#${kind}-usb-serial`).fill('draft-console');
      }
      await modal.locator(`#${kind}-poll-interval`).fill('300');
      await checkbox.check();
      await expect(testButton).toBeEnabled();
      await checkbox.uncheck();
      await expect(testButton).toBeDisabled();
      await checkbox.check();
      pending = new Promise(resolve => {finish = resolve;});
      await testButton.click();
      await expect.poll(() => tests.length).toBe(index * 2 + 1);
      await expect(testButton).toBeDisabled();
      await expect(saveButton).toBeDisabled();
      await expect(checkbox).toBeDisabled();
      expect(tests.at(-1)).toEqual({kind, payload: kind === 'davis'
        ? {enabled: true, poll_interval_sec: '300', port: '/dev/cu.usbserial-draft', baud_rate: '9600'}
        : {enabled: true, poll_interval_sec: '300', model: kind === 'acurite' ? '02032' : 'WMR88A', usb_serial: 'draft-console'}});
      finish();
      pending = null;
      await expect(status).toHaveText(`${kind} data retrieval succeeded.`);
      await expect(modal.locator('.modal-pane-toast-container .toast.ok').filter({hasText: `${kind} data retrieval succeeded.`})).toBeVisible();
      await expect(testButton).toBeEnabled();
      await expect(saveButton).toBeEnabled();
      failure = true;
      await testButton.click();
      await expect(status).toContainText('data retrieval failed');
      await expect(modal.locator('.modal-pane-toast-container .toast.error').filter({hasText: `${kind} data retrieval failed`})).toBeVisible();
      await expect(testButton).toBeEnabled();
      failure = false;
      expect(saveCalls).toBe(0);
      await footer.scrollIntoViewIfNeeded();
      const messageBox = await status.boundingBox();
      const testBox = await testButton.boundingBox();
      const saveBox = await saveButton.boundingBox();
      expect(testBox.x + testBox.width).toBeLessThan(saveBox.x);
      if (width > 700) {
        expect(messageBox.x + messageBox.width).toBeLessThan(testBox.x);
        expect(Math.abs(messageBox.y + messageBox.height / 2 - testBox.y - testBox.height / 2)).toBeLessThan(2);
      } else {
        expect(messageBox.y + messageBox.height).toBeLessThanOrEqual(testBox.y);
      }
      await page.screenshot({path: test.info().outputPath(`${kind}-test-${width}.png`)});
      await checkbox.uncheck();
      await expect(testButton).toBeDisabled();
      if (width < 700) await modal.getByRole('button', {name: '< Back', exact: true}).click();
    }
  });
}
