import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for the axe-core accessibility gate (R12).
 *
 * T-0 ships a single smoke spec against the placeholder shell. T-10 adds a
 * per-route axe sweep (zero serious/critical) plus the keyboard-only walkthrough.
 * The dev server is started by Playwright itself so `pnpm test:a11y` is one command.
 */
const PORT = 5173
// `localhost`, not `127.0.0.1`: Vite 8 binds the loopback name (which resolves
// to ::1 on macOS), so probing the IPv4 literal gets ECONNREFUSED.
const BASE_URL = `http://localhost:${PORT}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: `pnpm exec vite --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 120_000,
  },
})
