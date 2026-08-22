#!/usr/bin/env node
/**
 * Runs the Playwright accessibility suite (`playwright test --config=playwright.config.ts`),
 * but degrades to a loud SKIP when the Chromium build Playwright expects is not
 * present on this machine and cannot be fetched (offline CI, sandboxed runner).
 *
 * IMPORTANT: this guard ONLY covers "no browser binary". Once the browser is
 * present, the real `playwright test` runs and its exit code is propagated
 * unchanged — a failing a11y assertion still fails the command.
 */
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const args = process.argv.slice(2)

function playwright(argv, opts = {}) {
  return spawnSync('pnpm', ['exec', 'playwright', ...argv], {
    stdio: 'inherit',
    ...opts,
  })
}

/**
 * Ask Playwright where the Chromium build for THIS version would live, then
 * check the filesystem. Trusting `playwright install`'s exit status is not
 * enough: it exits 0 when PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD is set.
 */
function chromiumInstalled() {
  const probe = spawnSync(
    'pnpm',
    ['exec', 'playwright', 'install', '--dry-run', 'chromium'],
    { encoding: 'utf8' },
  )
  if (probe.status !== 0 || typeof probe.stdout !== 'string') return false
  const locations = [...probe.stdout.matchAll(/Install location:\s+(\S+)/g)].map(
    (m) => m[1],
  )
  return locations.length > 0 && locations.some((p) => existsSync(p))
}

let present = chromiumInstalled()

if (!present) {
  // Last chance: try to fetch it. Offline runners fail here, which is fine.
  playwright(['install', '--with-deps', 'chromium'])
  present = chromiumInstalled()
}

if (!present) {
  const bar = '='.repeat(72)
  console.warn(
    `\n${bar}\n` +
      'SKIPPED: pnpm test:a11y — Playwright Chromium is not installed and could\n' +
      'not be downloaded (offline?). The accessibility gate did NOT run.\n' +
      'Install it with:  pnpm exec playwright install --with-deps chromium\n' +
      `${bar}\n`,
  )
  process.exit(0)
}

const result = playwright(['test', '--config=playwright.config.ts', ...args])
process.exit(result.status ?? 1)
