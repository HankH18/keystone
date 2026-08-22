import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

/**
 * T-0 accessibility smoke. The full per-route sweep and the keyboard-only
 * walkthrough are T-10's acceptance criteria; this proves the harness is real
 * (browser launches, axe injects, violations would fail the run).
 */
test.describe('shell accessibility', () => {
  test('renders the Keystone heading and skip link', async ({ page }) => {
    await page.goto('/')
    await expect(
      page.getByRole('heading', { level: 1, name: 'Keystone' }),
    ).toBeVisible()
    await expect(page.getByRole('main')).toHaveAttribute('id', 'main-content')
  })

  test('has zero serious or critical axe violations', async ({ page }) => {
    await page.goto('/')
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()

    const blocking = results.violations.filter(
      (v) => v.impact === 'serious' || v.impact === 'critical',
    )
    expect(
      blocking,
      `axe violations:\n${blocking.map((v) => `${v.id}: ${v.help}`).join('\n')}`,
    ).toEqual([])
  })
})
