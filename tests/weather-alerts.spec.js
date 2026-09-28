import {test, expect} from '@playwright/test';

function warning() {
  const now = Date.now();
  return {
    event_key: 'fixture-warning', event: 'Severe Thunderstorm Warning',
    effective: new Date(now - 60000).toISOString(),
    expires: new Date(now + 3600000).toISOString(),
    ends: new Date(now + 3600000).toISOString(),
    message: 'Severe Weather — Severe Thunderstorm Warning\nStarts: Sep 28, 2026 2:00 PM MDT\nEnds: Sep 28, 2026 3:00 PM MDT (about 1h 0m)\nArea: Test County\nWinds to 70 mph and large hail.\nMove indoors.',
  };
}

for (const width of [1440, 390]) {
  test(`NWS warning changes dashboard colors and Caelus details at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 1000});
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({contentType: 'application/javascript', body: ''}));
    let events = [warning()];
    await page.route('**/api/weather-alerts', route => route.fulfill({json: {enabled: true, active: !!events.length, events}}));
    await page.goto('/');
    const tile = page.locator('#weatherForecastBox');
    await expect(tile).toHaveClass(/severe-weather-active/);
    await expect(tile.locator('.forecast-current')).toHaveCSS('background-color', 'rgb(239, 140, 35)');
    await expect(tile.locator('.forecast-open-btn')).toHaveCSS('background-color', 'rgb(239, 140, 35)');
    await tile.screenshot({path: testInfo.outputPath('severe-weather-tile.png')});
    await page.goto('/weather-forecast?native=true');
    const synopsis = page.locator('.forecast-synopsis');
    await expect(synopsis).toContainText('Severe Thunderstorm Warning');
    await expect(synopsis).toContainText('Ends:');
    await expect(synopsis).toContainText('70 mph');
    await expect(synopsis).toHaveCSS('white-space', 'pre-line');
    await page.locator('.forecast-panel').screenshot({path: testInfo.outputPath('severe-weather-caelus.png')});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    events = [];
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(synopsis).not.toContainText('Severe Weather');
    await expect(synopsis).toContainText('NWS · original units');
    await expect(page.locator('.forecast-panel')).not.toHaveClass(/severe-weather-active/);
    await page.goto('/');
    await expect(tile).not.toHaveClass(/severe-weather-active/);
  });
}

test('an expired NWS alert cannot keep the dashboard orange', async ({page}) => {
  const event = warning();
  event.expires = '2020-01-01T00:00:00Z';
  await page.route('**/api/weather-alerts', route => route.fulfill({json: {enabled: true, active: true, events: [event]}}));
  await page.goto('/');
  await expect(page.locator('#weatherForecastBox')).not.toHaveClass(/severe-weather-active/);
});

test('Severe Weather condition saves with any existing actor', async ({page}) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({contentType: 'application/javascript', body: ''}));
  await page.route('**/automation-context', route => route.fulfill({json: {
    actors: [{switch_id: 'switch-test', channel_id: 'S1-test', label: 'Fan', value: 'switch-test::S1-test'}],
    email_enabled: true, executor_switch_id: '__system__',
  }}));
  await page.route('**/switch-info?*', route => route.fulfill({json: {labels: {}, astral_status: {ok: true}}}));
  await page.route('**/sensor-directory', route => route.fulfill({json: []}));
  await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: []}}));
  let saved;
  await page.route('**/submit-advanced-trigger', async route => {
    saved = route.request().postDataJSON();
    await route.fulfill({json: {ok: true}});
  });
  await page.goto('/');
  await page.getByRole('link', {name: 'Open General Settings'}).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', {name: 'Automations', exact: true}).click();
  await modal.locator('#autoName').fill('Storm protection');
  await modal.locator('#conditionsContainer .cond-group select').first().selectOption('severe_weather');
  const conditionRow = modal.locator('.cond.severe-weather');
  await expect(conditionRow).toBeVisible();
  await expect(conditionRow).not.toContainText('NWS warnings and watches');
  const rowBounds = await conditionRow.boundingBox();
  const removeBounds = await conditionRow.getByRole('button', {name: 'Remove condition'}).boundingBox();
  expect(Math.abs(removeBounds.x + removeBounds.width - rowBounds.x - rowBounds.width)).toBeLessThan(2);
  expect(Math.abs(removeBounds.y + removeBounds.height - rowBounds.y - rowBounds.height)).toBeLessThan(2);
  const actors = modal.locator('.action-actor').first();
  await expect(actors.locator('option[value="none"]')).toHaveText('Alert');
  await expect(actors.locator('option[value="notify"]')).toHaveText('Notify');
  await expect(actors.locator('option[value="switch-test::S1-test"]')).toHaveText('Fan');
  await actors.selectOption('none');
  await modal.locator('#btnSetAutomation').click();
  await expect.poll(() => saved?.script_json).toBeTruthy();
  const script = JSON.parse(saved.script_json);
  expect(script.conditions).toEqual([{type: 'severe_weather'}]);
  expect(script.actions[0].type).toBe('none');
});

test('detailed Severe Weather toast can be dismissed while the forecast stays orange', async ({page}) => {
  let connection;
  await page.routeWebSocket('**/ws/switch-updates', ws => { connection = ws; });
  const event = warning();
  await page.route('**/api/weather-alerts', route => route.fulfill({json: {enabled: true, events: [event]}}));
  await page.goto('/');
  await expect.poll(() => !!connection).toBeTruthy();
  connection.send(JSON.stringify({type: 'automation_notification', name: 'Storm protection',
    weather_event_ids: [event.event_key], trigger_conditions: [event.message], trigger_values: ['Triggered'],
    occurred_at: new Date().toISOString()}));
  const toast = page.locator('.automation-notification-toast');
  await expect(toast).toContainText('Severe Thunderstorm Warning');
  await expect(toast).toContainText('Ends:');
  await toast.click();
  await expect(toast).toHaveCount(0);
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.locator('#weatherForecastBox')).toHaveClass(/severe-weather-active/);
  await expect(toast).toHaveCount(0);
});

test('pending weather Alert replays on load and dismissal survives reload', async ({page}) => {
  test.setTimeout(60000);
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({contentType: 'application/javascript', body: ''}));
  const event = warning();
  let dismissed = false;
  const message = {type: 'automation_notification', weather_alert_id: 'saved-alert',
    weather_event_ids: [event.event_key], name: 'Storm protection',
    trigger_conditions: [event.message], trigger_values: ['Triggered'], occurred_at: new Date().toISOString()};
  await page.route('**/api/weather-alerts', route => route.fulfill({json: {
    enabled: true, events: [event], pending_alerts: dismissed ? [] : [message],
  }}));
  await page.route('**/api/weather-alerts/saved-alert/dismiss', route => {
    dismissed = true;
    return route.fulfill({json: {ok: true}});
  });
  await page.goto('/');
  const toast = page.locator('.automation-notification-toast');
  await expect(toast).toContainText('Severe Thunderstorm Warning', {timeout: 20000});
  await expect(toast).toHaveCSS('background-color', 'rgb(239, 140, 35)');
  await expect(toast).toHaveCSS('color', 'rgb(0, 0, 0)');
  await page.reload();
  await expect(toast).toContainText('Severe Thunderstorm Warning', {timeout: 20000});
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(toast).toHaveCount(1);
  await toast.click();
  await expect(toast).toHaveCount(0);
  expect(dismissed).toBe(true);
  await page.reload();
  await expect(page.locator('#weatherForecastBox')).toHaveClass(/severe-weather-active/);
  await expect(toast).toHaveCount(0);
});
