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
