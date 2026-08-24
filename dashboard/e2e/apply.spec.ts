import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

/**
 * The R24 apply control, in a real browser — reachable, named, and not
 * colour-only.
 *
 * WHY THIS FILE EXISTS. Until A10 was fixed the "Apply approved fix" button
 * could not render for ANY proposal: the gate read `action.target_path`, a key
 * `ck_proposals_action_vocabulary` (migration 0007) forbids, so "does this
 * proposal write a field?" answered `false` forever. Every a11y sweep here ran
 * over routes where the control was structurally absent, so nothing in the
 * gate had ever seen it. Now that it renders, it is held to the same four bars
 * as everything else: contrast, keyboard reach, an accessible name, and a state
 * that survives greyscale.
 *
 * The sweep in `a11y.spec.ts` discovers routes from the FIRST row of each list,
 * which is a pending proposal — so it still never reaches this screen. This
 * test navigates to an approved, field-writing proposal on purpose.
 *
 * C9's committed fix target is `appdb.enrollment.crm_deal_id`: on
 * `AUTO_APPLY_ELIGIBLE`, on no sensitive list, so an approved C9 proposal is
 * exactly the row the apply control exists for.
 */
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']

/** An approved, non-sensitive, field-writing proposal's detail path. */
async function approvedFieldWritingProposal(page: Page): Promise<string> {
  await page.goto('/proposals?type=C9&status=approved')
  await page.getByRole('table', { name: /proposals/i }).waitFor()
  const href = await page
    .locator('table a[href^="/proposals/"]')
    .first()
    .getAttribute('href')
  if (!href) {
    throw new Error(
      'no approved C9 proposal in the mock seed — this test would prove nothing',
    )
  }
  return href
}

test.describe('the apply control (R24)', () => {
  test('renders, is named, is keyboard-reachable, and passes axe', async ({
    page,
  }) => {
    const detail = await approvedFieldWritingProposal(page)
    await page.goto(detail)
    await page.getByRole('heading', { level: 1 }).waitFor()
    await page.waitForLoadState('networkidle')

    const apply = page.getByRole('button', { name: 'Apply approved fix' })
    await expect(apply).toBeVisible()

    // The screen says WHICH field it would write, not merely that it would.
    await expect(page.getByTestId('proposed-fix')).toContainText(
      'appdb.enrollment.crm_deal_id',
    )
    await expect(page.getByTestId('proposed-fix')).toContainText(
      'auto-apply eligible',
    )

    // Keyboard-reachable, and focusable without a mouse.
    await apply.focus()
    const focusedName = await page.evaluate(
      () => (document.activeElement as HTMLElement | null)?.textContent ?? '',
    )
    expect(focusedName.trim()).toBe('Apply approved fix')

    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
    console.log(
      `axe — approved proposal detail (${detail}): ` +
        `${results.violations.length} violation(s), ${results.passes.length} pass(es)`,
    )
    expect(
      results.violations.map((violation) => violation.id),
      JSON.stringify(results.violations, null, 2),
    ).toEqual([])
  })

  test('its inert state is announced and drawn, not only coloured', async ({
    page,
  }) => {
    const detail = await approvedFieldWritingProposal(page)
    await page.goto(detail)
    const apply = page.getByRole('button', { name: 'Apply approved fix' })
    await expect(apply).toBeVisible()

    // Active: no aria-disabled, and a solid border.
    await expect(apply).not.toHaveAttribute('aria-disabled', 'true')
    const activeBorder = await apply.evaluate(
      (node) => getComputedStyle(node).borderStyle,
    )
    expect(activeBorder).toBe('solid')

    await apply.click()

    // Inert while the decision is in flight, and afterwards `applied`: the
    // state is carried by `aria-disabled` (announced) AND by a border-style
    // change (survives greyscale), never by colour alone.
    await expect(page.getByTestId('decided-notice')).toContainText('Applied')
    const badge = page.locator('.status-badge').first()
    await expect(badge).toContainText('Applied')
    // Three channels on the status itself: text, an icon silhouette, a border.
    await expect(badge.locator('[data-status-icon]')).toHaveCount(1)
    const badgeBorder = await badge.evaluate(
      (node) => getComputedStyle(node).borderStyle,
    )
    expect(badgeBorder).toBe('double')
  })
})

/**
 * The TWO apply paths, and the reversal — in a real browser.
 *
 * `httpClient.applyProposal` sent no `auto` parameter from anywhere, so the
 * single "Apply approved fix" button above was the UNGATED reviewer write 100%
 * of the time and R24's guarded auto-apply could not be reached from the UI at
 * all. These sweeps hold the two new controls, and the reversal ledger, to the
 * same four bars as everything else: contrast, keyboard reach, an accessible
 * name, and a state that survives greyscale.
 *
 * The mock's gate (`mocks/mockClient.ts::mockGate`) evaluates the real
 * conditions against MOCK-ONLY confidence, so an approved C9 may legitimately
 * pass or be refused here. The invariant asserted is the one that matters and is
 * not a coin flip: an auto-apply always resolves to ONE of the two honest
 * outcomes — the ledger, or the refusal naming the condition — and never to a
 * silent nothing.
 */
