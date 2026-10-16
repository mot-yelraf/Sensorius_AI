import { test, expect } from '@playwright/test';

const sid = 'ecowitt-aabbccddeeff-ac1100-000008d1';
const key = `${sid}-1::Plug`;
const now = new Date('2026-10-16T18:00:00Z');
const at = minutesAgo => new Date(now.getTime() - minutesAgo * 60000).toISOString();

test.beforeEach(async ({ page }) => {
  await page.clock.install({ time: now });
  await page.clock.pauseAt(now);
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
});

async function liveSocket(page) {
  let socket;
  await page.routeWebSocket('**/ws/switch-updates', ws => { socket = ws; });
  return async message => {
    await expect.poll(() => Boolean(socket)).toBe(true);
    socket.send(JSON.stringify(message));
  };
}

async function openSwitch(page, events = [], state = false) {
  await page.route('**/switch-status-update', route => route.fulfill({ json: {
    [key]: { state, confirmed: true, online: true, events: events.slice(-10) },
  } }));
  await page.route('**/switch/history?**', route => {
    expect(new URL(route.request().url()).searchParams.get('switch_key')).toBe(key);
    return route.fulfill({ json: { events: events.map(line => ({
      state: line.startsWith('On'),
      epoch: Date.parse(line.replace(/^(On|Off)\s+/, '').split(' (')[0].replace(' ', 'T')) / 1000,
      source: (line.match(/\(([^)]+)\)/) || [])[1] || '',
    })) } });
  });
  await page.goto('/ac1100-fixture');
  return page.locator('.switch-state-slider');
}

test('transition plot uses green On and black Off lines and cycles 24hr > 6hr > 1hr > 24hr', async ({ page }, testInfo) => {
  await openSwitch(page, [
    `On ${at(1200)} (manual)`, `Off ${at(720)} (auto - humidity)`, `On ${at(120)} (manual)`,
  ], true);
  const plot = page.locator('.switch-history-plot');
  await expect(plot).toHaveAttribute('data-hours', '24');
  await expect(plot).toHaveAttribute('data-event-count', '3');
  await expect(page.locator('.switch-history-ticks > span')).toHaveCount(3);
  const fullDayStart = await page.locator('.switch-history-ticks > span').first().getAttribute('title');
  expect(await plot.evaluate(node => node.clientHeight)).toBeLessThanOrEqual(32);
  await expect(page.locator('.switch-history-states')).toHaveText('OnOff');
  const colors = await plot.locator('canvas').evaluate(canvas => {
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let green = 0, black = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] && pixels[i + 1] > pixels[i] * 2 && pixels[i + 1] > pixels[i + 2] * 2) green++;
      if (pixels[i + 3] && pixels[i] === 0 && pixels[i + 1] === 0 && pixels[i + 2] === 0) black++;
    }
    return { green, black };
  });
  expect(colors.green).toBeGreaterThan(0);
  expect(colors.black).toBeGreaterThan(0);
  await expect(page.locator('.switch-history-detail')).toHaveText(/^On: .+ · manual$/);
  await expect(page.locator('.switch-history-detail')).not.toContainText('Green =');
  const latestDetail = await page.locator('.switch-history-detail').textContent();
  await plot.hover({ position: { x: 28, y: 10 } });
  await expect(plot).toHaveAttribute('title', /^On: .+ · manual$/);
  await expect(page.locator('.switch-history-detail')).toHaveText(latestDetail);
  await expect(page.locator('.switch-history-detail')).toContainText('On:');
  await expect(page.locator('.switch-history-detail')).toContainText('manual');
  await page.screenshot({ path: testInfo.outputPath('switch-24hr.png'), fullPage: true, animations: 'disabled' });
  await plot.click();
  await expect(plot).toHaveAttribute('data-hours', '6');
  expect(await page.locator('.switch-history-ticks > span').first().getAttribute('title')).not.toBe(fullDayStart);
  await expect(plot).toHaveAttribute('data-event-count', '1');
  await plot.click();
  await expect(plot).toHaveAttribute('data-hours', '1');
  await expect(plot).toHaveAttribute('data-event-count', '0');
  await expect(page.locator('.switch-history-detail')).toHaveText(latestDetail);
  await plot.click();
  await expect(plot).toHaveAttribute('data-hours', '24');
  await expect(plot).toHaveAttribute('data-event-count', '3');
});

