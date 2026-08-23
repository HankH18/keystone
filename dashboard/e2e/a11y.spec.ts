import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import { discoverRoutes } from './routes'

/**
 * axe-core over EVERY route (R12), including a row-detail view.
 *
 * THE GATE IS "ZERO VIOLATIONS", AT ANY IMPACT. It used to fail only on
 * serious/critical and merely print the rest. The result was true — the routes
 * had none of any impact — but the ASSERTION was weaker than the result, so a
 * future moderate WCAG regression would have shipped behind a console line
 * nobody reads. An assertion that is looser than the thing it protects is not
 * protecting it.
 *
 * THE TAG LIST INCLUDES best-practice. Running only the four wcag tags left
 * axe's best-practice ruleset switched off, and it was hiding two real defects:
 * `region` (the mock-data banner sat outside every landmark, so the one
 * population that most needs "this data is fake" — screen-reader users
 * navigating by landmark — was the population that never got it) and
 * `landmark-unique` on the overview. Both are fixed; this tag list is what
 * stops them coming back.
 *
 * There is no dialog in this dashboard: the detail views are routes, not
 * modals, so there is no focus trap to test and nothing behind an overlay.
 */
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']

test.describe('accessibility', () => {
  test('every route has ZERO axe violations at any impact', async ({ page }) => {
    const routes = await discoverRoutes(page)
    const failures: string[] = []

    for (const route of routes) {
      await page.goto(route.path)
      await page.getByRole('heading', { level: 1 }).waitFor()
      // Let the queries settle so axe sees the real, populated page.
      await page.waitForLoadState('networkidle')

      const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()

      const summary = results.violations
        .map(
          (v) =>
            `    [${v.impact ?? 'unknown'}] ${v.id}: ${v.help} (${v.nodes.length} node(s))`,
        )
        .join('\n')
      console.log(
        `axe — ${route.name} (${route.path}): ` +
          `${results.violations.length} violation(s), ` +
          `${results.passes.length} pass(es), ` +
          `${results.incomplete.length} incomplete\n${summary}`,
      )

      for (const violation of results.violations) {
        failures.push(
          `${route.path} — [${violation.impact ?? 'unknown'}] ${violation.id}: ${violation.help}\n` +
            violation.nodes.map((n) => `      ${n.target.join(' ')}`).join('\n'),
        )
      }
    }

    expect(
      failures,
      `axe violations (tags: ${TAGS.join(', ')}):\n${failures.join('\n')}`,
    ).toEqual([])
  })

  test('the shell exposes the landmarks and the skip link', async ({ page }) => {
    await page.goto('/conflicts')
    await expect(page.getByRole('main')).toHaveAttribute('id', 'main-content')
    await expect(
      page.getByRole('navigation', { name: 'Primary' }),
    ).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1)
    await expect(
      page.getByRole('link', { name: /skip to main content/i }),
    ).toHaveAttribute('href', '#main-content')
  })

  /**
   * MOCK HONESTY, as a landmark.
   *
   * This run is the only place the banner is rendered (the vitest suite builds
   * the app with VITE_USE_MOCK_API unset, so the real client is the default),
   * which made the banner deletable with both suites green. It is the single
   * signal that separates "the reviewer is looking at the service" from "the
   * reviewer is looking at a seeded fake", so it is asserted here — including
   * that it is reachable by landmark navigation, which is what the `region`
   * violation was really about.
   */
  test('the mock-data banner is present, inside a landmark, and says the data is fake', async ({
    page,
  }) => {
    for (const path of ['/', '/conflicts', '/proposals']) {
      await page.goto(path)
      await page.getByRole('heading', { level: 1 }).waitFor()

      const notice = page.getByRole('complementary', {
        name: 'Data source notice',
      })
      await expect(notice).toBeVisible()
      await expect(notice).toContainText(/mock data/i)
      await expect(notice).toContainText(/not connected/i)
      await expect(notice).toContainText(/simulated/i)
    }
  })

  /**
   * The tables live in labelled scroll regions and the pagination in a named
   * <nav>. axe checks that a scrollable region is FOCUSABLE, never that it is
   * NAMED, and it cannot check that a name is useful, so both are asserted.
   */
  test('the table scroll region and the pagination nav carry accessible names', async ({
    page,
  }) => {
    await page.goto('/conflicts')
    await expect(
      page.getByRole('region', { name: /^Conflicts — \d+ match the current filters$/ }),
    ).toBeVisible()
    await expect(
      page.getByRole('navigation', { name: 'conflicts pagination' }),
    ).toBeVisible()

    await page.goto('/proposals')
    await expect(
      page.getByRole('region', { name: /^Proposals — \d+ match the current filters$/ }),
    ).toBeVisible()
    await expect(
      page.getByRole('navigation', { name: 'proposals pagination' }),
    ).toBeVisible()
  })

  /** Every landmark on the overview is distinguishable by name (landmark-unique). */
  test('the overview has no two landmarks with the same name', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('heading', { level: 1 }).waitFor()
    await page.waitForLoadState('networkidle')

    const names = await page
      .locator(
        'main [role="region"], main section[aria-labelledby], main section[aria-label]',
      )
      .evaluateAll((nodes) =>
        nodes.map((node) => {
          const label = node.getAttribute('aria-label')
          if (label) return label
          const id = node.getAttribute('aria-labelledby')
          return id ? (document.getElementById(id)?.textContent ?? '') : ''
        }),
      )

    expect(names.length).toBeGreaterThan(3)
    expect(new Set(names).size).toBe(names.length)
  })
})
