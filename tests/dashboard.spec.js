import { replaceDashboardConfig } from './helpers/dashboard-config.js';
// @ts-check
import { test, expect } from '@playwright/test';

function successfulBiodynamicMonth(monthKey = '2026-09') {
  const [year, month] = monthKey.split('-').map(Number);
  const monthStart = new Date(Date.UTC(year, month - 1, 1));
  const gridStart = new Date(monthStart);
  gridStart.setUTCDate(1 - monthStart.getUTCDay());
  const calendar = Array.from({ length: 42 }, (_, offset) => {
    const current = new Date(gridStart);
    current.setUTCDate(gridStart.getUTCDate() + offset);
    const day = current.toISOString().slice(0, 10);
    return {
      date: day,
      day: current.getUTCDate(),
      weekday: current.toLocaleString('en-US', { weekday: 'short', timeZone: 'UTC' }),
      in_month: current.getUTCMonth() === month - 1,
      is_today: day === `${monthKey}-15`,
      sunrise: current.getUTCDate() === 17 ? '' : (current.getUTCDate() === 16 ? '06:46' : '06:45'),
      sunset: current.getUTCDate() === 17 ? '' : (current.getUTCDate() === 16 ? '19:08' : '19:10'),
      daylight_minutes: current.getUTCDate() === 17 ? null : (current.getUTCDate() === 16 ? 742 : 745),
      daylight_label: current.getUTCDate() === 15 ? '12 Hrs 25 Mins' : '',
      dominant_sign: 'Taurus',
      dominant_sign_abbr: 'Tau',
      dominant_element: 'Earth',
      dominant_plant_part: 'Root',
      dominant_color: '#e5b172',
      dominant_accent: '#644817',
      moon_direction: 'ascending',
      segments: [{ start: '00:00', end: '24:00', sign: 'Taurus', element: 'Earth', plant_part: 'Root', kind: 'sign' }],
      lunar_events: [],
    };
  });
  return {
    ok: true,
    month_label: monthStart.toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' }),
    weekday_labels: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
    current: { sign: 'Taurus', element: 'Earth', plant_part: 'Root', window_start_hm: '00:00', window_end_hm: '24:00' },
    calendar,
    notes: {},
    plantings: [],
    astro: { ok: false, reason: 'Browser fixture' },
    location: { ok: true, latitude: 39.7392, longitude: -104.9903, timezone_name: 'America/Denver' },
  };
}

test.beforeEach(async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', async (route) => {
    await route.fulfill({ contentType: 'application/javascript', body: '' });
  });
  await page.addInitScript(() => {
    window.Gauge = class {
      setOptions() { return this; }
      set() {}
    };
    window.Chart = class {
      static getChart() { return null; }
      constructor() {}
      destroy() {}
    };
  });
});

