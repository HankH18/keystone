import type { Page } from '@playwright/test'

/**
 * The five routes, discovered rather than hardcoded where they carry an id.
 *
 * The detail routes need a real id, and inventing one would test the
 * "not found" page instead of the row detail — so the ids are read off the
 * first row of each list, the same way a reviewer reaches them.
 */
export interface RouteUnderTest {
  name: string
  path: string
}

export async function discoverRoutes(page: Page): Promise<RouteUnderTest[]> {
  await page.goto('/conflicts')
  await page.getByRole('table', { name: /conflicts/i }).waitFor()
  const conflictHref = await page
    .locator('table a[href^="/conflicts/"]')
    .first()
    .getAttribute('href')

  await page.goto('/proposals')
  await page.getByRole('table', { name: /proposals/i }).waitFor()
  const proposalHref = await page
    .locator('table a[href^="/proposals/"]')
    .first()
    .getAttribute('href')

  if (!conflictHref || !proposalHref) {
    throw new Error(
      'Could not discover a conflict/proposal detail route — is the mock seeded?',
    )
  }

  return [
    { name: 'overview', path: '/' },
    { name: 'conflicts list', path: '/conflicts' },
    { name: 'conflicts list, filtered', path: '/conflicts?type=C14&page=2&page_size=5' },
    { name: 'conflict detail', path: conflictHref },
    { name: 'proposals list', path: '/proposals' },
    { name: 'proposals list, filtered', path: '/proposals?status=sensitive_hold' },
    { name: 'proposal detail', path: proposalHref },
    { name: 'audit log', path: '/audit' },
    { name: 'not found', path: '/no-such-page' },
  ]
}
