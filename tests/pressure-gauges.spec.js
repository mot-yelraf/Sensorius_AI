import { test, expect } from '@playwright/test';
import { pressureGaugeConfig } from '../ui_static/js/pressure_gauges.js';

const base = { unit: 'hPa', min: 700, max: 1100 };
test('pressure ranges cover altitude bounds, fallback and corrected readings', () => {
  for (const altitude of ['', null, undefined, 'NaN', Infinity, -501, 10001]) {
    expect(pressureGaugeConfig('Baro-Pressure', base, altitude)).toBe(base);
  }
  for (const altitude of [-500, 0, 1609.3, 10000]) {
    const scale = pressureGaugeConfig('Pressure', base, altitude);
    const center = 1013.25 * Math.pow(1 - altitude / 44330, 5.255);
    expect(center - scale.min).toBeGreaterThanOrEqual(50);
    expect(scale.max - center).toBeGreaterThanOrEqual(50);
    expect(scale.ticks[0]).toBe(scale.min);
    expect(scale.ticks.at(-1)).toBe(scale.max);
  }
  for (const device of ['bme280', 'bme680', 'bme688', 'vpd', 'avpd', 'apvpd', 'aqi', 'ecowitt', 'weewx']) {
    const context = { device, altitude: 1609.3 };
    expect(pressureGaugeConfig('Baro-Pressure', base, 1609.3, context).min).toBe(960);
    expect(pressureGaugeConfig('Absolute Baro-Pressure', base, 1609.3, context).min).toBe(780);
  }
  expect(pressureGaugeConfig('Temperature', base, 1609.3)).toBe(base);
  expect(base).toEqual({ unit: 'hPa', min: 700, max: 1100 });
});

for (const units of ['Metric', 'Imperial']) {
  test(`mixed pressure sensors render altitude scales in ${units}`, async ({ page }, testInfo) => {
    await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
    await page.addInitScript(() => {
      window.pressureTestGauges = [];
      window.Gauge = class {
        constructor(canvas) { this.id = canvas.id; window.pressureTestGauges.push(this); }
        setOptions(options) { this.options = options; return this; }
        setMinValue(value) { this.min = value; }
        set(value) { this.value = value; }
        render() {}
      };
    });
    await page.goto(`/pressure-gauges?unit_system=${units}`, { waitUntil: 'domcontentloaded' });
    await expect.poll(() => page.evaluate(() => window.pressureTestGauges.length)).toBe(5);
    const gauges = await page.evaluate(() => window.pressureTestGauges.map(g => ({ id: g.id, min: g.min, max: g.maxValue, ticks: g.options.staticLabels.labels, value: g.value })));
    for (const gauge of gauges) {
      const corrected = gauge.id.startsWith('corrected_') || gauge.id === 'ecowitt_Gateway_Baro-PressureGauge';
      const raw = gauge.id.startsWith('raw_') || gauge.id.includes('Absolute');
      expect(corrected || raw).toBe(true);
      expect([gauge.min, gauge.max]).toEqual(units === 'Metric' ? (corrected ? [960, 1070] : [780, 890]) : (corrected ? [28.4, 31.4] : [23.1, 26.2]));
      expect(gauge.ticks[0]).toBe(gauge.min);
      expect(gauge.ticks.at(-1)).toBe(gauge.max);
    }
    await expect(page.locator('#corrected_Baro-Pressure_val')).toContainText(units === 'Metric' ? '1022.5 hPa' : '30.19 inHg');
    await page.screenshot({ path: testInfo.outputPath(`pressure-${units}.png`), fullPage: true });
  });
}

test('fallback canvas keeps altitude bounds after a live update', async ({ page }, testInfo) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.route('**/*json_only=true*', route => route.fulfill({ json: {
    available: ['raw'], values: { raw: { 'Baro-Pressure': 845 } }, timestamps: { raw: '2026-09-18T12:00:00' }, stats: {},
  } }));
  await page.addInitScript(() => {
    window.pressureLabels = [];
    const fillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
      if (this.canvas.id === 'raw_Baro-PressureGauge') window.pressureLabels.push(String(text));
      return fillText.call(this, text, ...args);
    };
  });
  await page.goto('/pressure-gauges', { waitUntil: 'load' });
  await page.evaluate(() => { window.pressureLabels = []; return window.updateGauges({ ignoreVisibility: true, ignoreModal: true }); });
  await expect(page.locator('#raw_Baro-Pressure_val')).toContainText('845.0 hPa');
  const labels = await page.evaluate(() => window.pressureLabels);
  expect(labels).toContain('780');
  expect(labels).toContain('890');
  expect(labels).not.toContain('700');
  await page.screenshot({ path: testInfo.outputPath('pressure-fallback.png'), fullPage: true });
});
