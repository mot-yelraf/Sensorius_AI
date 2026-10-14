/** Recapture documentation UI with synthetic data, never a running hub.
 * Usage: npm run docs:screenshots -- --output-dir /absolute/staging/directory
 * Reserved example.com names and 192.0.2.0/24 addresses are intentional.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';

const root = path.resolve(import.meta.dirname, '..');
const outputIndex = process.argv.indexOf('--output-dir');
const output = outputIndex < 0 ? path.join(root, 'assets/screenshots') : path.resolve(process.argv[outputIndex + 1]);
const runtime = await mkdtemp(path.join(os.tmpdir(), 'sensorius-docs-'));
const captureId = randomUUID();
const origin = 'http://127.0.0.1:18772';
const now = Date.parse('2026-10-14T12:00:00Z');
const epoch = now / 1000;
const devices = [
  {device_id: 'lux-demo01', board_type: 'pico2w', firmware_version: 'v0.26.287.1', ip: '192.0.2.10', eligible: true},
  {device_id: 'avpd-demo02', board_type: 'xesp32s3', firmware_version: 'v0.26.287.1', ip: '192.0.2.20', eligible: true},
];
const ecowitt = {
  gateway_url: 'http://192.0.2.40', gateway_model: 'GW1200', label: 'Receiving Ecowitt weather data',
  inventory: [{id: 'DEMO-ARRAY', name: 'Temperature, Humidity, Solar, Wind & Rain', family: 'WH69', reporting: true}],
  smart_plugs: [{id: 1001, online: true, state: false}], poll_interval_sec: 60,
  smart_plug_interval_sec: 30, live_metric_count: 9, last_accepted_reading: '2026-10-14T11:59:55+00:00',
};
const payloads = {
  '/ecowitt/status': ecowitt,
  '/api/nodus-ota/devices': {ok: true, devices},
  '/api/nodus-ota/jobs': {ok: true, jobs: []},
  '/remove-device-list': {
    devices: ['avpd-demo02', 'ecowitt-demo', 'lux-demo01', 'switch-demo'],
    device_details: Object.fromEntries([
      ['avpd-demo02', '192.0.2.20'], ['ecowitt-demo', '192.0.2.40'],
      ['lux-demo01', '192.0.2.10'], ['switch-demo', '192.0.2.30'],
    ].map(([id, ip]) => [id, {url: `http://${ip}:8000`, last_seen_s: 5}])),
  },
  '/sensor-settings/statistics': {ok: true, offline_events_24h: 1, last_offline_epoch: epoch - 1800,
    last_offline_event_label: '2026-10-14 11:30:00 UTC', last_packet_epoch: epoch - 5, data_packets_received: 1200},
  '/switch-settings/statistics': {ok: true, offline_events_24h: 0, switch_last_packet_epoch: epoch - 5,
    switch_channel_state_stats: [
      {channel_id: 's1-demo', row_label: 'S1 current state, age:', state: 'Off', state_epoch: epoch - 1800},
      {channel_id: 's2-demo', row_label: 'S2 current state, age:', state: 'On', state_epoch: epoch - 600},
    ]},
  '/sensor-directory': [], '/notifications/rules': {rules: []},
  '/timezone-options': {options: ['UTC']},
};
const host = spawn(process.env.SENSORIUS_PYTHON || 'python3', [
  '-m', 'uvicorn', 'testApparatus.playwright_host:app', '--host', '127.0.0.1', '--port', '18772',
], {cwd: root, env: {...process.env, SENSORIUS_RUNTIME_ROOT: runtime, SENSORIUS_GUI: 'false',
  SENSORIUS_DOCS_CAPTURE_ID: captureId, SENSORIUS_EMAIL_ENABLED: 'false'}, stdio: ['ignore', 'ignore', 'pipe']});
let hostError = '';
host.stderr.on('data', data => { hostError = (hostError + data.toString()).slice(-3000); });
let browser;
try {
  await mkdir(output, {recursive: true});
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (host.exitCode !== null) throw new Error(`Isolated capture host failed: ${hostError}`);
    try {
      const response = await fetch(`${origin}/documentation-capture`);
      const data = await response.json();
      if (data.capture_id === captureId) { ready = true; break; }
    } catch { /* Wait only for our nonce-identified fixture, never another hub. */ }
    await delay(100);
  }
  assert(ready, 'The isolated documentation fixture did not become ready');
  browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1200, height: 1200}, timezoneId: 'UTC', locale: 'en-US', serviceWorkers: 'block'});
  let blockedWrites = 0;
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.fulfill({contentType: 'application/javascript', body: ''});
    if (route.request().method() !== 'GET') {
      blockedWrites++;
      return route.fulfill({status: 405, json: {ok: false, error: 'Documentation capture is read-only'}});
    }
    if (Object.hasOwn(payloads, url.pathname)) return route.fulfill({json: payloads[url.pathname]});
    if (['/edit-system', '/edit-sensor', '/edit-switch'].includes(url.pathname)) {
      url.searchParams.set('documentation', 'true');
      return route.continue({url: url.toString()});
    }
    if (url.pathname === '/' || url.pathname.startsWith('/ui_static/')) return route.continue();
    // Background API requests receive empty fixture data, never live services.
    return route.fulfill({json: {ok: true, active: false, events: [], rules: [], items: []}});
  });

  async function pageWithDashboard() {
    const page = await context.newPage();
    await page.clock.setFixedTime(now);
    await page.goto(`${origin}/?documentation=true`);
    await page.waitForFunction(() => !!window.BackdropModal);
    return page;
  }

  async function auditAndCapture(page, locator, filename) {
    await locator.waitFor({state: 'visible'});
    await page.evaluate(() => document.fonts.ready);
    // Check the rendered data before encoding pixels, including visible controls.
    const text = await locator.evaluate(element => [element.innerText,
      ...[...element.querySelectorAll('input, textarea, select')]
        .filter(control => control.getClientRects().length && getComputedStyle(control).visibility !== 'hidden')
        .map(control => control.value),
    ].join('\n'));
    for (const address of text.match(/\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b/g) || []) {
      assert(address.endsWith('@example.com'), `${filename}: non-example email detected`);
    }
    for (const ip of text.match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g) || []) {
      if (ip.split('.').some(part => Number(part) > 255)) continue;
      assert(ip.startsWith('192.0.2.'), `${filename}: non-documentation IP detected`);
    }
    assert(!/\b[\w-]+\.local\b|\/Users\/|\/home\/|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b/i.test(text), `${filename}: private host/path/device identity detected`);
    const domains = text.match(/\b(?:[A-Za-z0-9-]+\.)+(?:com|net|org|arpa|test)\b/gi) || [];
    for (const domain of domains) assert(domain === 'example.com' || domain.endsWith('.example.com'), `${filename}: unexpected hostname detected`);
    await locator.screenshot({path: path.join(output, filename), animations: 'disabled'});
    console.log(`Captured and checked ${filename}`);
  }

  async function systemCapture(filename, pane, section) {
    const page = await pageWithDashboard();
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    if (pane) await modal.getByRole('button', {name: pane, exact: true}).click();
    if (section) {
      await modal.locator('details').evaluateAll(details => details.forEach(detail => { detail.open = false; }));
      await modal.locator('summary').filter({hasText: new RegExp(`^${section}$`)}).click();
    }
    assert.equal(await modal.locator('#hostname').inputValue(), 'hub.example.com');
    assert.equal(await modal.locator('#email_username').inputValue(), 'operator@example.com');
    if (pane === 'Update Device') await page.waitForFunction(() => document.querySelector('#ota-device-list')?.textContent.includes('avpd-demo02'));
    if (pane === 'Remove Device') await page.waitForFunction(() => document.querySelectorAll('#remove-device-list input').length === 4);
    if (pane === 'Add Device') await page.waitForFunction(() => document.querySelector('#ecowitt-sensor-list')?.textContent.includes('DEMO-ARRAY'));
    if (section) {
      // Give expanded sections enough space for their Save controls in the guide.
      await modal.locator('.system-settings-shell').evaluate(shell => {
        const scrollAreas = [...shell.querySelectorAll('*')].filter(element =>
          element.clientHeight > 0 && /auto|scroll/.test(getComputedStyle(element).overflowY));
        const overflow = Math.max(0, ...scrollAreas.map(element => element.scrollHeight - element.clientHeight));
        if (overflow) shell.style.height = `${shell.getBoundingClientRect().height + overflow + 24}px`;
      });
    }
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (section) {
      const complete = await modal.locator('details[open]').first().evaluate(detail => {
        const scroll = detail.closest('.integrations-list');
        return detail.getBoundingClientRect().bottom <= scroll.getBoundingClientRect().bottom + 1;
      });
      assert(complete, `${filename}: expanded section would be clipped`);
    }
    await auditAndCapture(page, modal.locator('.system-settings-shell'), filename);
    await page.close();
  }

  await systemCapture('system-settings-overview.png', null, 'Network Settings');
  await systemCapture('system-settings-notifications.png', null, 'Notifications');
  await systemCapture('system-settings-add-device-ecowitt.png', 'Add Device', 'Ecowitt Gateway');
  await systemCapture('system-settings-update-device.png', 'Update Device');
  await systemCapture('system-settings-remove-device.png', 'Remove Device');
  for (const [kind, id, filename] of [
    ['sensor', 'avpd-demo02', 'sensor-settings-device-info.png'],
    ['switch', 'switch-demo', 'switch-settings-device-info.png'],
  ]) {
    const page = await pageWithDashboard();
    await page.evaluate(({kind, id}) => kind === 'sensor' ? window.editSensorSettings(id) : window.editSwitchSettings(id), {kind, id});
    const modal = page.locator(`#${kind}SettingsModal`);
    await modal.getByRole('button', {name: kind === 'sensor' ? 'Sensor Info' : 'Switch Info', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.modal:not(#setupPiModal) [data-stat-value="last-packet"]')?.textContent.includes('5s'));
    await auditAndCapture(page, modal, filename);
    await page.close();
  }
  assert.equal(blockedWrites, 0, 'A documentation capture attempted to mutate runtime state');
  console.log('Seven screenshots recaptured from isolated, read-only example data.');
} finally {
  await browser?.close();
  host.kill('SIGTERM');
  await new Promise(resolve => { if (host.exitCode !== null) resolve(); else host.once('exit', resolve); });
  await rm(runtime, {recursive: true, force: true});
}
