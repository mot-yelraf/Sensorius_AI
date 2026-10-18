import { test, expect } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`orders dashboard rows and switch cards from the dialog and dashboard at ${width}px`, async ({ browser, baseURL }, testInfo) => {
    const page = await browser.newPage({ baseURL, viewport: { width, height: 900 } });
    try {
      await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
      await page.goto('/?switches=true', { waitUntil: 'domcontentloaded' });
      await page.evaluate(() => {
        const first = document.querySelector('#row_switches > .switch-metric-container');
        const second = first.cloneNode(true);
        second.dataset.switchLocation = 'garden';
        second.dataset.switchIds = 'switch-garden';
        second.querySelectorAll('[id]').forEach(element => element.removeAttribute('id'));
        second.querySelector('.switch-device-title').textContent = 'SWITCH-GARDEN Garden';
        first.parentElement.appendChild(second);
      });
      let verticalOrder;
      let switchOrder;
      await page.route('**/dashboard/metric-position', async route => {
        const ids = await page.locator('.dashboard-content > .sensor-group[data-dashboard-row-id]').evaluateAll(elements => elements.map(element => element.dataset.dashboardRowId));
        const body = route.request().postDataJSON();
        const index = ids.indexOf(body.sensor_id);
        const next = index + (body.direction === 'up' ? -1 : 1);
        [ids[index], ids[next]] = [ids[next], ids[index]];
        verticalOrder = ids;
        await route.fulfill({ json: { moved: true, order: ids } });
      });
      await page.route('**/dashboard/switch-order', async route => {
        switchOrder = route.request().postDataJSON().order;
        await route.fulfill({ json: { status: 'ok', order: switchOrder } });
      });
      await page.getByRole('link', { name: 'Open General Settings' }).click();
      const general = page.locator('#setupPiModal');
      if (width < 700) await general.getByRole('button', { name: 'General Settings', exact: true }).click();
      await general.locator('[data-runtime-section="system-display"] > summary').click();
      const order = general.locator('.dashboard-order-section');
      await order.locator('summary').click();
      const rows = order.locator('#dashboard-row-order-list');
      await expect(rows.getByRole('listitem')).toHaveCount(2);
      await rows.getByRole('listitem').last().getByRole('button', { name: /up$/ }).click();
      await expect.poll(() => verticalOrder?.[0]).toBe('__switches__');
      await expect(rows.getByRole('listitem').first()).toHaveAttribute('data-row-id', '__switches__');
      await expect(order.locator('#sensor_row_order')).toHaveValue('existing');
      const cards = order.locator('#dashboard-switch-order-list');
      await expect(cards.getByRole('listitem')).toHaveCount(2);
      await cards.getByRole('listitem').last().getByRole('button', { name: /up$/ }).click();
      await expect.poll(() => switchOrder?.[0]).toBe('garden');
      await expect(cards.getByRole('listitem').first()).toHaveAttribute('data-row-id', 'garden');
      await expect(order.locator('#switch_row_order')).toHaveValue('existing');
      expect(await order.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`dashboard-order-${width}.png`), fullPage: true });
      await general.getByRole('button', { name: 'Close General Settings' }).click();
      const groups = page.locator('.dashboard-content > .sensor-group[data-dashboard-row-id]');
      const switches = page.locator('#group_switches');
      const up = switches.getByRole('button', { name: 'Move Switches row up', exact: true });
      const down = switches.getByRole('button', { name: 'Move Switches row down', exact: true });
      await expect(up).toBeDisabled();
      await expect(down).toBeEnabled();
      await expect(up).toHaveText('↑');
      await expect(down).toHaveText('↓');
      await expect(down).toHaveCSS('color', 'rgb(34, 89, 242)');
      const upBox = await up.boundingBox();
      const downBox = await down.boundingBox();
      expect(upBox.x + upBox.width).toBeCloseTo(downBox.x, 0);
      expect(downBox.x - upBox.x).toBeCloseTo(17, 0);
      expect(upBox.y).toBe(downBox.y);
      await down.click();
      await expect(groups.last()).toHaveAttribute('data-dashboard-row-id', '__switches__');
      await expect(down).toBeDisabled();
      await expect(up).toBeEnabled();
      await expect(groups.first().locator('[data-move="up"]')).toBeDisabled();
      await expect(groups.first().locator('[data-move="down"]')).toBeEnabled();
      await up.click();
      await expect(groups.first()).toHaveAttribute('data-dashboard-row-id', '__switches__');
      await expect(up).toBeDisabled();
      await expect(down).toBeEnabled();
      await page.screenshot({ path: testInfo.outputPath(`row-order-arrows-${width}.png`), fullPage: true });
      const garden = page.locator('#row_switches > [data-switch-location="garden"]');
      const left = garden.getByRole('button', { name: /switch card left$/ });
      const right = garden.getByRole('button', { name: /switch card right$/ });
      await expect(left).toBeDisabled();
      await expect(right).toBeEnabled();
      await expect(right).toHaveText('→');
      await expect(left).toHaveText('←');
      await expect(right).toHaveCSS('color', 'rgb(34, 89, 242)');
      expect(await right.evaluate(element => element.closest('.switch-card-title-row').contains(element.parentElement.querySelector('.switch-device-title')))).toBe(true);
      await right.click();
      await expect.poll(() => switchOrder?.[0]).toBe('test room');
      await expect(page.locator('#row_switches > .switch-metric-container').first()).toHaveAttribute('data-switch-location', 'test room');
      await expect(left).toBeEnabled();
      await expect(right).toBeDisabled();
      await page.screenshot({ path: testInfo.outputPath(`switch-order-arrows-${width}.png`), fullPage: true });
      await page.route('**/dashboard/switch-order', route => route.fulfill({ status: 500, json: { error: 'test' } }));
      await garden.getByRole('button', { name: /switch card left$/ }).click();
      await expect(page.locator('#row_switches > .switch-metric-container').first()).toHaveAttribute('data-switch-location', 'test room');
    } finally {
      await page.close();
    }
  });
}