test('renders the Sensorius dashboard and expands additional metrics', async ({ page }) => {
  const response = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(response?.ok()).toBeTruthy();
  await expect(page).toHaveTitle(/Sensorius/i);

  const sensorGroup = page.locator(".sensor-group[data-sensor-id='aht-pr-check']");
  await expect(sensorGroup).toBeVisible();
  await expect(sensorGroup.locator('.metric-container')).toHaveCount(8);

  const toggle = sensorGroup.locator('.sensor-collapse-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(sensorGroup.locator('.metric-container').last()).toBeVisible();
});

test('keeps the header image above sensors after moving the bottom sensor up', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => {
    const dashboardContent = document.querySelector('.dashboard-content');
    const source = dashboardContent?.querySelector('.sensor-group[data-sensor-id]');
    if (!dashboardContent || !source) throw new Error('dashboard fixture is incomplete');
    const ecowitt = source.cloneNode(true);
    ecowitt.id = 'group_ecowitt-bottom-test';
    ecowitt.dataset.sensorId = 'ecowitt-bottom-test';
    ecowitt.dataset.dashboardRowId = 'ecowitt-bottom-test';
    ecowitt.querySelectorAll('[data-sensor-id]').forEach((element) => {
      element.setAttribute('data-sensor-id', 'ecowitt-bottom-test');
    });
    dashboardContent.appendChild(ecowitt);
    window.updateSensorOrderButtons();
  });

  let reordered = false;
  await page.route('**/dashboard/metric-position', async (route) => {
    const groups = await page.locator('.sensor-group[data-sensor-id]').evaluateAll((elements) =>
      elements.map((element) => element.getAttribute('data-sensor-id')),
    );
    const body = route.request().postDataJSON();
    const index = groups.indexOf(body.sensor_id);
    if (index > 0 && body.direction === 'up') {
      [groups[index - 1], groups[index]] = [groups[index], groups[index - 1]];
    }
    reordered = true;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ status: 'ok', moved: true, order: groups }) });
  });

  const ecowitt = page.locator(".sensor-group[data-sensor-id='ecowitt-bottom-test']");
  await ecowitt.locator(".sensor-order-item[data-move='up']").click();
  await expect.poll(() => reordered).toBe(true);
  const image = page.locator('#sensor_header .dashboard-brand-image');
  await expect(image).toBeVisible();
  await expect(page.locator('#dashboard-overview-footer')).toHaveCount(0);
  const imageBox = await image.boundingBox();
  expect(imageBox.width).toBeCloseTo(230, 0);
  const sensorTop = await page.locator('.sensor-group[data-sensor-id]').evaluateAll((elements) =>
    Math.min(...elements.map((element) => element.getBoundingClientRect().top)),
  );
  expect(imageBox.y + imageBox.height).toBeLessThanOrEqual(sensorTop);
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    const brandBox = await image.boundingBox();
    const moonBox = await page.locator('#moonBox').boundingBox();
    expect(brandBox.width).toBeCloseTo(moonBox.width, 1);
    const clock = page.locator('#update_time');
    for (const text of ['Tue 2026-10-13 01:11:11 AM', 'Wed 2026-12-30 08:58:58 PM']) {
      await clock.evaluate((element, value) => { element.textContent = value; }, text);
      expect((await clock.boundingBox()).width).toBeCloseTo(brandBox.width, 1);
      const textFits = await clock.evaluate((element) => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const text = range.getBoundingClientRect();
        const pill = element.getBoundingClientRect();
        return text.left >= pill.left && text.right <= pill.right;
      });
      expect(textFits).toBe(true);
    }
    await expect.poll(() => page.locator('#sensor_header').evaluate((header) => {
      const brand = header.querySelector('.dashboard-brand-image').getBoundingClientRect();
      return Array.from(header.querySelectorAll('.dashboard-header-action')).map((button) => {
        const box = button.getBoundingClientRect();
        return Math.round(Math.abs((box.y + box.height / 2) - (brand.y + brand.height / 2)));
      });
    })).toEqual([0, 0]);
    await page.locator('#sensor_header').screenshot({ path: test.info().outputPath(`header-${width}.png`) });
    await page.screenshot({ path: test.info().outputPath(`dashboard-${width}.png`) });
  }

});

test('shares and persists the Lunar Calendar view mode', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.removeItem('sensorius.moonViewMode'));
  await page.reload({ waitUntil: 'domcontentloaded' });

  await page.locator('#moonCalendarBtn').click();
  await expect.poll(() => page.locator('#caelusMoonDialog').evaluate((element) => getComputedStyle(element).borderRadius)).toBe('16px');
  await expect(page.locator('#caelusMoonViewLocal')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => {
    window.CaelusMoon.renderMoonDisk(canvas, {
      name: 'First quarter',
      illumination: 50,
      bright_limb_angle: 135,
      disk_rotation: 42,
    });
  });
  const localDisk = await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => canvas.toDataURL());

  await page.locator('#caelusMoonViewReference').click();
  await expect(page.locator('#caelusMoonViewReference')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('sensorius.moonViewMode'))).toBe('reference');
  const referenceDisk = await page.locator('#caelusCurrentMoonDisk').evaluate((canvas) => canvas.toDataURL());
  expect(referenceDisk).not.toBe(localDisk);
  await expect(page.locator('#caelusCurrentMoonDisk')).toHaveAttribute('aria-label', /Reference north-up view/);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#moonViewReference')).toHaveAttribute('aria-pressed', 'true');
});

