import { expect, test, type Page } from '@playwright/test'

/**
 * A genuine keyboard-only walkthrough: tab to a conflict, open it, approve it.
 *
 * NOT ONE MOUSE EVENT. Every interaction is `page.keyboard`. Every step asserts
 * WHERE FOCUS IS, not merely that the DOM changed — a test that only checked
 * the DOM would pass on a page that silently dumps focus onto <body>, which is
 * the failure this requirement exists to catch.
 */

interface Focused {
  tag: string
  id: string
  text: string
  href: string | null
  /** The control's accessible NAME — a labelled <select> is named by its label. */
  name: string
}

async function focusDescriptor(page: Page): Promise<Focused> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null
    const labelFor = el?.id
      ? document.querySelector(`label[for="${el.id}"]`)?.textContent
      : null
    const name = (
      el?.getAttribute('aria-label') ??
      labelFor ??
      (el instanceof HTMLSelectElement || el instanceof HTMLInputElement
        ? ''
        : (el?.textContent ?? ''))
    )
      .trim()
      .slice(0, 80)
    return {
      tag: el?.tagName ?? 'NONE',
      id: el?.id ?? '',
      text: (el?.textContent ?? '').trim().slice(0, 80),
      href: el?.getAttribute('href') ?? null,
      name,
    }
  })
}

/** Tab until `predicate` holds. Returns the number of Tab presses used. */
async function tabUntil(
  page: Page,
  predicate: (d: Focused) => boolean,
  limit = 40,
): Promise<number> {
  for (let presses = 1; presses <= limit; presses += 1) {
    await page.keyboard.press('Tab')
    if (predicate(await focusDescriptor(page))) return presses
  }
  throw new Error(`focus never reached the target within ${limit} Tab presses`)
}

