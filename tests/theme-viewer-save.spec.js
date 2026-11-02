import { test, expect } from '@playwright/test';

for (const viewer of [
  ...['leaf', 'root', 'leaf-crop', 'flower', 'fruit'].map(theme => ({ path: '/', name: 'Dashboard', section: 'sensorius', prefix: 'dashboard', open: '#dashboardThemeBtn', theme, initial: 'leaf', bodyPrefix: 'dashboard-theme-' })),
  ...['pollinator', 'garden', 'island', 'river', 'desert'].map(theme => ({ path: '/weather-forecast', name: 'Caelus', section: 'caelus', prefix: 'caelus', open: '[data-open-caelus-theme]', theme, initial: 'pollinator', bodyPrefix: 'theme-' })),
  ...['auto', 'garden_tools', 'spring', 'summer', 'autumn', 'winter'].map(theme => ({ path: '/calendar', name: 'Calendar', section: 'biodynamic', prefix: 'calendar', open: '[data-open-calendar-theme]', theme, initial: 'garden_tools', bodyPrefix: 'biodynamic-theme-' })),
]) {
  test(`${viewer.name} viewer saves ${viewer.theme} and keeps previews reversible`, async ({ page }) => {
    await page.goto(viewer.path);
    const view = page.locator(`#${viewer.prefix}ThemeView`);
    const open = async () => {
      await page.locator(viewer.open).first().click();
      await expect(view).toBeVisible();
    };
    const select = () => page.locator(`[data-${viewer.prefix}-preview-theme="${viewer.theme}"]`).click();
    await open();
    await select();
    await page.locator(`#close${viewer.name}ThemeBtn`).click();
    await expect(page.locator('body')).toHaveClass(new RegExp(`\\b${viewer.bodyPrefix}${viewer.initial}\\b`));
    await open();
    await select();
    await page.route('**/api/themes/selection', route => route.fulfill({ status: 500, json: { ok: false, error: 'Please retry saving.' } }));
    await page.locator(`#save${viewer.name}ThemeBtn`).click();
    await expect(page.locator(`#${viewer.prefix}ThemeSaveStatus`)).toHaveText('Please retry saving.');
    await expect(view).toBeVisible();
    await page.route('**/api/themes/selection', async route => {
      const form = new URLSearchParams(route.request().postData());
      expect(form.get('section')).toBe(viewer.section);
      expect(form.get('theme')).toBe(viewer.theme === 'leaf-crop' ? 'leaf_crop' : viewer.theme);
      await route.fulfill({ json: { ok: true, theme: viewer.theme === 'leaf-crop' ? 'leaf_crop' : viewer.theme } });
    });
    await page.locator(`#save${viewer.name}ThemeBtn`).click();
    await expect(view).toBeHidden();
    if (viewer.section === 'sensorius') {
      const colors = {leaf: 'rgb(196, 220, 248)', root: 'rgb(239, 226, 198)', 'leaf-crop': 'rgb(220, 239, 212)', flower: 'rgb(204, 204, 255)', fruit: 'rgb(253, 225, 211)'};
      await expect(page.locator('.metric-container').first()).toHaveCSS('background-color', colors[viewer.theme]);
    }
    const resolved = viewer.theme === 'auto' ? 'autumn' : viewer.theme;
    await expect(page.locator('body')).toHaveClass(new RegExp(`\\b${viewer.bodyPrefix}${resolved}\\b`));
    await open();
    await expect(page.locator(`[data-${viewer.prefix}-preview-theme="${viewer.theme}"]`)).toHaveAttribute('aria-pressed', 'true');
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator(`#save${viewer.name}ThemeBtn`)).toBeInViewport();
    const toolbar = await page.locator(`.${viewer.prefix}-theme-toolbar`).boundingBox();
    expect(toolbar.x).toBeGreaterThanOrEqual(0);
    expect(toolbar.x + toolbar.width).toBeLessThanOrEqual(390);
    await page.keyboard.press('Escape');
    await expect(view).toBeHidden();
    await expect(page.locator('body')).toHaveClass(new RegExp(`\\b${viewer.bodyPrefix}${resolved}\\b`));
  });
}

async function openDashboardDisplay(page) {
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  const root = page.locator('#setupPiModal');
  await root.getByRole('button', { name: 'General Settings', exact: true }).click();
  const display = root.locator('[data-runtime-section="system-display"]');
  await display.locator(':scope > summary').click();
  await display.locator('[data-runtime-section="display-dashboard-theme"] > summary').click();
  return { root, display };
}

for (const custom of [false, true]) {
  test(`Display theme save updates tile palette from ${custom ? 'custom' : 'default'} theme`, async ({ page }) => {
    await page.goto('/');
    if (custom) {
      await page.evaluate(() => {
        document.body.classList.replace('dashboard-theme-leaf', 'dashboard-theme-custom');
        document.body.setAttribute('style', '--dashboard-card-bg: #123456; --dashboard-card-border: #654321; --dashboard-card-text: #ffffff; background-image: none');
      });
    }
    const { display } = await openDashboardDisplay(page);
    await page.route('**/submit-pi-setup', route => route.fulfill({ json: { ok: true } }));
    await display.locator('input[name="dashboard_background_theme"][value="fruit"]').check();
    await display.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('body')).toHaveClass(/\bdashboard-theme-fruit\b/);
    await expect(page.locator('.metric-container').first()).toHaveCSS('background-color', 'rgb(253, 225, 211)');
    await expect(page.locator('.metric-container').first()).toHaveCSS('color', 'rgb(59, 28, 18)');
    await expect(page.locator('.metric-container').first()).toHaveCSS('border-top-color', 'rgb(184, 106, 74)');
  });
}

test('viewer save synchronizes Display selection and allows switching back', async ({ page }) => {
  await page.goto('/');
  await page.route('**/api/themes/selection', route => route.fulfill({ json: { ok: true } }));
  await page.locator('#dashboardThemeBtn').click();
  await page.locator('[data-dashboard-preview-theme="root"]').click();
  await page.locator('#saveDashboardThemeBtn').click();
  await expect(page.locator('.metric-container').first()).toHaveCSS('background-color', 'rgb(239, 226, 198)');
  const { display } = await openDashboardDisplay(page);
  await expect(display.locator('input[name="dashboard_background_theme"][value="root"]')).toBeChecked();
  await page.route('**/submit-pi-setup', route => route.fulfill({ json: { ok: true } }));
  await display.locator('input[name="dashboard_background_theme"][value="leaf"]').check();
  await display.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('.metric-container').first()).toHaveCSS('background-color', 'rgb(196, 220, 248)');
  await page.locator('#setupPiModal').getByRole('button', { name: 'Close General Settings' }).click();
  await page.locator('#dashboardThemeBtn').click();
  await page.locator('[data-dashboard-preview-theme="fruit"]').click();
  await page.locator('#saveDashboardThemeBtn').click();
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  await expect(display.locator('input[name="dashboard_background_theme"][value="fruit"]')).toBeChecked();
  await display.locator('input[name="dashboard_background_theme"][value="leaf"]').check();
  await display.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('.metric-container').first()).toHaveCSS('background-color', 'rgb(196, 220, 248)');
});
