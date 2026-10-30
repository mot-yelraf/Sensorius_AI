import { test, expect } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`switch tile finishes refresh and preserve event controls at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
    let finish = 'Theme';
    await page.route('**/*json_only=true*', async route => route.fulfill({ json: {
      available: [], values: {}, stats: {}, timestamps: {},
      available_switches: ['switch-pr-check'], renderable_switches_view: ['switch-pr-check'],
      switch_tile_style_map: await page.locator('.switch-metric-container').evaluateAll((cards, style) => Object.fromEntries(cards.map(card => [card.dataset.switchLocation, style])), finish),
    } }));
    await page.route('**/switch-status-update', route => route.fulfill({ json: {} }));
    await page.route('**/switch/history?*', route => route.fulfill({ json: { events: [
      { state: false, epoch: Date.now() / 1000 - 7200, source: 'manual' },
      { state: true, epoch: Date.now() / 1000 - 3600, source: 'manual' },
    ] } }));
    await page.goto('/?switches=true', { waitUntil: 'load' });
    const card = page.locator('.switch-metric-container');
    await expect(card).toHaveAttribute('data-tile-style', 'Theme');
    for (const style of ['Theme', 'Chrome', 'Brass', 'Mahogany & Brass']) {
      finish = style;
      await page.evaluate(() => window.updateGauges({ ignoreVisibility: true, ignoreModal: true }));
      await expect(card).toHaveAttribute('data-tile-style', style);
      if (style !== 'Theme') {
        expect(await card.evaluate(el => getComputedStyle(el).backgroundImage)).toContain('gradient');
        expect(await card.locator('.switch-view').evaluate(el => getComputedStyle(el).paddingTop)).toBe('7px');
        expect(await card.locator('.switch-events').evaluate(el => getComputedStyle(el).borderTopWidth)).toBe('4px');
        expect(await card.locator('.switch-events').evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
      }
      const plot = card.locator('.switch-history-plot');
      await expect(plot).toHaveAttribute('data-hours', '24');
      for (const hours of ['6', '1', '24']) {
        await plot.click();
        await expect(plot).toHaveAttribute('data-hours', hours);
      }
      const cardBox = await card.boundingBox();
      if (style !== 'Theme') {
        expect(cardBox.x).toBeGreaterThanOrEqual(0);
        expect(cardBox.x + cardBox.width).toBeLessThanOrEqual(width + 1);
      }
      await card.screenshot({ path: testInfo.outputPath(`switch-${style.replace(/[^a-z]/gi, '-')}.png`) });
    }
    await page.evaluate(() => window.editSwitchSettings('switch-pr-check'));
    if (width < 700) {
      await page.locator('#switchSettingsModal').getByRole('button', { name: 'Switch Settings', exact: true }).click();
    }
    const select = page.getByLabel('Style for all switches at this location');
    await expect(select).toBeVisible();
    await expect(select.locator('option')).toHaveText(['Theme', 'Chrome', 'Brass', 'Mahogany & Brass']);
    await select.selectOption('Mahogany & Brass');
    await expect(select).toHaveValue('Mahogany & Brass');
    const selectStyle = await select.evaluate(el => {
      const style = getComputedStyle(el);
      const context = document.createElement("canvas").getContext("2d");
      context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
      const available = el.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
      return { appearance: style.appearance, height: el.getBoundingClientRect().height, lineHeight: style.lineHeight, textFits: context.measureText(el.selectedOptions[0].textContent.trim()).width <= available };
    });
    expect(selectStyle.appearance).toBe('none');
    expect(selectStyle.textFits).toBe(true);
    expect(selectStyle.height).toBeGreaterThanOrEqual(32);
    expect(Number.parseFloat(selectStyle.lineHeight)).toBeGreaterThan(18);
    await page.locator('#switchSettingsModal').screenshot({ path: testInfo.outputPath('switch-style-setting.png') });
    expect(errors).toEqual([]);
  });
}
