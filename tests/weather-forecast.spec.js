import { test, expect } from '@playwright/test';

// Heights measured on the existing card before adding the RH and wind rows.
const cardBudgets = [{ width: 1440, height: 546 }, { width: 1024, height: 580 }, { width: 390, height: 779 }];
for (const budget of cardBudgets) {
  test(`Caelus daily ranges fit the existing card at ${budget.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: budget.width, height: 1000 });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      return url.hostname === '127.0.0.1' || url.hostname === 'localhost' ? route.continue() : route.abort();
    });
    for (const units of ['Metric', 'Imperial']) {
      const response = await page.goto(`/weather-forecast?units=${units}`, { waitUntil: 'domcontentloaded' });
      expect(response.ok()).toBe(true);
      const card = page.locator('.forecast-panel');
      await expect(card).not.toContainText('Updated from');
      await expect(card.getByRole('button', { name: '6-day details' })).toHaveCount(0);
      await expect(card.locator('.forecast-synopsis')).toContainText('then rain showers around 8 PM');
      await expect(card.locator('.forecast-hour:visible').first()).toContainText('RH 55%');
      await expect(card.locator('.forecast-hour:visible').first()).toContainText(units === 'Metric' ? 'Wind 14 km/h' : 'Wind 9 mph');
      const initialHeight = (await card.boundingBox()).height;
      const history = card.locator('[data-weather-history]');
      await expect(history).toHaveAttribute('data-status', 'ready');
      await expect(history).toContainText('Historical daily average 1991–2026 for 18-Sep-2026');
      await expect(history).toHaveAttribute('title', /through 2026-09-13.*35 samples/);
      await expect(history.locator('[data-history-value="temperature_c"]')).toHaveText(units === 'Metric' ? '20.0°C' : '68.0°F');
      await expect(history.locator('[data-history-value="humidity_pct"]')).toHaveText('55%');
      await expect(history.locator('[data-history-value="wind_kmh"]')).toHaveText(units === 'Metric' ? '16.1 km/h' : '10.0 mph');
      await expect(history.locator('[data-history-value="rain_mm"]')).toHaveText(units === 'Metric' ? '2.5 mm' : '0.10 in');
      const originalHeight = (await card.boundingBox()).height;
      expect(originalHeight).toBe(initialHeight);
      const days = card.locator('.forecast-day');
      await expect(days).toHaveCount(6);
      for (const day of await days.all()) {
        await expect(day.locator('time')).toBeVisible();
        await expect(day.locator('.forecast-glyph')).toBeVisible();
        await expect(day).toContainText(units === 'Metric' ? '17.8-30.0°C' : '64-86°F');
        await expect(day).toContainText('RH 35-85%');
        await expect(day).toContainText(units === 'Metric' ? 'Wind 4-29 km/h' : 'Wind 2-18 mph');
        await expect(day).toContainText('Rain 59%');
        expect(await day.evaluate(element => Array.from(element.querySelectorAll('time, strong, small')).every(row => row.scrollWidth <= row.clientWidth + 1))).toBe(true);
      }
      expect((await card.boundingBox()).height).toBeLessThanOrEqual(budget.height);
      await card.screenshot({ path: testInfo.outputPath(`forecast-${units}.png`) });
      await card.getByRole('button', { name: 'Show next forecast hour' }).click();
      await expect(card.locator('[data-hourly-status]')).toHaveText('Hours 2–9 of 24');
      await page.goto(`/weather-forecast?units=${units}&native=true`);
      const synopsis = card.locator('.forecast-synopsis');
      await expect(synopsis).toContainText('This Afternoon (NWS · original units)');
      expect((await card.boundingBox()).height).toBe(originalHeight);
      await synopsis.focus();
      await page.keyboard.press('End');
      await expect.poll(() => synopsis.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
    }
  });
}


test('all nine bundled glyphs render without remote assets or emoji fonts', async ({ page }, testInfo) => {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return ['127.0.0.1', 'localhost'].includes(url.hostname) ? route.continue() : route.abort();
  });
  await page.goto('/weather-forecast');
  const glyphs = ['sunny', 'partly-cloudy', 'cloudy', 'rain', 'snow', 'thunder', 'fog', 'clear-night', 'partly-cloudy-night'];
  await page.setContent(`<body style="background:#163c49;color:white;font-family:Arial;display:flex;flex-wrap:wrap;gap:20px;padding:24px">${glyphs.map(key => `<figure style="margin:0;text-align:center;width:160px"><img alt="${key}" width="96" height="96" src="/ui_static/weather_forecast/glyphs/${key}.svg"><figcaption>${key}</figcaption></figure>`).join('')}</body>`);
  await page.locator('img').evaluateAll(images => Promise.all(images.map(image => image.decode())));
  for (const img of await page.locator('img').all()) {
    expect(await img.evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
  }
  await page.screenshot({ path: testInfo.outputPath('nine-weather-glyphs.png') });
});


test('historical averages load asynchronously and show missing values without changing card height', async ({ page }) => {
  let calls = 0;
  await page.route('**/api/weather-climate', route => route.fulfill({ json: ++calls === 1
    ? { status: 'warming' } : { status: 'unavailable' } }));
  await page.goto('/weather-forecast', { waitUntil: 'domcontentloaded' });
  const row = page.locator('[data-weather-history]');
  await expect(row).toHaveAttribute('data-status', 'warming');
  const height = (await page.locator('.forecast-panel').boundingBox()).height;
  await expect(row).toHaveAttribute('data-status', 'unavailable', { timeout: 10000 });
  await expect(row.locator('[data-history-value]')).toHaveText(['—', '—', '—', '—']);
  expect((await page.locator('.forecast-panel').boundingBox()).height).toBe(height);
});