test.describe('keyboard-only reviewer walkthrough', () => {
  test('tab to a conflict, open it, and approve its proposal without a mouse', async ({
    page,
  }) => {
    await page.goto('/conflicts?page_size=10')
    await page.getByRole('table', { name: /conflicts/i }).waitFor()

    // --- Step 1: the very first Tab reaches the skip link ------------------
    await page.keyboard.press('Tab')
    let focus = await focusDescriptor(page)
    expect(focus.tag).toBe('A')
    expect(focus.href).toBe('#main-content')
    expect(focus.text.toLowerCase()).toContain('skip to main content')

    // --- Step 2: focus order is DOM order through the shell ---------------
    // Record what the next few tabs land on and assert it is the document
    // order (nav links, then the filter controls), never a jump.
    const order: string[] = []
    for (let i = 0; i < 8; i += 1) {
      await page.keyboard.press('Tab')
      const current = await focusDescriptor(page)
      order.push(current.name)
    }
    // Header nav first, then the filters in the order they are painted, each
    // one reached by its own accessible NAME — an unlabelled control would show
    // up here as an empty string.
    expect(order).toEqual([
      'Overview',
      'Conflicts',
      'Proposals',
      'Audit log',
      'Source',
      'Conflict type',
      'Status',
      'Clear filters',
    ])

    // Shift+Tab walks the same order backwards.
    await page.keyboard.press('Shift+Tab')
    expect((await focusDescriptor(page)).name).toBe('Status')
    await page.keyboard.press('Tab')
    expect((await focusDescriptor(page)).name).toBe('Clear filters')

    // --- Step 3: keep tabbing until focus is on a conflict row link -------
    const presses = await tabUntil(
      page,
      (d) => d.tag === 'A' && (d.href ?? '').startsWith('/conflicts/'),
    )
    expect(presses).toBeLessThan(10)
    focus = await focusDescriptor(page)
    const conflictHref = focus.href
    expect(conflictHref).toBeTruthy()

    // --- Step 4: Enter opens the detail, and focus MOVES to its heading ---
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`${conflictHref}$`))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Conflict')

    focus = await focusDescriptor(page)
    expect(focus.tag).toBe('H1')
    expect(focus.id).toBe('page-heading')

    // --- Step 5: tab to the Approve button and read the status first ------
    await page.getByRole('button', { name: 'Approve proposal' }).waitFor()
    const statusBefore = await page
      .locator('[data-status-value]')
      .first()
      .getAttribute('data-status-value')

    await tabUntil(page, (d) => d.text === 'Approve proposal')
    focus = await focusDescriptor(page)
    expect(focus.tag).toBe('BUTTON')
    expect(focus.text).toBe('Approve proposal')

    // --- Step 6: activate it from the keyboard ----------------------------
    await page.keyboard.press('Enter')

    // The decision landed...
    await expect(
      page.locator('[data-status-value="approved"]').first(),
    ).toBeVisible({ timeout: 10_000 })
    expect(statusBefore).not.toBe('approved')

    // ...and focus is STILL on the button the reviewer pressed. It went inert
    // via aria-disabled, which does not remove it from the tab order.
    focus = await focusDescriptor(page)
    expect(focus.tag).toBe('BUTTON')
    expect(focus.text).toBe('Approve proposal')
    await expect(
      page.getByRole('button', { name: 'Approve proposal' }),
    ).toHaveAttribute('aria-disabled', 'true')

    // --- Step 7: the change was announced, not just painted ---------------
    await expect(page.locator('[data-testid="live-region"]')).toContainText(
      /is now Approved/,
    )
  })

  test('the skip link jumps to main, and focus is never trapped', async ({
    page,
  }) => {
    await page.goto('/conflicts?page_size=5')
    await page.getByRole('table', { name: /conflicts/i }).waitFor()

    await page.keyboard.press('Tab')
    await expect(page.locator('a:focus')).toHaveAttribute('href', '#main-content')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/#main-content$/)

    // The skip link must actually MOVE focus, not just scroll: <main> carries
    // tabIndex={-1} for exactly this.
    expect((await focusDescriptor(page)).id).toBe('main-content')

    // From main, the next Tab lands inside main — the header is behind us.
    await page.keyboard.press('Tab')
    expect((await focusDescriptor(page)).name).toBe('Source')

    // Now sweep the whole document. A focus trap would cycle a handful of
    // nodes forever; a healthy page visits many distinct controls and
    // eventually wraps back round to the skip link.
    const visited: string[] = []
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press('Tab')
      const current = await focusDescriptor(page)
      visited.push(`${current.tag}|${current.id}|${current.href ?? ''}|${current.name}`)
    }
    const distinct = new Set(visited)
    expect(distinct.size).toBeGreaterThan(15)
    // Wrapping round proves the sweep left the region it started in.
    expect(
      visited.some((entry) => entry.includes('#main-content')),
    ).toBe(true)
    // And it genuinely reached the table rows, not just the chrome.
    expect(visited.some((entry) => entry.includes('|/conflicts/'))).toBe(true)
  })

  test('every interactive control has an accessible name', async ({ page }) => {
    await page.goto('/proposals?page_size=10')
    await page.getByRole('table', { name: /proposals/i }).waitFor()

    const unnamed = await page.evaluate(() => {
      const selector = 'button, a[href], select, input, [tabindex]:not([tabindex="-1"])'
      const problems: string[] = []
      for (const el of Array.from(document.querySelectorAll(selector))) {
        const element = el as HTMLElement
        const labelled =
          element.getAttribute('aria-label') ??
          (element.getAttribute('aria-labelledby')
            ? document.getElementById(
                element.getAttribute('aria-labelledby') as string,
              )?.textContent
            : null) ??
          (element instanceof HTMLSelectElement || element instanceof HTMLInputElement
            ? document.querySelector(`label[for="${element.id}"]`)?.textContent
            : null) ??
          element.textContent
        if (!labelled || labelled.trim() === '') {
          problems.push(`${element.tagName}#${element.id}.${element.className}`)
        }
      }
      return problems
    })

    expect(unnamed, `controls with no accessible name: ${unnamed.join(', ')}`).toEqual(
      [],
    )
  })
})
