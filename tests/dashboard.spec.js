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

test('keeps the overview graphic below sensors after moving the bottom sensor up', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => {
    const footer = document.getElementById('dashboard-overview-footer');
    const dashboardContent = footer?.parentElement;
    const source = dashboardContent?.querySelector('.sensor-group[data-sensor-id]');
    if (!dashboardContent || !source || !footer) throw new Error('dashboard fixture is incomplete');
    const ecowitt = source.cloneNode(true);
    ecowitt.id = 'group_ecowitt-bottom-test';
    ecowitt.dataset.sensorId = 'ecowitt-bottom-test';
    ecowitt.querySelectorAll('[data-sensor-id]').forEach((element) => {
      element.setAttribute('data-sensor-id', 'ecowitt-bottom-test');
    });
    dashboardContent.insertBefore(ecowitt, footer);
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
  await ecowitt.locator('.sensor-order-btn').click();
  await ecowitt.locator(".sensor-order-item[data-move='up']").click();
  await expect.poll(() => reordered).toBe(true);
  await expect.poll(() => page.evaluate(() => document.getElementById('dashboard-overview-footer')?.parentElement?.lastElementChild?.id)).toBe('dashboard-overview-footer');

  const footerTop = await page.locator('#dashboard-overview-footer').evaluate((element) => element.getBoundingClientRect().top + window.scrollY);
  const sensorBottom = await page.locator('.sensor-group[data-sensor-id]').evaluateAll((elements) =>
    Math.max(...elements.map((element) => element.getBoundingClientRect().bottom + window.scrollY)),
  );
  expect(footerTop).toBeGreaterThanOrEqual(sensorBottom);
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
  expect(chartBox?.height).toBeLessThanOrEqual(72);

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
