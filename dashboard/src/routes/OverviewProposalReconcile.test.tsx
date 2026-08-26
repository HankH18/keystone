/**
 * The proposal mix is RECONCILED, not quoted.
 *
 * Found by driving the deployed dashboard. "Conflicts by type" fetches each
 * figure twice — once from `/api/scorecard`, once as the `total` of the matching
 * `/api/conflicts` query — and prints Match / Mismatch per row. Directly beneath
 * it, "Proposals by status" printed the scorecard's own numbers and captioned
 * them "as reported by the scorecard for this run". Two tables, same visual
 * weight, and only one of them was actually checking anything.
 *
 * That matters because it is an acceptance clause, not a nicety. Core #4:
 * "every dashboard figure reconciles with the raw ingestion, invariant, and
 * proposal logs for the selected window." Core #6: "the log reconciles with the
 * dashboard." A figure read straight off an artifact reconciles with nothing —
 * and on the deployed instance the scorecard artifact and the live database were
 * in fact describing different datasets, which is exactly the failure this
 * column exists to catch.
 *
 * These tests bind the comparison itself: the row must go Mismatch when the
 * live total disagrees with the scorecard, which a scorecard-only rendering can
 * never do.
 */
import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { gradeConflict, makeClient, renderApp } from '../test/harness'
import type { KeystoneApi, Scorecard } from '../lib/contract'

const entries = Array.from({ length: 6 }, (_, i) => gradeConflict(i))

function cardWith(byStatus: Record<string, number>): Scorecard {
  return {
    generated_at: '2026-08-22T09:00:00Z',
    run_id: 'run-0003',
    conflicts: { total: 6, by_type: { C6: 6 } },
    proposals: {
      total: Object.values(byStatus).reduce((a, b) => a + b, 0),
      by_status: byStatus,
    },
    checks: { sc_golden_key_unique: true },
  }
}

/** A client whose scorecard says `byStatus` and whose proposals endpoint says `live`. */
function clientWhere(
  byStatus: Record<string, number>,
  live: Record<string, number>,
): KeystoneApi {
  const base = makeClient(entries)
  return {
    ...base,
    getScorecard: () => Promise.resolve(cardWith(byStatus)),
    listProposals: (query) =>
      Promise.resolve({
        items: [],
        page: 1,
        page_size: 1,
        // No status on the query means the unfiltered total, which is what the
        // route asks for to decide "moved by review" vs "a proposal is missing".
        total:
          query.status === undefined
            ? Object.values(live).reduce((a, b) => a + b, 0)
            : (live[String(query.status)] ?? 0),
      }),
  } as KeystoneApi
}

function mixTable(): HTMLElement {
  return screen.getByRole('table', { name: /proposals by status/i })
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('/ — the proposal mix is reconciled against the proposals endpoint', () => {
  it('reports Match when the scorecard agrees with the live proposal totals', async () => {
    renderApp(
      clientWhere({ pending: 4, sensitive_hold: 2 }, { pending: 4, sensitive_hold: 2 }),
      '/',
    )

    await waitFor(() => {
      expect(within(mixTable()).getAllByText('Match').length).toBe(2)
    })
    expect(within(mixTable()).queryByText('Mismatch')).toBeNull()
  })

  it('reports Mismatch — in words, not only in colour — when a proposal is missing', async () => {
    // The scorecard claims 2,670 pending; the database holds 0, and the total
    // has moved with it (3,050 -> 380). This is the deployed failure, reduced:
    // a stale artifact beside a live store that genuinely lost rows.
    renderApp(
      clientWhere({ pending: 2670, sensitive_hold: 380 }, { pending: 0, sensitive_hold: 380 }),
      '/',
    )

    await waitFor(() => {
      expect(within(mixTable()).getByText('Mismatch')).toBeInTheDocument()
    })
    // The disagreeing row shows BOTH numbers, so a reviewer can see which side
    // is wrong rather than only that something is.
    expect(within(mixTable()).getByText('2670')).toBeInTheDocument()
    expect(within(mixTable()).getByText('0')).toBeInTheDocument()
  })

  it('does NOT cry Mismatch when a reviewer decision moved the mix but not the total', async () => {
    // One approval: pending 2,670 -> 2,669, approved 0 -> 1. Nothing appeared or
    // vanished — the total is still 3,050. Scoring this as a discrepancy would
    // mean the dashboard reports a fault the moment anybody reviews anything,
    // which is the one activity it exists to support.
    renderApp(
      clientWhere(
        { pending: 2670, sensitive_hold: 380 },
        { pending: 2669, sensitive_hold: 380, approved: 1 },
      ),
      '/',
    )

    await waitFor(() => {
      expect(within(mixTable()).getByText('Moved by review')).toBeInTheDocument()
    })
    expect(within(mixTable()).queryByText('Mismatch')).toBeNull()
  })

  it('asks the proposals endpoint for a count, never for the population', async () => {
    const seen: unknown[] = []
    const base = makeClient(entries)
    const client = {
      ...base,
      getScorecard: () => Promise.resolve(cardWith({ pending: 4 })),
      listProposals: (query: Record<string, unknown>) => {
        seen.push(query)
        return Promise.resolve({ items: [], page: 1, page_size: 1, total: 4 })
      },
    } as unknown as KeystoneApi

    renderApp(client, '/')

    await waitFor(() => expect(seen.length).toBeGreaterThan(1))
    // EVERY call is a count, never a page of rows: the client must not pull the
    // population to render a figure.
    for (const query of seen as Record<string, unknown>[]) {
      expect(query.page_size).toBe(1)
    }
    // Exactly one call is unfiltered — the total that decides "moved by review"
    // vs "a proposal is missing". The rest are per-status.
    const unfiltered = (seen as Record<string, unknown>[]).filter(
      (query) => query.status === undefined,
    )
    expect(unfiltered).toHaveLength(1)
    expect(seen.length).toBeGreaterThan(unfiltered.length)
  })
})
