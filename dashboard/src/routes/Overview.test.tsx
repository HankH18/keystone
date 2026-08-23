/**
 * The overview's job is reconciliation (R11), so these tests check that it
 * actually compares two independently-fetched figures — and, crucially, that it
 * says "Mismatch" when they disagree. A reconciliation screen that can only
 * ever say "Match" is decoration.
 */
import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { gradeConflict, makeClient, renderApp } from '../test/harness'
import type { KeystoneApi, Scorecard } from '../lib/contract'

const entries = Array.from({ length: 6 }, (_, i) => gradeConflict(i))

function scorecardWith(byType: Record<string, number>): Scorecard {
  return {
    generated_at: '2026-08-22T09:00:00Z',
    run_id: 'run-0003',
    conflicts: { total: 6, by_type: byType },
    proposals: { total: 6, by_status: { pending: 4, approved: 2 } },
    checks: { sc_golden_key_unique: true },
  }
}

function clientWithScorecard(card: Scorecard): KeystoneApi {
  const base = makeClient(entries)
  return { ...base, getScorecard: () => Promise.resolve(card) }
}

function reconcileRow(type: string): HTMLElement {
  const table = screen.getByRole('table', { name: /conflicts by type/i })
  const cell = within(table).getByText(new RegExp(`^${type} —`))
  return cell.closest('tr') as HTMLElement
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('/ — reconciliation against /api/scorecard', () => {
  it('reports Match when the scorecard agrees with the conflicts endpoint', async () => {
    renderApp(clientWithScorecard(scorecardWith({ C6: 6 })), '/')

    await screen.findByRole('table', { name: /conflicts by type/i })
    await waitFor(() =>
      expect(within(reconcileRow('C6')).getByText('Match')).toBeInTheDocument(),
    )
    expect(within(reconcileRow('C6')).getAllByText('6')).toHaveLength(2)
  })

  it('reports Mismatch — in words, not only in colour — when they disagree', async () => {
    // The scorecard claims 99 C6 conflicts; the conflicts endpoint has 6.
    renderApp(clientWithScorecard(scorecardWith({ C6: 99 })), '/')

    await screen.findByRole('table', { name: /conflicts by type/i })
    await waitFor(() =>
      expect(
        within(reconcileRow('C6')).getByText('Mismatch'),
      ).toBeInTheDocument(),
    )
    const row = reconcileRow('C6')
    expect(within(row).getByText('99')).toBeInTheDocument()
    expect(within(row).getByText('6')).toBeInTheDocument()
    // Icon + word, so the verdict survives greyscale.
    expect(row.querySelector('svg[data-status-icon="cross"]')).not.toBeNull()
  })

  it('announces the reconciliation result in the live region', async () => {
    renderApp(clientWithScorecard(scorecardWith({ C6: 99 })), '/')
    await waitFor(() =>
      expect(screen.getByTestId('live-region')).toHaveTextContent(
        /1 conflict types do not match the scorecard/,
      ),
    )
  })

  it('counts each type with page_size 1, so the client never pulls the population', async () => {
    const base = makeClient(entries)
    const sizes: (number | undefined)[] = []
    const client: KeystoneApi = {
      ...base,
      getScorecard: () => Promise.resolve(scorecardWith({ C6: 6 })),
      listConflicts: (query, signal) => {
        sizes.push(query.page_size)
        return base.listConflicts(query, signal)
      },
    }

    renderApp(client, '/')
    await screen.findByRole('table', { name: /conflicts by type/i })
    await waitFor(() => expect(sizes.length).toBeGreaterThanOrEqual(14))
    expect(sizes.every((size) => size === 1)).toBe(true)
  })

  it('shows a labelled error when the scorecard itself cannot be loaded', async () => {
    const base = makeClient(entries)
    const client: KeystoneApi = {
      ...base,
      getScorecard: () => Promise.reject(new Error('scorecard unavailable')),
    }
    renderApp(client, '/')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load the scorecard')
    expect(alert).toHaveTextContent('scorecard unavailable')
  })
})

/**
 * "Proposals by status" used to print the raw enum keys the service happens to
 * use — `rolled_back`, `sensitive_hold` — on the one screen a reviewer opens
 * first, while every other surface in the app said "Rolled back" and "Held for
 * human review". `sensitive_hold` in particular is a SAFETY state: it means a
 * person must look at it, and reading it as a database value invites treating
 * it as a fault.
 */
describe('/ — the proposal status mix speaks the reviewer’s vocabulary', () => {
  function mixCard(byStatus: Record<string, number>): Scorecard {
    return {
      ...scorecardWith({ C6: 6 }),
      proposals: { total: 6, by_status: byStatus },
    }
  }

  it('renders the label and the icon for every status, never the raw key', async () => {
    renderApp(
      clientWithScorecard(
        mixCard({
          pending: 2,
          approved: 1,
          rejected: 1,
          applied: 1,
          rolled_back: 1,
          sensitive_hold: 3,
        }),
      ),
      '/',
    )

    const table = await screen.findByRole('table', { name: /proposals by status/i })
    for (const [raw, label] of [
      ['pending', 'Pending review'],
      ['approved', 'Approved'],
      ['rejected', 'Rejected'],
      ['applied', 'Applied'],
      ['rolled_back', 'Rolled back'],
      ['sensitive_hold', 'Held for human review'],
    ]) {
      expect(within(table).getByText(label)).toBeInTheDocument()
      expect(within(table).queryByText(raw)).toBeNull()
    }
    // Icon + label, the same two channels as every other status in the app.
    expect(
      table.querySelectorAll('.status-badge svg[data-status-icon]').length,
    ).toBe(6)
  })

  it('reads sensitive_hold as a deliberate hold, with its explanation', async () => {
    renderApp(clientWithScorecard(mixCard({ sensitive_hold: 3 })), '/')
    const table = await screen.findByRole('table', { name: /proposals by status/i })
    const row = within(table)
      .getByText('Held for human review')
      .closest('tr') as HTMLElement
    expect(within(row).getByText(/held for a person by design/i)).toBeInTheDocument()
    expect(within(row).getByText(/never auto-apply/i)).toBeInTheDocument()
    expect(row.querySelector('svg[data-status-icon="shield"]')).not.toBeNull()
  })

  it('keeps the RAW status in the link, so the filter still speaks to the service', async () => {
    renderApp(clientWithScorecard(mixCard({ sensitive_hold: 3 })), '/')
    const table = await screen.findByRole('table', { name: /proposals by status/i })
    const link = within(table).getByRole('link', { name: /held for human review/i })
    expect(link).toHaveAttribute('href', '/proposals?status=sensitive_hold')
  })

  it('orders the rows by the pinned status vocabulary, not by JSON key order', async () => {
    renderApp(
      clientWithScorecard(
        mixCard({ sensitive_hold: 1, applied: 1, pending: 1, approved: 1 }),
      ),
      '/',
    )
    const table = await screen.findByRole('table', { name: /proposals by status/i })
    const labels = within(table)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent)
    expect(labels).toEqual([
      'Pending review',
      'Approved',
      'Applied',
      'Held for human review',
    ])
  })

  it('shows a status this build does not know rather than dropping it', async () => {
    renderApp(clientWithScorecard(mixCard({ pending: 1, quarantined: 2 })), '/')
    const table = await screen.findByRole('table', { name: /proposals by status/i })
    expect(within(table).getByText('quarantined')).toBeInTheDocument()
    expect(
      table.querySelector('.status-badge[data-status="unknown"]'),
    ).not.toBeNull()
  })
})
