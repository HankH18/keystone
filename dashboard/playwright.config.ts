import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for the accessibility gate (R12): the per-route axe sweep,
 * the keyboard-only walkthrough, and the computed-contrast check.
 *
 * The dev server is started by Playwright itself so `pnpm test:a11y` is one
 * command, and it is started WITH VITE_USE_MOCK_API=1 — the service API does
 * not exist yet (T-5/T-7/T-8), and an a11y sweep over empty error states would
 * prove nothing. It runs on its own port and never reuses an existing server,
 * so it can never accidentally audit a `pnpm dev` process started without the
 * flag.
 */
const PORT = 5199
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
    reuseExistingServer: false,
    env: { VITE_USE_MOCK_API: '1' },
    stdout: 'ignore',
    stderr: 'pipe',
    timeout: 120_000,
  },
})