test('history includes all transitions beyond the recent snapshot and merges live origins', async ({ page }) => {
  const send = await liveSocket(page);
  await openSwitch(page, Array.from({ length: 12 }, (_, index) => `${index % 2 ? 'Off' : 'On'} ${at(120 - index * 10)}`));
  const plot = page.locator('.switch-history-plot');
  await expect(plot).toHaveAttribute('data-event-count', '12');
  await send({ type: 'switch_event', key, ui_key: key, state: true, timestamp: at(5), source: 'mqtt-manual' });
  await expect(plot).toHaveAttribute('data-event-count', '13');
  await page.route('**/switch-status-update', route => route.fulfill({ json: { [key]: { state: true, events: [`On ${at(5)}`] } } }));
  await page.clock.fastForward(60000);
  await expect(plot).toHaveAttribute('data-event-count', '13');
  await plot.focus();
  for (let i = 0; i < 13; i++) await page.keyboard.press('ArrowRight');
  await expect(page.locator('.switch-history-detail')).toContainText('manual');
  await page.keyboard.press('Enter');
  await expect(plot).toHaveAttribute('data-hours', '6');
});

test('history respects UTC offsets and selected-window boundaries', async ({ page }) => {
  await openSwitch(page, [
    'On 2026-10-16 11:00:00-06:00 (manual)',
    'Off 2026-10-16T16:00:00Z (auto)',
    `Off ${at(1441)}`,
  ], true);
  const plot = page.locator('.switch-history-plot');
  await expect(plot).toHaveAttribute('data-event-count', '2');
  await plot.focus();
  await page.keyboard.press('ArrowRight');
  await expect(plot).toHaveAttribute('title', /^Off: .+ · auto$/);
  await expect(page.locator('.switch-history-detail')).toHaveText(/^On: .+ · manual$/);
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('.switch-history-detail')).toContainText('On:');
  await plot.click();
  await plot.click();
  await expect(plot).toHaveAttribute('data-hours', '1');
  await expect(plot).toHaveAttribute('data-event-count', '1');
  await page.clock.fastForward(60000);
  await expect(plot).toHaveAttribute('data-event-count', '0');
});

test('empty history and history failure are distinguishable', async ({ page }, testInfo) => {
  await openSwitch(page);
  const plot = page.locator('.switch-history-plot');
  await expect(page.locator('.switch-history-detail')).toHaveText('No recorded state');
  await page.screenshot({ path: testInfo.outputPath('switch-off-empty.png'), fullPage: true, animations: 'disabled' });
  await page.route('**/switch/history?**', route => route.fulfill({ status: 500, json: { error: 'Database unavailable' } }));
  await page.clock.fastForward(60000);
  await expect(page.locator('.switch-history-detail')).toContainText('History refresh failed');
  await expect(plot).toHaveAttribute('data-event-count', '0');
});

test('latest state keeps the rule name across windows, hover, refresh, and live updates', async ({ page }) => {
  const send = await liveSocket(page);
  await openSwitch(page, [
    `On ${at(1200)} (manual)`, `Off ${at(720)} (auto/rule:Humidity: high/mqtt)`,
  ]);
  const plot = page.locator('.switch-history-plot');
  const detail = page.locator('.switch-history-detail');
  await expect(detail).toHaveText(/^Off: .+ · auto - Humidity: high$/);
  const latest = await detail.textContent();
  await plot.hover({ position: { x: 1, y: 10 } });
  await expect(detail).toHaveText(latest);
  await plot.click();
  await expect(plot).toHaveAttribute('data-event-count', '0');
  await expect(detail).toHaveText(latest);
  await page.clock.fastForward(60000);
  await expect(detail).toHaveText(latest);
  await send({ type: 'switch_event', key, ui_key: key, state: true, timestamp: at(30), source: 'mqtt-auto:Humidity: high' });
  await expect(detail).toHaveText(/^On: .+ · auto - Humidity: high$/);
  await send({ type: 'switch_event', key, ui_key: key, state: true, timestamp: at(0), source: 'mqtt-manual' });
  await expect(detail).toHaveText(/^On: .+ · manual$/);
  await page.route('**/switch/history?**', route => route.fulfill({ status: 500 }));
  await page.clock.fastForward(60000);
  await expect(detail).toHaveText(/^On: .+ · manual$/);
  await expect(detail).toHaveAttribute('title', /History refresh failed/);
});

