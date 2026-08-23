#!/usr/bin/env node
/**
 * Runs the Playwright accessibility suite (`playwright test --config=playwright.config.ts`).
 *
 * MISSING BROWSER IS A FAILURE, NOT A SKIP. The T-0 version of this script
 * exited 0 when Chromium was absent, which produced a green accessibility gate
 * that had never run — the worst possible outcome for a requirement (R12) whose
 * whole point is that it is tested rather than intended. The skip now requires
 * an explicit ALLOW_A11Y_SKIP=1 from whoever is choosing to run blind, and it
 * still says loudly that the gate did not run.
 *
 * This guard ONLY covers "no browser binary". Once the browser is present, the
 * real `playwright test` runs and its exit code is propagated unchanged — a
 * failing a11y assertion still fails the command.
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
  const allowSkip = process.env.ALLOW_A11Y_SKIP === '1'
  const message =
    `\n${bar}\n` +
    'Playwright Chromium is not installed and could not be downloaded\n' +
    '(offline?). THE ACCESSIBILITY GATE DID NOT RUN.\n' +
    'Install it with:  pnpm exec playwright install --with-deps chromium\n' +
    `${bar}\n`

  if (allowSkip) {
    console.warn(`${message}Skipping because ALLOW_A11Y_SKIP=1 was set.\n`)
    process.exit(0)
  }

  console.error(
    `${message}Failing rather than reporting a green gate that never ran.\n` +
      'Set ALLOW_A11Y_SKIP=1 to override deliberately.\n',
  )
  process.exit(1)
}

const result = playwright(['test', '--config=playwright.config.ts', ...args])
process.exit(result.status ?? 1)
