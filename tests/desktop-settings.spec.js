import { test, expect } from '@playwright/test';

// Screen geometry can disagree with the available desktop window geometry.
for (const screenWidth of [1, 600]) {
  test(`wide desktop settings ignore a ${screenWidth}px screen report`, async ({ browser, baseURL }) => {
    const page = await browser.newPage({
      baseURL,
      viewport: { width: 1280, height: 900 },
      screen: { width: screenWidth, height: 900 },
      hasTouch: false,
      isMobile: false,
    });
    try {
      await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
      await page.goto('/', { waitUntil: 'domcontentloaded' });
      // Confirm the browser really matches the old, faulty screen-only fallback.
      expect(await page.evaluate(() => matchMedia('(max-device-width:700px)').matches)).toBe(true);
      await page.getByRole('link', { name: 'Open General Settings' }).click();
      const general = page.locator('#setupPiModal');
      await expect(general).toBeVisible();
      await expect(general).not.toHaveClass(/(^|\s)mobile-settings(\s|$)/);
      await expect(general.locator('.mobile-settings-nav')).toBeHidden();
      await expect(general.locator('.system-settings-menu')).toBeVisible();
      await expect(general.locator('.system-settings-content')).toBeVisible();
      const display = general.locator('[data-runtime-section="system-display"]');
      await display.locator(':scope > summary').click();
      await expect(display.locator('#metric_set')).toBeVisible();
      await general.getByRole('button', { name: 'Close General Settings' }).click();
      for (const kind of ['Sensor', 'Switch']) {
        await page.evaluate(kind => kind === 'Sensor'
          ? window.editSensorSettings('aht-pr-check')
          : window.editSwitchSettings('switch-pr-check'), kind);
        const root = page.locator(`#${kind.toLowerCase()}SettingsModal`);
        await expect(root).toBeVisible();
        await expect(root).not.toHaveClass(/(^|\s)mobile-settings(\s|$)/);
        await expect(root.locator('.mobile-settings-nav')).toBeHidden();
        await expect(root.locator('.mobile-settings-menu')).toBeVisible();
        await expect(root.locator('.mobile-settings-content')).toBeVisible();
        await expect(root.locator('#location')).toBeVisible();
        await page.goto('/', { waitUntil: 'domcontentloaded' });
      }
      await expect(page.locator('meta[name="viewport"]')).toHaveCount(0);
    } finally {
      await page.close();
    }
  });
}
