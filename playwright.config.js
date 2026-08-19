// @ts-check
import { defineConfig, devices } from '@playwright/test';

const host = process.env.SENSORIUS_PLAYWRIGHT_HOST || '127.0.0.1';
const port = process.env.SENSORIUS_PLAYWRIGHT_PORT || '8771';
const baseURL = process.env.SENSORIUS_PLAYWRIGHT_BASE_URL || `http://${host}:${port}`;
const python = process.env.SENSORIUS_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: process.env.SENSORIUS_PLAYWRIGHT_BASE_URL ? undefined : {
    command: `${python} -m uvicorn testApparatus.playwright_host:app --host ${host} --port ${port}`,
    url: `${baseURL}/healthz`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