test('slider waits for success, prevents duplicate commands, and preserves state on failure', async ({ page }) => {
  const slider = await openSwitch(page);
  await expect(slider).toHaveAttribute('aria-checked', 'false');
  expect(await slider.evaluate(node => node.getBoundingClientRect().width)).toBeLessThanOrEqual(96);
  let release;
  let requests = 0;
  const ready = new Promise(resolve => { release = resolve; });
  await page.route('**/switch/toggle?**', async route => {
    requests += 1;
    expect(new URL(route.request().url()).searchParams.get('switch_key')).toBe(key);
    await ready;
    await route.fulfill({ json: { state: true, confirmed: true, events: [`On ${at(0)} (manual)`] } });
  });
  await slider.focus();
  await page.keyboard.press('Space');
  await expect(slider).toBeDisabled();
  await expect(slider).toHaveText('Off');
  await page.evaluate(() => window.toggleSwitchInline(document.querySelector('.switch-state-slider')));
  expect(requests).toBe(1);
  release();
  await expect(slider).toHaveText('On');
  await expect(slider).toHaveAttribute('aria-checked', 'true');
  await expect(slider).toBeEnabled();
  await page.route('**/switch/toggle?**', route => route.fulfill({ status: 500, json: { error: 'Failed' } }));
  await slider.click();
  await expect(slider).toBeEnabled();
  await expect(slider).toHaveText('On');
  await expect(slider).toHaveAttribute('aria-checked', 'true');
});

test('automation blocks manual commands and unknown status is not presented as Off', async ({ page }) => {
  const send = await liveSocket(page);
  const slider = await openSwitch(page);
  await send({ type: 'automation_toggle', switch_id: sid, label: 'Plug', enabled: true });
  await expect(slider).toHaveAttribute('aria-disabled', 'true');
  let requests = 0;
  await page.route('**/switch/toggle?**', route => { requests += 1; return route.fulfill({ json: { state: true } }); });
  page.once('dialog', dialog => dialog.accept());
  await slider.dispatchEvent('click');
  expect(requests).toBe(0);
  await expect(slider).toHaveText('Off');
  await page.route('**/switch-status-update', route => route.fulfill({ json: { [key]: { state: false, confirmed: false, online: false } } }));
  await page.clock.fastForward(60000);
  await expect(slider).toHaveText('Unknown');
  await expect(slider).not.toHaveAttribute('aria-checked');
  await expect(slider).toHaveClass(/switch-unconfirmed/);
});


test('queued device command keeps confirmed state until a matching live report', async ({ page }) => {
  const send = await liveSocket(page);
  const slider = await openSwitch(page);
  const pending = { state: false, pending: true, requested_state: true, pending_until: now.getTime() / 1000 + 45 };
  await page.route('**/switch/toggle?**', route => route.fulfill({ json: pending }));
  await slider.click();
  await expect(slider).toHaveText('Off');
  await expect(slider).toHaveAttribute('aria-checked', 'false');
  await expect(slider).toBeDisabled();
  await expect(page.locator('.switch-command-status')).toHaveText('Waiting for device: On');
  // A history-only replay must not change the live slider or confirm the command.
  await send({ type: 'switch_event', key, state: true, state_confirmed: false, timestamp: at(10) });
  await expect(slider).toHaveText('Off');
  await expect(slider).toBeDisabled();
  // A report of the old state also leaves the different requested state pending.
  await send({ type: 'switch_event', key, state: false, state_confirmed: true, timestamp: at(0) });
  await expect(slider).toHaveText('Off');
  await expect(slider).toBeDisabled();
  await send({ type: 'switch_event', key, state: true, state_confirmed: true, timestamp: at(0), source: 'manual/web' });
  await expect(slider).toHaveText('On');
  await expect(slider).toHaveAttribute('aria-checked', 'true');
  await expect(slider).toBeEnabled();
  await expect(page.locator('.switch-command-status')).toBeEmpty();
});

for (const failure of ['rejection', 'timeout']) {
  test(`device command ${failure} preserves last confirmed state`, async ({ page }) => {
    const slider = await openSwitch(page);
    let pending = false;
    await page.route('**/switch-status-update', route => route.fulfill({ json: {
      [key]: { state: false, pending, requested_state: true, pending_until: now.getTime() / 1000 + 45 },
    } }));
    await page.route('**/switch/toggle?**', route => {
      pending = true;
      return route.fulfill({ json: { state: false, pending: true, requested_state: true, pending_until: now.getTime() / 1000 + 45 } });
    });
    await slider.click();
    await expect(page.locator('.switch-command-status')).toContainText('Waiting for device');
    if (failure === 'rejection') {
      await page.route('**/switch-status-update', route => route.fulfill({ json: {
        [key]: { state: false, pending: false, command_error: 'Device rejected the switch command.' },
      } }));
      await page.clock.fastForward(1500);
      await expect(page.locator('.switch-command-status')).toContainText('rejected');
    } else {
      await page.clock.fastForward(45001);
      await expect(page.locator('.switch-command-status')).toContainText('did not confirm');
    }
    await expect(slider).toHaveText('Off');
    await expect(slider).toHaveAttribute('aria-checked', 'false');
    await expect(slider).toBeEnabled();
  });
}