test('shows eclipse details in the Moon tile and Lunar Calendar', async ({ page }) => {
  await page.route('**/api/weather-forecast-app/astronomy', async (route) => {
    const eclipse = {
      kind: 'Partial lunar eclipse',
      date: 'Aug 27, 2026',
      at: '2026-08-27T22:12:00-06:00',
      starts_at: '2026-08-27T20:48:00-06:00',
      ends_at: '2026-08-27T23:38:00-06:00',
      time: '10:12 PM',
      starts: '8:48 PM',
      ends: '11:38 PM',
    };
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        eclipse_next_24h: eclipse,
        next_eclipses: [eclipse],
        previous_phases: [],
        upcoming_phases: [],
      }),
    });
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#moonEclipse24h')).toBeVisible();
  await expect(page.locator('#moonEclipse24hText')).toContainText('Partial lunar eclipse');
  await expect(page.locator('#moonEclipse24hText')).toContainText('8:48 PM–11:38 PM');

  await page.locator('#moonCalendarBtn').click();
  await expect(page.locator('#caelusUpcomingEclipseText')).toContainText('Partial lunar eclipse');
  await expect(page.locator('#caelusUpcomingEclipseText')).toContainText('Aug 27, 2026');
});

test('clears stale eclipse details at the next station-local midnight', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-08-28T23:59:30-06:00') });
  let astronomyRequests = 0;
  await page.route('**/api/weather-forecast-app/astronomy', async (route) => {
    astronomyRequests += 1;
    const eclipse = {
      kind: 'Partial lunar eclipse',
      date: 'Aug 27, 2026',
      starts: '7:43 PM',
      ends: '12:43 AM',
    };
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        timezone: 'America/Denver',
        eclipse_next_24h: astronomyRequests === 1 ? eclipse : null,
        next_eclipses: astronomyRequests === 1 ? [eclipse] : [],
        previous_phases: [],
        upcoming_phases: [],
      }),
    });
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#moonEclipse24h')).toBeVisible();
  await page.clock.fastForward(1_000);
  await page.clock.fastForward(60_000);
  await expect.poll(() => astronomyRequests).toBeGreaterThanOrEqual(2);
  await expect(page.locator('#moonEclipse24h')).toBeHidden();
});

test('opens the 29 day Sun/Moon graph from an aligned tile button', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });

  const graphButton = page.locator('#sunMoon29Btn');
  const lunarButton = page.locator('#moonCalendarBtn');
  await expect(graphButton).toBeVisible();
  await expect(graphButton).toHaveText('29 Day Graph');

  const chartBox = await page.locator('#sunPathCanvas').boundingBox();
  expect(chartBox?.height).toBeCloseTo(86.4, 1);

  const graphButtonBox = await graphButton.boundingBox();
  const lunarButtonBox = await lunarButton.boundingBox();
  expect(Math.abs((graphButtonBox?.y || 0) - (lunarButtonBox?.y || 0))).toBeLessThanOrEqual(2);

  await graphButton.click();
  await expect(page.locator('#sunMoon29Overlay')).toBeVisible();
  await expect(page.locator('#sunMoon29Overlay')).toHaveAttribute('aria-hidden', 'false');
});

