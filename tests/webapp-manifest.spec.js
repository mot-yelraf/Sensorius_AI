// @ts-check
import { test, expect } from '@playwright/test';

test('dashboard exposes a valid launcher manifest and loadable Android icons', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/**', route => route.fulfill({ body: '' }));
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const manifestURL = await page.locator('link[rel="manifest"]').evaluate(link => link.href);
  const response = await page.request.get(manifestURL);
  expect(response.ok()).toBeTruthy();
  expect(response.headers()['content-type']).toContain('application/manifest+json');
  const manifest = await response.json();
  const origin = new URL(page.url()).origin;
  expect(new URL(manifest.start_url, manifestURL).href).toBe(`${origin}/`);
  expect(new URL(manifest.scope, manifestURL).href).toBe(`${origin}/`);
  expect(manifest.display).toBe('standalone');
  expect(manifest.icons.filter(icon => icon.purpose === 'any').map(icon => icon.sizes)).toEqual(['192x192', '512x512']);
  expect(manifest.icons.some(icon => icon.purpose === 'maskable')).toBeTruthy();
  for (const icon of manifest.icons) {
    const info = await page.evaluate(async ({ icon, manifestURL }) => {
      const image = new Image();
      image.src = new URL(icon.src, manifestURL).href;
      await image.decode();
      return `${image.naturalWidth}x${image.naturalHeight}`;
    }, { icon, manifestURL });
    expect(info).toBe(icon.sizes);
  }
  const session = await page.context().newCDPSession(page);
  const parsed = await session.send('Page.getAppManifest');
  expect(parsed.errors).toEqual([]);
  expect(parsed.url).toBe(manifestURL);
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveCount(1);
});
