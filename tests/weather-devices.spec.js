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
    if (width < 700) await modal.locator('#add-weather-section > summary').click();
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

for (const width of [1440, 390]) {
  test(`Weather Station groups console setup and supports Davis and Oregon at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    const saved = {};
    let rejected = false;
    for (const kind of ['davis', 'oregon']) {
      await page.route(`**/${kind}/status`, route => route.fulfill({ json: {
        enabled: false, state: 'disabled', port: '/dev/cu.usbserial-test', baud_rate: 19200,
        model: 'WMR100', usb_serial: '', poll_interval_sec: 60,
      } }));
      await page.route(`**/${kind}/save`, route => {
        saved[kind] = route.request().postDataJSON();
        return route.fulfill({ status: rejected ? 400 : 200, json: rejected
          ? { ok: false, error: 'Invalid console configuration.' } : { ok: true } });
      });
      await page.route(`**/${kind}/disable`, route => route.fulfill({ json: { ok: true } }));
    }
    await page.goto('/');
    await page.getByRole('link', { name: 'Open General Settings' }).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', { name: 'Add Device', exact: true }).click();
    await expect(modal.locator('#pane-add > .integrations-list > details > summary')).toHaveText(['Nodus Device', 'Weather Station']);
    const weather = modal.locator('#add-weather-section');
    await expect(weather.locator('.integration-block > summary')).toHaveText([
      'Ecowitt Gateway', 'AcuRite USB Console', 'Davis Vue USB Console', 'Oregon Scientific USB Console',
    ]);
    if (width < 700) {
      await expect(modal.locator('#add-davis-section > summary')).toBeHidden();
      await weather.locator(':scope > summary').click();
      await modal.getByRole('button', { name: '< Back', exact: true }).click();
    } else {
      await weather.locator(':scope > summary').click();
    }
    await expect(modal.locator('#add-davis-section > summary')).toBeHidden();
    await weather.locator(':scope > summary').click();
    for (const kind of ['davis', 'oregon']) {
      await modal.locator(`#add-${kind}-section > summary`).click();
      if (kind === 'davis') {
        await modal.locator('#davis-port').fill('/dev/cu.usbserial-123');
        await modal.locator('#davis-baud-rate').selectOption('9600');
      } else {
        await modal.locator('#oregon-model').selectOption('WMR88A');
        await modal.locator('#oregon-usb-serial').fill('oregon-123');
      }
      await modal.locator(`#${kind}-poll-interval`).fill('300');
      await modal.locator(`#btn-${kind}-save`).click();
      await expect(modal.locator(`#${kind}-status`)).toContainText('Console saved');
      expect(saved[kind]).toEqual(kind === 'davis'
        ? { port: '/dev/cu.usbserial-123', baud_rate: '9600', poll_interval_sec: '300' }
        : { model: 'WMR88A', usb_serial: 'oregon-123', poll_interval_sec: '300' });
      await modal.locator(`#btn-${kind}-disable`).click();
      await expect(modal.locator(`#${kind}-status`)).toContainText('historical readings retained');
      rejected = true;
      await modal.locator(`#btn-${kind}-save`).click();
      await expect(modal.locator(`#${kind}-status`)).toContainText('Invalid console configuration');
      await expect(modal.locator(`#btn-${kind}-save`)).toBeEnabled();
      rejected = false;
      await page.screenshot({ path: testInfo.outputPath(`${kind}-setup-${width}.png`), fullPage: false });
      if (width < 700) await modal.getByRole('button', { name: '< Back', exact: true }).click();
      else {
        await modal.locator(`#add-${kind}-section > summary`).focus();
        await modal.locator(`#add-${kind}-section > summary`).press('Enter');
      }
      await expect(modal.locator(`#${kind}-poll-interval`)).toBeHidden();
    }
  });
}