test('renders the integrated Biodynamic Calendar and exercises its user workflows', async ({ page }) => {
  const noteWrites = [];
  const plantingWrites = [];
  await page.route('**/api/biodynamic-calendar-app/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith('/calendar')) {
      const month = url.searchParams.get('month') || '2026-09';
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(successfulBiodynamicMonth(month)) });
      return;
    }
    if (url.pathname.endsWith('/calendar-range')) {
      const start = url.searchParams.get('start') || '2026-09';
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, months: [successfulBiodynamicMonth(start)] }) });
      return;
    }
    if (url.pathname.endsWith('/daily-summary')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, summary: 'Biodynamic Hints\nSuggestion: tend roots.' }) });
      return;
    }
    if (url.pathname.endsWith('/note') && request.method() === 'POST') {
      noteWrites.push(request.postDataJSON());
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }
    if (url.pathname.endsWith('/planting') && request.method() === 'POST') {
      const planting = { id: 'lettuce', ...request.postDataJSON() };
      plantingWrites.push(planting);
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, planting, plantings: [planting] }) });
      return;
    }
    await route.continue();
  });

  await page.goto('/calendar', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#calendar .bio-day')).toHaveCount(42);
  await expect(page.locator('#monthLabel')).toHaveText('September 2026');
  await page.locator('#calendar [data-date="2026-09-15"]').click();
  await expect(page.locator('#dailySummary')).toContainText('tend roots');
  await expect(page.locator('#selectedSunTimes')).toHaveText('Sunrise: 6:45 AM / Sunset: 7:10 PM');
  await expect(page.locator('#selectedDaylight')).toHaveText('Hours of Daylight: 12 Hrs 25 Mins');
  await page.locator('#calendar [data-date="2026-09-16"]').click();
  await expect(page.locator('#selectedSunTimes')).toHaveText('Sunrise: 6:46 AM / Sunset: 7:08 PM');
  await expect(page.locator('#selectedDaylight')).toHaveText('Hours of Daylight: 12 Hrs 22 Mins');
  await page.locator('#calendar [data-date="2026-09-17"]').click();
  await expect(page.locator('#selectedSunTimes')).toHaveText('Sunrise: -- / Sunset: --');
  await expect(page.locator('#selectedDaylight')).toHaveText('Hours of Daylight: --');
  await page.locator('#calendar [data-date="2026-09-15"]').click();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const dateBox = await page.locator('.inspector-date').boundingBox();
    const daylightBox = await page.locator('.inspector-daylight').boundingBox();
    expect(daylightBox.x).toBeGreaterThanOrEqual(dateBox.x + dateBox.width);
    expect(daylightBox.x + daylightBox.width).toBeLessThanOrEqual(width);
    await page.locator('.inspector-head').screenshot({ path: test.info().outputPath(`selected-day-${width}.png`) });
  }
  await page.setViewportSize({ width: 1280, height: 720 });

  await page.locator('.inspector-section').filter({ hasText: 'Note' }).locator(':scope > summary').click();
  await page.locator('#noteInput').fill('Watered the root bed.');
  await page.locator('#saveNoteBtn').click();
  await expect.poll(() => noteWrites.length).toBe(1);
  expect(noteWrites[0]).toEqual({ date: '2026-09-15', note: 'Watered the root bed.' });

  await page.locator('.planting-panel > summary').click();
  await page.locator('#plantingEditor > summary').click();
  await page.locator('#plantingForm [name="name"]').fill('Lettuce');
  await page.locator('#plantingForm [name="start_date"]').fill('2026-09-15');
  await page.locator('#savePlantingBtn').click();
  await expect.poll(() => plantingWrites.length).toBe(1);
  expect(plantingWrites[0].name).toBe('Lettuce');

  await page.locator('#nextBtn').click();
  await expect(page.locator('#monthLabel')).toHaveText('October 2026');
});

test('supports keyboard expansion and keeps collapse controls consistent after resize and inventory changes', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const group = page.locator(".sensor-group[data-sensor-id='aht-pr-check']");
  const toggle = group.locator('.sensor-collapse-toggle');
  const lastMetric = group.locator('.metric-container').last();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(lastMetric).toBeHidden();
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(lastMetric).toBeVisible();
  await page.setViewportSize({ width: 720, height: 900 });
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(lastMetric).toBeVisible();
  await toggle.focus();
  await page.keyboard.press('Space');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(lastMetric).toBeHidden();
  await group.evaluate((element) => {
    Array.from(element.querySelectorAll('.metric-container')).slice(6).forEach((card) => card.remove());
    window.dispatchEvent(new Event('resize'));
  });
  await expect(toggle).toBeHidden();
  await expect(group.locator('.metric-container')).toHaveCount(6);
  await expect(group.locator('.metric-container').last()).toBeVisible();
});

test('refreshes warming Sun and Moon data while a dashboard image is still loading', async ({ page }) => {
  let releaseImage;
  const imageGate = new Promise(resolve => { releaseImage = resolve; });
  await page.route('**/ui_static/weather_forecast/moon-surface.png*', async route => {
    await imageGate;
    await route.abort();
  });
  const warming = { ok: false, reason: 'warming', cache_status: 'warming' };
  let extrasRequests = 0;
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/') return route.fallback();
    if (url.searchParams.get('json_only') === 'true') {
      if (url.searchParams.get('include_extras') === 'true') extrasRequests += 1;
      return route.fulfill({ json: {
        statuses: {},
        astro: { ok: true, moon_phase_value: 25.23, moon_lit_pct: 9,
          moon_phase_label: 'Waning Crescent', sun_points: [], moon_points: [], position_29d: [] },
        biodynamic: { ok: false },
      } });
    }
    const response = await route.fetch();
    const html = replaceDashboardConfig(await response.text(), { astroData: warming });
    await route.fulfill({ response, body: html });
  });
  try {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    expect(await page.evaluate(() => document.readyState)).toBe('interactive');
    await expect.poll(() => extrasRequests, { timeout: 4000 }).toBeGreaterThan(0);
    await expect(page.locator('#moonBox')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#sunBox')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#moonBox')).not.toContainText('Moon data unavailable');
    expect(await page.evaluate(() => document.readyState)).toBe('interactive');
  } finally {
    releaseImage();
  }
});

