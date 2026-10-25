import { test, expect } from '@playwright/test';

// Allow the added FL/WC rows and legend while keeping all forecast content bounded.
const cardBudgets = [{ width: 1440, height: 546 }, { width: 1024, height: 620 }, { width: 390, height: 810 }];
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
      await expect(history).toContainText('Historical Min, Max, Avg 1991–2026 for 18-Sep');
      await expect(history).toHaveAttribute('title', /through 2026-09-13.*35 samples/);
      await expect(history.locator('[data-history-value="temperature_c"]')).toHaveText(units === 'Metric' ? 'Min 6.7°C (1993)Max 30.7°C (2016)Avg 20.0°C' : 'Min 44.1°F (1993)Max 87.3°F (2016)Avg 68.0°F');
      await expect(history.locator('[data-history-value="humidity_pct"]')).toHaveText('Min 10% (1993)Max 100% (2016)Avg 55%');
      await expect(history.locator('[data-history-value="wind_kmh"]')).toHaveText(units === 'Metric' ? 'Min 0.3 km/h (1993)Max 28.2 km/h (2016)Avg 16.1 km/h' : 'Min 0.2 mph (1993)Max 17.5 mph (2016)Avg 10.0 mph');
      await expect(history.locator('[data-history-value="rain_mm"]')).toHaveText(units === 'Metric' ? 'Min 0.0 mm (1993)Max 21.1 mm (2016)Avg 2.5 mm' : 'Min 0.00 in (1993)Max 0.83 in (2016)Avg 0.10 in');
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
  await expect(row.locator('[data-history-value]')).toHaveText(Array(4).fill('Min —Max —Avg —'));
  expect((await page.locator('.forecast-panel').boundingBox()).height).toBe(height);
});


for (const width of [1440, 390]) {
  test(`thermal readings and hourly estimates render at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort());
    for (const station of ['ecowitt-pr-check', 'weewx-station', 'custom-weather-station', 'aht-local']) {
      for (const units of ['Imperial', 'Metric']) {
        await page.goto(`/weather-forecast?thermal=true&station=${station}&units=${units}`);
        const current = page.locator('[data-readings-thermal]');
        if (station === 'aht-local') {
          await expect(current).toBeHidden();
        } else {
          await expect(current).toBeVisible();
          await expect(current).toContainText(units === 'Imperial' ? 'Feels Like5.9 °FWind Chill6.2 °F' : 'Feels Like-14.5 °CWind Chill-14.3 °C');
          expect(await current.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
        }
        const hour = page.locator('.forecast-hour:visible').first();
        await expect(hour.locator('[aria-label="Feels Like"]')).toContainText(units === 'Imperial' ? 'FL 87.6°F' : 'FL 30.9°C');
        await expect(hour.locator('[aria-label="Wind Chill"]')).toHaveText('WC —');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        if (station === 'ecowitt-pr-check') {
          await page.locator('.conditions-row').screenshot({ path: testInfo.outputPath(`thermal-${width}-${units}.png`) });
        }
      }
    }
  });
}


test('thermal current readings refresh and clear when the selection changes', async ({ page }) => {
  await page.clock.install();
  let thermal = [{ key: 'feels_like', name: 'Feels Like', value: 37.3, unit: '°F' }, { key: 'wind_chill', name: 'Wind Chill', value: null, unit: '°F' }];
  await page.route('**/api/weather-forecast-app/current-readings', route => route.fulfill({ json: {
    ok: true, sensor_id: 'weewx-station', is_weewx: true,
    display_metrics: [{ name: 'Temperature_F', value: 39.9, unit: '°F' }],
    thermal_metrics: thermal, refresh_interval_sec: 60,
  } }));
  await page.goto('/weather-forecast?thermal=true');
  const current = page.locator('[data-readings-thermal]');
  await page.clock.fastForward(61000);
  await expect(current).toHaveText('Feels Like37.3 °FWind Chill—');
  thermal = [];
  await page.clock.fastForward(61000);
  await expect(current).toBeHidden();
  await expect(current).toBeEmpty();
});
