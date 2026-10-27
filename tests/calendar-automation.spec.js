import {test, expect} from '@playwright/test';

async function openCalendarEditor(page, condition) {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({contentType: 'application/javascript', body: ''}));
  await page.route('**/sensor-directory', route => route.fulfill({json: []}));
  await page.route('**/sensor-metrics?*', route => route.fulfill({json: {}}));
  let script = {name: 'Calendar notice', enabled: true, conditions: [condition], actions: [{type: 'none', executor_switch_id: '__system__'}]};
  await page.route('**/automation-context', route => route.fulfill({json: {actors: [], email_enabled: true, executor_switch_id: '__system__', calendar_timezone: 'America/Denver', astral_status: {ok: true}}}));
  await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: [{rule_id: 'calendar', enabled: true, script_json: JSON.stringify(script)}]}}));
  const saves = [];
  await page.route('**/submit-advanced-trigger', route => {
    script = JSON.parse(route.request().postDataJSON().script_json);
    saves.push(script);
    return route.fulfill({json: {ok: true, rule_id: 'calendar'}});
  });
  await page.goto('/');
  await page.getByRole('link', {name: 'Open General Settings'}).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', {name: 'Automations', exact: true}).click();
  await modal.getByText('Calendar notice', {exact: true}).click();
  return {modal, saves};
}

test('Calendar loads legacy BD rules, filters elements and phases, and saves selections', async ({page}) => {
  const {modal, saves} = await openCalendarEditor(page, {type: 'bd_transitions', executor_switch_id: '__system__'});
  const row = modal.locator('.cond.calendar');
  const type = row.locator('select').nth(0);
  await expect(type).toHaveValue('calendar');
  expect(await type.locator('option').allTextContents()).toEqual(['Sensor', 'Time of Day', 'Timer', 'Astral', 'Calendar', 'Severe Weather', 'System Status', 'Or']);
  const kind = row.getByRole('combobox', {name: 'Calendar event type', exact: true});
  const event = row.getByRole('combobox', {name: 'Calendar event', exact: true});
  await expect(kind).toHaveValue('bd_transitions');
  expect(await event.locator('option').allTextContents()).toEqual(['All', 'Earth/Root', 'Water/Leaf', 'Air/Flower', 'Fire/Fruit']);
  const boxes = await Promise.all([type, kind, event].map(item => item.boundingBox()));
  expect(boxes[0].x).toBeLessThan(boxes[1].x);
  expect(boxes[1].x).toBeLessThan(boxes[2].x);
  await event.selectOption('earth');
  await modal.locator('#btnSetAutomation').click();
  await expect.poll(() => saves.length).toBe(1);
  await expect(modal.locator('#btnSetAutomation')).toBeEnabled();
  expect(saves[0].conditions[0]).toMatchObject({type: 'calendar', calendar_kind: 'bd_transitions', calendar_event: 'earth'});
  await kind.selectOption('lunar_events');
  await expect(event).toHaveValue('all');
  expect(await event.locator('option').allTextContents()).toEqual(['All', 'New Moon', 'Waxing Crescent', 'First Quarter', 'Waxing Gibbous', 'Full Moon', 'Waning Gibbous', 'Last Quarter', 'Waning Crescent']);
  await event.selectOption('full_moon');
  await modal.locator('#btnSetAutomation').click();
  await expect.poll(() => saves.length).toBe(2);
  expect(saves[1].conditions[0]).toEqual({type: 'calendar', calendar_kind: 'lunar_events', calendar_event: 'full_moon', executor_switch_id: '__system__'});
  await expect(row.locator('input')).toHaveCount(0);
});

test('Calendar selectors restore saved lunar filters and fit a phone', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  const {modal} = await openCalendarEditor(page, {type: 'calendar', calendar_kind: 'lunar_events', calendar_event: 'waning_crescent'});
  const row = modal.locator('.cond.calendar');
  await expect(row.locator('.calendar-event')).toHaveValue('waning_crescent');
  for (const select of await row.locator('select').all()) {
    const box = await select.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
  }
});