for (const initialResponse of ['warming', 'failed', 'missing', 'stalled']) {
  test(`recovers Sun and Moon data after ${initialResponse} extras without a reload`, async ({ page }) => {
    const warming = { ok: false, reason: 'warming', cache_status: 'warming' };
    let extrasRequests = 0;
    let pageRequests = 0;
    let releaseStalled;
    const stalledGate = new Promise(resolve => { releaseStalled = resolve; });
    await page.route('**/ui_static/js/dashboard_runtime.js?*', async route => {
      const response = await route.fetch();
      const script = (await response.text()).replace('const dashboardFetchTimeoutMs = 12000;', 'const dashboardFetchTimeoutMs = 500;');
      await route.fulfill({ response, body: script });
    });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname !== '/') return route.fallback();
      if (url.searchParams.get('json_only') === 'true') {
        if (url.searchParams.get('include_extras') !== 'true') return route.fallback();
        extrasRequests += 1;
        if (extrasRequests === 1) {
          if (initialResponse === 'failed') return route.fulfill({ status: 503, body: 'Unavailable' });
          if (initialResponse === 'missing') return route.fulfill({ json: { statuses: {} } });
          if (initialResponse === 'stalled') {
            await stalledGate;
            return route.fulfill({ json: { astro: warming, biodynamic: warming } }).catch(() => {});
          }
        }
        return route.fulfill({ json: {
          statuses: {},
          astro: extrasRequests < 3 ? warming : {
            ok: true, moon_phase_value: 25.23, moon_lit_pct: 9,
            moon_phase_label: 'Waning Crescent', sun_points: [], moon_points: [], position_29d: [],
          },
          biodynamic: { ok: false },
        } });
      }
      pageRequests += 1;
      const response = await route.fetch();
      const html = replaceDashboardConfig(await response.text(), { astroData: warming });
      await route.fulfill({ response, body: html });
    });
    try {
      await page.goto('/', { waitUntil: 'domcontentloaded' });
      await expect(page.locator('#moonBox')).toHaveAttribute('aria-busy', 'true');
      await expect(page.locator('#moonBox')).toHaveAttribute('aria-busy', 'false', { timeout: 20000 });
      await expect(page.locator('#sunBox')).toHaveAttribute('aria-busy', 'false');
      await expect(page.locator('#moonBox')).not.toContainText('Moon data unavailable');
      expect(extrasRequests).toBe(3);
      expect(pageRequests).toBe(1);
    } finally {
      releaseStalled();
    }
  });
}

