/**
 * The reviewer must never see a filtered heading over unfiltered rows.
 *
 * CONTRACT_ASSUMPTIONS A8: the dashboard sends `source` and `type` to
 * /api/proposals, DESIGN pins "(+ filters)" without listing them, and the
 * proposals table has neither column — so serving them needs a JOIN to
 * conflicts that the service may never write. A service that ignores an
 * unknown param answers 200 with the whole population, and "Proposals · CRM"
 * over payments rows is a reviewer approving a fix they never asked to see.
 *
 * These tests drive the real routes and assert the warning is on the SCREEN,
 * announced, not merely present in some object.
 */
import { screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  gradeConflict,
  makeClient,
  nameConflict,
  paymentConflict,
  renderApp,
} from '../test/harness'
import type { KeystoneApi } from '../lib/contract'

const entries = [
  ...Array.from({ length: 6 }, (_, i) => gradeConflict(i)),
  ...Array.from({ length: 4 }, (_, i) => nameConflict(i)),
  ...Array.from({ length: 3 }, (_, i) => paymentConflict(i)),
]

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

/** A service that accepts a filter, answers 200, and ignores it. */
function clientIgnoring(
  which: 'conflict-type' | 'proposal-status',
): KeystoneApi {
  const base = makeClient(entries)
  if (which === 'conflict-type') {
    return {
      ...base,
      listConflicts: (query, signal) =>
        base.listConflicts({ ...query, type: undefined }, signal),
    }
  }
  return {
    ...base,
    listProposals: (query, signal) =>
      base.listProposals({ ...query, status: undefined }, signal),
  }
}

describe('a filter the service demonstrably ignored', () => {
  it('warns, in an alert, when /api/conflicts returns rows of another type', async () => {
    renderApp(clientIgnoring('conflict-type'), '/conflicts?type=C14')

    const alert = await screen.findByTestId('filter-warning')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent('/api/conflicts was asked for type=C14')
    expect(alert).toHaveTextContent(/do not match/i)
    expect(
      within(alert).getByTestId('filter-warning-type'),
    ).toHaveAttribute('data-warning-kind', 'ignored')
  })

  it('warns when /api/proposals returns a status it was not asked for', async () => {
    renderApp(clientIgnoring('proposal-status'), '/proposals?status=rejected')

    const alert = await screen.findByTestId('filter-warning')
    expect(alert).toHaveTextContent('/api/proposals was asked for status=rejected')
    expect(
      within(alert).getByTestId('filter-warning-status'),
    ).toHaveAttribute('data-warning-kind', 'ignored')
  })

  it('shows NO warning when the service honours the filter', async () => {
    renderApp(makeClient(entries), '/conflicts?type=C14')
    await screen.findByRole('table', { name: /conflicts/i })
    expect(screen.queryByTestId('filter-warning')).toBeNull()
  })
})

describe('a proposals filter the response cannot prove either way (A8)', () => {
  it('warns that source= is unverifiable, and names the missing JOIN', async () => {
    renderApp(makeClient(entries), '/proposals?source=crm')

    const alert = await screen.findByTestId('filter-warning')
    const item = within(alert).getByTestId('filter-warning-source')
    expect(item).toHaveAttribute('data-warning-kind', 'unverifiable')
    expect(item).toHaveTextContent(/no source column/i)
    expect(item).toHaveTextContent(/JOIN to conflicts/i)
    expect(item).toHaveTextContent(/A8/)
  })

  it('warns that type= is unverifiable on the proposals list', async () => {
    renderApp(makeClient(entries), '/proposals?type=C6')
    const alert = await screen.findByTestId('filter-warning')
    expect(
      within(alert).getByTestId('filter-warning-type'),
    ).toHaveAttribute('data-warning-kind', 'unverifiable')
  })

  it('does NOT warn about source/type on the conflicts list — there they are verifiable', async () => {
    renderApp(makeClient(entries), '/conflicts?source=crm&type=C6')
    await screen.findByRole('table', { name: /conflicts/i })
    expect(screen.queryByTestId('filter-warning')).toBeNull()
  })
})
