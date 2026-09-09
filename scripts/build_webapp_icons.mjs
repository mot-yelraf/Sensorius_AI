/** Rasterize the existing Sensorius SVG for web-app launchers.
 * The maskable variant keeps the logo inside Android's circular safe zone.
 */
import { readFile, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const root = new URL('../', import.meta.url);
const svg = await readFile(new URL('ui_static/favicon.svg', root), 'utf8');
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  for (const [name, size, scale] of [
    ['sensorius-192', 192, 1],
    ['sensorius-512', 512, 1],
    ['sensorius-maskable-512', 512, 0.66],
  ]) {
    const data = await page.evaluate(async ({ svg, size, scale }) => {
      const img = new Image();
      img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
      await img.decode();
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = size;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, size, size);
      const inset = size * (1 - scale) / 2;
      ctx.drawImage(img, inset, inset, size * scale, size * scale);
      return canvas.toDataURL('image/png').split(',')[1];
    }, { svg, size, scale });
    await writeFile(new URL(`ui_static/icons/${name}.png`, root), Buffer.from(data, 'base64'));
  }
} finally {
  await browser.close();
}