test('forecast synopsis stays readable without enlarging the dashboard tile', async ({ page }) => {
  let synopsis = 'Sunny.';
  await page.route(url => url.pathname === '/api/weather-forecast', route => route.fulfill({ json: {
    ok: true, provider: 'us', hourly: [], days: [],
    current_24h: { overall: 'Sunny', synopsis, temp_range: '20–25°C', rh_range: '40–60%', wind: '1-4 m/s', precip_probability: 10 },
  } }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const tile = page.locator('#weatherForecastBox');
  await expect(page.locator('#forecastOverall')).toHaveText('Sunny.');
  const original = await tile.boundingBox();
  const longText = 'Today (NWS · original units): ' + 'Sunny, with a high near 80. West wind 5 to 10 mph. '.repeat(15);
  synopsis = longText;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('#forecastOverall')).toHaveText(longText.trim());
  const changed = await tile.boundingBox();
  expect(changed.width).toBe(original.width);
  expect(changed.height).toBe(original.height);
  await page.locator('#forecastOverall').focus();
  await page.keyboard.press('End');
  await expect.poll(() => page.locator('#forecastOverall').evaluate(el => el.scrollTop)).toBeGreaterThan(0);
});


test('moves the Switches row with the heading arrows and restores saved order', async ({ page }) => {
  await page.goto('/?switches=true', { waitUntil: 'domcontentloaded' });
  const rows = page.locator('.sensor-group[data-dashboard-row-id]');
  const switchRow = page.locator('#group_switches');
  await expect(rows.last()).toHaveAttribute('id', 'group_switches');
  await page.route('**/dashboard/metric-position', async (route) => {
    const body = route.request().postDataJSON();
    expect(body.sensor_id).toBe('__switches__');
    expect(body.include_switches).toBe(true);
    const order = body.direction === 'up'
      ? ['__switches__', 'aht-pr-check'] : ['aht-pr-check', '__switches__'];
    await route.fulfill({ json: { moved: true, order } });
  });
  await switchRow.getByRole('button', { name: 'Move Switches row up', exact: true }).click();
  await expect(rows.first()).toHaveAttribute('id', 'group_switches');
  await switchRow.getByRole('button', { name: 'Move Switches row down', exact: true }).click();
  await expect(rows.last()).toHaveAttribute('id', 'group_switches');
  await page.goto('/?switches=true&switches_first=true', { waitUntil: 'domcontentloaded' });
  await expect(rows.first()).toHaveAttribute('id', 'group_switches');
  await page.reload();
  await expect(rows.first()).toHaveAttribute('id', 'group_switches');
});

for (const scenario of [
  {name: 'automatic selection', before: '2026-09-15', after: '2026-09-16', selection: ''},
  {name: 'clicked current day', before: '2026-09-15', after: '2026-09-16', selection: '2026-09-15'},
  {name: 'cached next day', before: '2026-09-15', after: '2026-09-16', selection: '2026-09-16'},
  {name: 'another day at month end', before: '2026-10-31', after: '2026-11-01', selection: '2026-10-20'},
  {name: 'another month at year end', before: '2026-12-31', after: '2027-01-01', selection: '2026-11-20'},
]) {
  test(`BD calendar rollover updates ${scenario.name} in the calendar timezone`, async ({page}) => {
    // Browser midnight is earlier than Denver midnight, including winter standard time.
    const session = await page.context().newCDPSession(page);
    await session.send('Emulation.setTimezoneOverride', {timezoneId: 'UTC'});
    const offset = scenario.before === '2026-12-31' ? '-07:00' : '-06:00';
    await page.clock.install({time: new Date(`${scenario.before}T23:58:30${offset}`)});
    let today = scenario.before;
    const summaryDays = [];
    const calendarPayload = (month) => {
      const payload = successfulBiodynamicMonth(month);
      payload.astro.tz = 'America/Denver';
      payload.calendar.forEach(day => { day.is_today = day.date === today; });
      payload.notes = {[today]: `Note for ${today}`};
      return payload;
    };
    await page.route('**/api/biodynamic-calendar-app/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/calendar')) {
        return route.fulfill({json: calendarPayload(url.searchParams.get('month') || today.slice(0, 7))});
      }
      if (url.pathname.endsWith('/calendar-range')) {
        return route.fulfill({json: {ok: true, months: [calendarPayload(url.searchParams.get('start'))]}});
      }
      if (url.pathname.endsWith('/daily-summary')) {
        const day = url.searchParams.get('day');
        summaryDays.push(day);
        return route.fulfill({json: {ok: true, summary: `Suggestion: Guidance for ${day}, generated ${today}`}});
      }
      return route.continue();
    });
    await page.goto('/calendar');
    await expect(page.locator('#dailySummary')).toContainText(`Guidance for ${today}`);
    if (scenario.selection) {
      if (scenario.selection.slice(0, 7) !== today.slice(0, 7)) {
        await page.locator('#prevBtn').click();
      }
      await page.locator(`#calendar [data-date="${scenario.selection}"]`).click();
      await expect(page.locator('#dailySummary')).toContainText(`Guidance for ${scenario.selection}`);
    }
    const selectedBefore = scenario.selection || today;
    const requestCount = summaryDays.length;
    await page.clock.runFor(60_000);
    await expect(page.locator('#calendar .selected')).toHaveAttribute('data-date', selectedBefore);
    expect(summaryDays).toHaveLength(requestCount);
    today = scenario.after;
    await page.clock.runFor(60_000);
    await expect(page.locator('#calendar .selected')).toHaveAttribute('data-date', today);
    await expect(page.locator('#calendar .today')).toHaveAttribute('data-date', today);
    await expect(page.locator('#dailySummary')).toContainText(`Guidance for ${today}, generated ${today}`);
    await expect(page.locator('#noteInput')).toHaveAttribute('data-date', today);
    await expect(page.locator('#noteInput')).toHaveValue(`Note for ${today}`);
    expect(summaryDays.at(-1)).toBe(today);
  });
}
