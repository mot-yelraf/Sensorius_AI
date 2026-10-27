import { test, expect } from '@playwright/test';

test('database storage sits below Archive and refreshes only while visible', async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  let failed = false;
  await page.route('**/advanced/status', route => route.fulfill({ json: {} }));
  await page.route('**/advanced/database/status', route => {
    calls++;
    return failed
      ? route.fulfill({ status: 503, json: {} })
      : route.fulfill({ json: { database_size: '1.6 GiB', disk_free: '220.6 GiB' } });
  });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  await page.locator('#setupPiModal').getByRole('button', { name: 'Advanced', exact: true }).click();
  const section = page.locator('#adv-section-database');
  await section.locator(':scope > summary').click();
  await expect(page.locator('#adv-db-size')).toHaveText('1.6 GiB');
  await expect(page.locator('#adv-disk-free')).toHaveText('220.6 GiB');
  await expect(section).not.toContainText('Includes SQLite working files');
  const storage = section.locator('.adv-database-storage');
  const typography = await storage.evaluate(el => {
    const style = getComputedStyle(el);
    return {font: parseFloat(style.fontSize), rows: [...el.children].map(row => ({
      height: row.getBoundingClientRect().height, lineHeight: parseFloat(style.lineHeight),
      width: row.clientWidth, contentWidth: row.scrollWidth,
    }))};
  });
  expect(typography.font).toBeLessThan(14);
  for (const row of typography.rows) {
    expect(row.height).toBeLessThanOrEqual(row.lineHeight + 1);
    expect(row.contentWidth).toBeLessThanOrEqual(row.width + 1);
  }
  const archive = await page.locator('#btn-archive-db').boundingBox();
  const size = await page.locator('#adv-db-size').boundingBox();
  expect(size.y).toBeGreaterThan(archive.y + archive.height);
  expect(size.x).toBeGreaterThanOrEqual(archive.x);
  expect(size.x + size.width).toBeLessThanOrEqual(archive.x + archive.width);
  failed = true;
  await page.clock.fastForward(30000);
  await expect(page.locator('#adv-db-size')).toHaveText('Unavailable');
  await expect(page.locator('#adv-disk-free')).toHaveText('Unavailable');
  await section.locator(':scope > summary').click();
  const closedCalls = calls;
  await page.clock.fastForward(60000);
  expect(calls).toBe(closedCalls);
});
