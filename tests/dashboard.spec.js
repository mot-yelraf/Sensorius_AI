// @ts-check
import { test, expect } from '@playwright/test';

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