test('BD alert keeps its color and excludes local sky details', async ({page}) => {
  await page.goto('/');
  await page.evaluate(() => window.receiveWeatherAlert({
    type: 'bd_transition', transition_at: '2026-10-14T08:30:00-06:00',
    from: {sign: 'Aries', element: 'Fire', plant_part: 'Fruit'},
    to: {sign: 'Taurus', element: 'Earth', plant_part: 'Root', color: '#e5b172'},
    context: 'Waxing Crescent\nMoonrise: 11:30 AM\nMoonset: 08:25 PM\nSunrise: 07:09 AM\nSunset: 06:21 PM',
  }));
  const toast = page.locator('.bd-transition-toast');
  await expect(toast).toContainText('From Aries');
  await expect(toast).not.toContainText('Sunrise');
  await expect(toast).not.toContainText('Moonrise');
  await expect(toast).not.toContainText('Waxing Crescent');
  await expect(toast).toHaveCSS('background-color', 'rgb(229, 177, 114)');
  await toast.click();
  await expect(toast).toHaveCount(0);
});

for (const width of [1280, 390]) {
  test(`Calendar Date/Time saves a timezone-labelled one-time event at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844});
    const {modal, saves} = await openCalendarEditor(page, {type: 'calendar', calendar_kind: 'date_time', calendar_local: '2026-10-31T15:30', calendar_timezone: 'America/Denver'});
    const row = modal.locator('.cond.calendar');
    const kind = row.locator('.calendar-kind');
    expect(await kind.locator('option').allTextContents()).toEqual(['Date/Time', 'BD Transitions', 'Lunar Events', 'Season']);
    const datetimeInput = row.locator('.calendar-datetime');
    await expect(datetimeInput).toHaveValue('2026-10-31T15:30');
    await expect(row).toContainText('America/Denver');
    await expect(row.locator('.calendar-event')).toHaveCount(0);
    await datetimeInput.fill('2026-11-02T17:45');
    const box = await datetimeInput.boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    if (width === 1280) expect(box.x).toBeGreaterThan((await kind.boundingBox()).x);
    await modal.locator('#btnSetAutomation').click();
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0].conditions[0]).toMatchObject({type: 'calendar', calendar_kind: 'date_time', calendar_local: '2026-11-02T17:45', calendar_timezone: 'America/Denver'});
    await expect(modal.locator('#btnSetAutomation')).toBeEnabled();
    await expect(datetimeInput).toHaveValue('2026-11-02T17:45');
  });
}

test('Calendar Season lists all festivals and equiluxes and saves a selection', async ({page}) => {
  const {modal, saves} = await openCalendarEditor(page, {type: 'calendar', calendar_kind: 'season', calendar_event: 'all'});
  const row = modal.locator('.cond.calendar');
  const event = row.locator('.calendar-event');
  expect(await event.locator('option').allTextContents()).toEqual(['All', 'Imbolc', 'Spring Equinox / Ostara', 'Spring Equilux', 'Beltane', 'Summer Solstice / Litha', 'Lughnasadh / Lammas', 'Autumn Equilux', 'Autumn Equinox / Mabon', 'Samhain', 'Winter Solstice / Yule']);
  await event.selectOption('samhain');
  await modal.locator('#btnSetAutomation').click();
  await expect.poll(() => saves.length).toBe(1);
  expect(saves[0].conditions[0]).toMatchObject({type: 'calendar', calendar_kind: 'season', calendar_event: 'samhain'});
  await expect(modal.locator('#btnSetAutomation')).toBeEnabled();
  await expect(event).toHaveValue('samhain');
  await row.locator('.calendar-kind').selectOption('date_time');
  await expect(row.locator('.calendar-datetime')).toBeVisible();
  await row.locator('.calendar-kind').selectOption('lunar_events');
  await expect(row.locator('.calendar-datetime')).toHaveCount(0);
  await expect(row.locator('.calendar-event')).toHaveValue('all');
});
