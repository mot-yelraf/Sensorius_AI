import { test, expect } from '@playwright/test';

const sid = 'ecowitt-aabbccddeeff-ac1100-000008d1';
const discovery = {
  gateway_url: 'http://gateway.test', gateway_model: 'GW1200A_V1.0.0',
  inventory: [{ id: 'AB12', name: 'Weather array', reporting: true }],
  smart_plugs: [{ id: 2257, model: 2, online: true }, { id: 2258, model: 2, online: false }],
  live_metric_count: 9, poll_interval_sec: 300, smart_plug_interval_sec: 30,
};

test('GW1200 discovery separates sensors and plugs and saves query interval', async ({ page }, testInfo) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.route('**/ecowitt/status', route => route.fulfill({ json: { ...discovery, label: 'Online' } }));
  await page.route('**/ecowitt/discover', route => route.fulfill({ json: discovery }));
  let saved;
  await page.route('**/ecowitt/save', route => {
    saved = route.request().postDataJSON();
    return route.fulfill({ json: { ...discovery, smart_plug_interval_sec: 15 } });
  });
  await page.goto('/');
  await page.getByRole('link', { name: 'Open General Settings' }).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', { name: 'Add Device', exact: true }).click();
  await modal.locator('summary').filter({ hasText: /^Ecowitt Gateway$/ }).click();
  await modal.getByRole('button', { name: 'Find Devices', exact: true }).click();
  await expect(modal.getByText('Discovered GW Sensors', { exact: true })).toBeVisible();
  await expect(modal.getByText('Discovered GW Smart Plugs', { exact: true })).toBeVisible();
  await expect(modal.locator('#ecowitt-smart-plug-list [role="listitem"]')).toHaveCount(2);
  await expect(modal.locator('#ecowitt-smart-plug-list')).toContainText('2258 — offline');
  await modal.locator('#ecowitt-smart-plug-interval').fill('15');
  await modal.getByRole('button', { name: 'Save Gateway', exact: true }).click();
  await expect.poll(() => saved?.smart_plug_interval_sec).toBe('15');
  await page.screenshot({ path: testInfo.outputPath('ecowitt-discovery.png'), fullPage: true });
});

test('AC1100 uses switch card, canonical toggle key, and event list', async ({ page }, testInfo) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  let offline = false;
  await page.route('**/switch-status-update', route => route.fulfill({ json: offline ? {
    [`${sid}-1::Plug`]: { state: true, online: false, confirmed: true, error: 'AC1100 is offline at the gateway.' },
  } : {} }));
  let toggleUrl;
  await page.route('**/switch/toggle?**', route => {
    toggleUrl = new URL(route.request().url());
    return route.fulfill({ json: { state: true, switch_id: sid, label: 'Plug', time: '', events: ['On 2026-09-10 12:00:00 (manual)'] } });
  });
  await page.goto('/ac1100-fixture');
  const toggle = page.locator(`button[data-switch-id="${sid}"][data-switch-name="Plug"]`);
  await expect(toggle).toHaveText('Off');
  await expect(page.locator('.ecowitt-plug-status')).toHaveText('Online');
  await expect(page.locator('h3').filter({ hasText: 'Greenhouse' })).toBeVisible();
  await toggle.click();
  await expect(toggle).toHaveText('On');
  expect(toggleUrl.searchParams.get('switch_key')).toBe(`${sid}-1::Plug`);
  await expect(page.locator('.switch-events-list li')).toHaveAttribute('data-raw-event', 'On 2026-09-10 12:00:00 (manual)');
  await expect(page.locator('.switch-events-list li')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('ac1100-switch.png'), fullPage: true });
  offline = true;
  await expect(page.locator('.ecowitt-plug-status')).toHaveText('Offline');
  await expect(toggle).toHaveText('On');
});
