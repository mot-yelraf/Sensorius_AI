import { test, expect } from '@playwright/test';

for (const scenario of [
  { name: 'Pi browser', pi: true, bridge: null, late: false, optimized: false },
  { name: 'non-Pi GTK', pi: false, bridge: 'gtkwebkit2', late: false, optimized: false },
  { name: 'Pi Cocoa', pi: true, bridge: 'cocoa', late: false, optimized: false },
  { name: 'Pi GTK early bridge', pi: true, bridge: 'gtkwebkit2', late: false, optimized: true },
  { name: 'Pi GTK late bridge', pi: true, bridge: 'gtkwebkit2', late: true, optimized: true },
]) {
  test(`${scenario.name} scopes metric shadow optimization correctly`, async ({ page }) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    await page.route('**/', async route => {
      const response = await route.fetch();
      const body = (await response.text()).replace(
        /(<script\b[^>]*\bid=['"]dashboard-config['"][^>]*>)([\s\S]*?)(<\/script>)/,
        (_, opening, json, closing) => opening + JSON.stringify({ ...JSON.parse(json), isPiPlatform: scenario.pi }) + closing,
      );
      await route.fulfill({ response, body });
    });
    if (scenario.bridge && !scenario.late) {
      await page.addInitScript(platform => { window.pywebview = { platform }; }, scenario.bridge);
    }
    await page.goto('/');
    await page.waitForFunction(() => typeof window.applySwitchTileStyles === 'function');
    if (scenario.late) {
      await expect(page.locator('html')).not.toHaveClass(/native-pi-window/);
      await page.evaluate(platform => {
        window.pywebview = { platform };
        window.dispatchEvent(new Event('pywebviewready'));
      }, scenario.bridge);
    }
    await expect.poll(() => page.locator('html').evaluate(el => el.classList.contains('native-pi-window'))).toBe(scenario.optimized);
    const styles = await page.evaluate(() => {
      const results = [];
      for (const tileStyle of ['Theme', 'Chrome', 'Brass', 'Mahogany & Brass']) {
        for (const className of ['metric-container', 'switch-metric-container']) {
          const card = document.createElement('div');
          card.className = className;
          card.dataset.tileStyle = tileStyle;
          card.style.boxShadow = '0 2px 6px red';
          card.innerHTML = '<div class="probe-shadow" style="box-shadow:0 2px 6px red;border-radius:9px;background-image:linear-gradient(red,blue)">Value</div>';
          document.body.appendChild(card);
          const child = card.firstElementChild;
          const childStyle = getComputedStyle(child);
          results.push({ tileStyle, card: getComputedStyle(card).boxShadow, child: childStyle.boxShadow, radius: childStyle.borderRadius, background: childStyle.backgroundImage });
          card.remove();
        }
      }
      return results;
    });
    for (const style of styles) {
      expect(style.card === 'none', `${style.tileStyle} card`).toBe(scenario.optimized);
      expect(style.child === 'none', `${style.tileStyle} child`).toBe(scenario.optimized);
      expect(style.radius).toBe('9px');
      expect(style.background).toContain('linear-gradient');
    }
  });
}