test.describe('the two R24 write paths (auto vs reviewer)', () => {
  test('both are named, separately described, and keyboard-reachable', async ({
    page,
  }) => {
    await page.goto(await approvedFieldWritingProposal(page))
    await page.getByRole('heading', { level: 1 }).waitFor()

    const auto = page.getByRole('button', { name: 'Auto-apply (R24 gate)' })
    const manual = page.getByRole('button', { name: 'Apply approved fix' })
    await expect(auto).toBeVisible()
    await expect(manual).toBeVisible()

    // A reviewer must be able to tell WHICH ONE THEY ARE DOING before clicking,
    // and a screen-reader user must hear it on focus rather than hunting for the
    // paragraph — so each control carries its own description, not a shared one.
    const autoDescription = await auto.getAttribute('aria-describedby')
    const manualDescription = await manual.getAttribute('aria-describedby')
    expect(autoDescription).toBeTruthy()
    expect(manualDescription).toBeTruthy()
    expect(autoDescription).not.toBe(manualDescription)
    await expect(page.locator(`#${autoDescription}`)).toContainText(
      /machine authorises/i,
    )
    await expect(page.locator(`#${manualDescription}`)).toContainText(
      /does not run the gate/i,
    )

    await auto.focus()
    expect(
      await page.evaluate(
        () => (document.activeElement as HTMLElement | null)?.textContent ?? '',
      ),
    ).toBe('Auto-apply (R24 gate)')

    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
    console.log(
      `axe — both apply controls: ${results.violations.length} violation(s), ` +
        `${results.passes.length} pass(es)`,
    )
    expect(
      results.violations.map((violation) => violation.id),
      JSON.stringify(results.violations, null, 2),
    ).toEqual([])
  })

  test('an auto-apply resolves to the ledger or to a named refusal, never to nothing', async ({
    page,
  }) => {
    await page.goto(await approvedFieldWritingProposal(page))
    await page.getByRole('button', { name: 'Auto-apply (R24 gate)' }).click()

    const outcome = page.locator(
      '[data-testid="reversal-ledger"], [data-testid="auto-apply-refusal"]',
    )
    await outcome.first().waitFor()

    const refused = await page.getByTestId('auto-apply-refusal').count()
    if (refused > 0) {
      const panel = page.getByTestId('auto-apply-refusal')
      await expect(panel).toHaveAttribute('role', 'alert')
      // The condition is named, in words, and marked NOT met — never by colour.
      await expect(panel).toContainText('NOT met')
      await expect(panel).toContainText(/Nothing was written/i)
      // A refusal must NOT quietly become the reviewer write.
      await expect(page.locator('.status-badge').first()).toContainText(
        'Approved',
      )
    } else {
      await expect(page.getByTestId('reversal-ledger')).toContainText('applied')
      await expect(page.locator('.status-badge').first()).toContainText(
        'Applied',
      )
    }

    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
    console.log(
      `axe — auto-apply outcome (${refused > 0 ? 'refused' : 'applied'}): ` +
        `${results.violations.length} violation(s), ${results.passes.length} pass(es)`,
    )
    expect(
      results.violations.map((violation) => violation.id),
      JSON.stringify(results.violations, null, 2),
    ).toEqual([])
  })
})

/** An `applied` proposal's detail path — the only status a rollback is legal from. */
async function appliedProposal(page: Page): Promise<string> {
  await page.goto('/proposals?status=applied')
  await page.getByRole('table', { name: /proposals/i }).waitFor()
  const href = await page
    .locator('table a[href^="/proposals/"]')
    .first()
    .getAttribute('href')
  if (!href) {
    throw new Error(
      'no applied proposal in the mock seed — this test would prove nothing',
    )
  }
  return href
}

test.describe('the reversal ledger and the rollback control (R24)', () => {
  test('the ledger is a table with a caption, headers and no colour-only state', async ({
    page,
  }) => {
    await page.goto(await appliedProposal(page))
    const ledger = page.getByTestId('reversal-ledger')
    await ledger.waitFor()

    // The transaction id is the property the ledger is FOR, and reversibility is
    // stated in words rather than as a tick or a colour.
    await expect(ledger).toContainText('applied')
    await expect(ledger).toContainText(/before-image captured|NOT reversible/)
    await expect(ledger.locator('caption')).toHaveCount(1)
    expect(await ledger.locator('thead th[scope="col"]').count()).toBe(7)
    // The wide table scrolls inside its own region rather than the page body.
    await expect(page.locator('.table-scroll').first()).toBeVisible()

    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
    console.log(
      `axe — reversal ledger: ${results.violations.length} violation(s), ` +
        `${results.passes.length} pass(es)`,
    )
    expect(
      results.violations.map((violation) => violation.id),
      JSON.stringify(results.violations, null, 2),
    ).toEqual([])
  })

  test('rolling back appends the reversal leg and never erases the apply', async ({
    page,
  }) => {
    await page.goto(await appliedProposal(page))
    const rollback = page.getByRole('button', { name: 'Roll back this fix' })
    await expect(rollback).toBeVisible()
    expect(
      await page.locator('[data-testid="reversal-ledger"] tbody tr').count(),
    ).toBe(1)

    await rollback.click()
    await page.getByTestId('rollback-receipt').waitFor()

    // Append-only: both legs of the guarded path stay on the record.
    expect(
      await page.locator('[data-testid="reversal-ledger"] tbody tr').count(),
    ).toBe(2)
    const ledger = page.getByTestId('reversal-ledger')
    await expect(ledger).toContainText('applied')
    await expect(ledger).toContainText('rolled_back')
    await expect(page.locator('.status-badge').first()).toContainText(
      'Rolled back',
    )
    // Once reversed there is nothing left to reverse, so the control goes away
    // rather than sitting there disabled.
    await expect(
      page.getByRole('button', { name: 'Roll back this fix' }),
    ).toHaveCount(0)

    const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()
    console.log(
      `axe — after rollback: ${results.violations.length} violation(s), ` +
        `${results.passes.length} pass(es)`,
    )
    expect(
      results.violations.map((violation) => violation.id),
      JSON.stringify(results.violations, null, 2),
    ).toEqual([])
  })
})
