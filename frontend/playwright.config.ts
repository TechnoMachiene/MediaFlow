import { defineConfig, devices } from '@playwright/test';

/* Browser end-to-end tests against the real app in demo mode (see ../scripts/e2e_server.py).
 * Needs Redis (REDIS_URL, default localhost) and Python with the app's requirements;
 * PYTHON picks the interpreter (default: the repo's .venv, else python3). */
const port = Number(process.env.E2E_PORT ?? 8093);
const python = process.env.PYTHON ?? '../.venv/bin/python';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1, // one shared demo database; specs are independent but run in turn
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', use: { ...devices['Pixel 7'] } },
  ],
  webServer: {
    command: `npm run build && ${python} ../scripts/e2e_server.py ${port}`,
    url: `http://127.0.0.1:${port}/health/ready`,
    timeout: 120_000,
    reuseExistingServer: false,
    // SIGTERM (not the default SIGKILL) lets the launcher delete its temp data and Redis keys.
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
