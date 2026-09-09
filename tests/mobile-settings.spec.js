import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test.beforeEach(async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
});

async function openGeneral(page) {
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  await expect(page.locator('#setupPiModal')).toBeVisible();
  return page.locator('#setupPiModal');
}

async function assertFits(root) {
  expect(await root.evaluate(element => {
    const body = element.querySelector('.system-settings-body, .modal-body');
    return body.scrollWidth <= body.clientWidth + 1;
  })).toBe(true);
}

test('mobile General Settings drills down, preserves edits, and saves within the section', async ({ page }) => {
  const dashboardWidth = await page.evaluate(() => innerWidth);
  const root = await openGeneral(page);
  const back = root.getByRole('button', { name: '< Back', exact: true });
  await expect(root.locator('.system-settings-content')).toBeHidden();
  await expect(back).toBeHidden();
  await root.getByRole('button', { name: 'General Settings', exact: true }).tap();
  await expect(root.locator('#systemSettingsMenu')).toBeHidden();
  await expect(root.locator('#astral_lat')).toBeHidden();
  await root.locator('summary').filter({ hasText: /^Astral$/ }).tap();
  await root.locator('#astral_lat').fill('39.75');
  await expect(root.locator('summary').filter({ hasText: /^Display$/ })).toBeHidden();
  await assertFits(root);
  await back.tap();
  await root.locator('summary').filter({ hasText: /^Astral$/ }).tap();
  await expect(root.locator('#astral_lat')).toHaveValue('39.75');
  let saved;
  await page.route('**/submit-pi-setup', async route => {
    saved = route.request().postData();
    await route.fulfill({ json: { status: 'ok' } });
  });
  await root.locator('[data-runtime-section="system-astral"]').getByRole('button', { name: 'Save', exact: true }).tap();
  await expect.poll(() => saved).toContain('39.75');
  await back.tap();
  await back.tap();
  await expect(root.locator('#systemSettingsMenu')).toBeVisible();
  await root.getByRole('button', { name: 'Edit Locations', exact: true }).tap();
  await expect(root.locator('#pane-locations')).toBeVisible();
  await back.tap();
  await root.getByRole('button', { name: 'Close General Settings' }).tap();
  await expect.poll(() => page.evaluate(() => innerWidth)).toBe(dashboardWidth);
  await openGeneral(page);
  await expect(root.locator('.system-settings-content')).toBeHidden();
});

test('General Settings section menus fit a narrow phone screen', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 });
  const root = await openGeneral(page);
  for (const menuName of ['General Settings', 'Integrations', 'Advanced', 'Add Device']) {
    await root.getByRole('button', { name: menuName, exact: true }).tap();
    const summaries = await root.locator('.settings-pane:not([hidden]) details > summary:visible').all();
    for (const summary of summaries) {
      await summary.tap();
      await assertFits(root);
      const back = root.getByRole('button', { name: '< Back', exact: true });
      await expect(back).toBeInViewport();
      await back.tap();
    }
    await root.getByRole('button', { name: '< Back', exact: true }).tap();
  }
});

test('nested theme views retain the Display Save button and edited values', async ({ page }, testInfo) => {
  const root = await openGeneral(page);
  await root.getByRole('button', { name: 'General Settings', exact: true }).tap();
  await root.locator('summary').filter({ hasText: /^Display$/ }).tap();
  await root.locator('#metric_set').selectOption('All');
  await root.locator('summary').filter({ hasText: /^Sensorius Dashboard Theme$/ }).tap();
  await expect(root.locator('#metric_set')).toBeHidden();
  await expect(root.locator('[data-runtime-section="system-display"]').getByRole('button', { name: 'Save', exact: true })).toBeVisible();
  await root.locator('input[name="dashboard_background_theme"][value="flower"]').check();
  await assertFits(root);
  await page.screenshot({ path: testInfo.outputPath('mobile-theme.png') });
  await root.getByRole('button', { name: '< Back', exact: true }).tap();
  await expect(root.locator('#metric_set')).toHaveValue('All');
  await expect(root.locator('input[name="dashboard_background_theme"][value="flower"]')).toBeChecked();
  await root.getByRole('button', { name: '< Back', exact: true }).tap();
  await root.getByRole('button', { name: '< Back', exact: true }).tap();
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(root.locator('.system-settings-content')).toBeHidden();
  await page.screenshot({ path: testInfo.outputPath('mobile-settings-menu.png') });
});

test('desktop settings retain their side menu and expansion state across resize', async ({ browser, baseURL }) => {
  const page = await browser.newPage({ baseURL, viewport: { width: 1280, height: 900 } });
  try {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const root = await openGeneral(page);
    const display = root.locator('[data-runtime-section="system-display"]');
    await expect(root.locator('.mobile-settings-nav')).toBeHidden();
    await display.locator(':scope > summary').click();
    await root.locator('#metric_set').selectOption('All');
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(root.locator('.system-settings-content')).toBeHidden();
    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(root.locator('#systemSettingsMenu')).toBeVisible();
    await expect(display).toHaveAttribute('open');
    await expect(root.locator('#metric_set')).toHaveValue('All');
  } finally {
    await page.close();
  }
});

for (const kind of ['Sensor', 'Switch']) {
  test(`${kind} Settings uses the same menu and Back flow`, async ({ page }, testInfo) => {
    await page.evaluate(kind => {
      if (kind === 'Sensor') return window.editSensorSettings('aht-pr-check');
      return window.editSwitchSettings('switch-pr-check');
    }, kind);
    const root = page.locator(`#${kind.toLowerCase()}SettingsModal`);
    await expect(root.locator('.mobile-settings-content')).toBeHidden();
    await root.getByRole('button', { name: `${kind} Settings`, exact: true }).tap();
    await root.locator('#location').fill('South Greenhouse');
    await assertFits(root);
    await page.screenshot({ path: testInfo.outputPath(`mobile-${kind.toLowerCase()}-settings.png`) });
    await root.getByRole('button', { name: '< Back', exact: true }).tap();
    await root.getByRole('button', { name: `${kind} Info`, exact: true }).tap();
    await expect(root.locator('#location')).toBeHidden();
    await root.getByRole('button', { name: '< Back', exact: true }).tap();
    await root.getByRole('button', { name: `${kind} Settings`, exact: true }).tap();
    await expect(root.locator('#location')).toHaveValue('South Greenhouse');
  });
}
