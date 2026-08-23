/**
 * The declared contract assumptions, and the guarantee that a SILENT one is
 * never merely written down.
 *
 * The dashboard is built against an API that does not exist yet, so it assumes
 * things. Assumptions are fine; unlisted assumptions are not, and an assumption
 * whose failure mode is "wrong data, no error" is not fine at all unless
 * something turns it loud. This file binds both rules so a future edit cannot
 * quietly add a tenth assumption or downgrade A8 back into prose.
 */
import { describe, expect, it } from 'vitest'
import { CONTRACT_ASSUMPTIONS, type ProposalQuery } from './contract'
import { checkProposalFilters, checkConflictFilters } from './filterGuard'

const ids = CONTRACT_ASSUMPTIONS.map((a) => a.id)

describe('CONTRACT_ASSUMPTIONS', () => {
  it('enumerates A1 through A10, in order, with no gaps or duplicates', () => {
    expect(ids).toEqual([
      'A1',
      'A2',
      'A3',
      'A4',
      'A5',
      'A6',
      'A7',
      'A8',
      'A9',
      'A10',
    ])
    expect(new Set(ids).size).toBe(ids.length)
  })

  it.each(CONTRACT_ASSUMPTIONS)(
    '$id says what it assumes, what DESIGN pins, and what breaks',
    (assumption) => {
      expect(assumption.subject.length).toBeGreaterThan(5)
      expect(assumption.assumption.length).toBeGreaterThan(20)
      expect(assumption.pinned.length).toBeGreaterThan(20)
      expect(assumption.consequence.length).toBeGreaterThan(20)
      expect(['loud', 'silent']).toContain(assumption.failure)
    },
  )

  it('names a guard for every assumption whose failure mode is SILENT', () => {
    const silent = CONTRACT_ASSUMPTIONS.filter((a) => a.failure === 'silent')
    expect(silent.map((a) => a.id)).toEqual(['A3', 'A8'])
    for (const assumption of silent) {
      // "It would be wrong and you would not know" is not an acceptable entry:
      // each one has to point at the thing that makes it visible.
      expect(assumption.consequence).toMatch(/filterGuard|warn/i)
    }
  })

  it('records A8 — the proposals source/type filters with no backing column', () => {
    const a8 = CONTRACT_ASSUMPTIONS.find((a) => a.id === 'A8')
    expect(a8?.subject).toContain('/api/proposals')
    expect(a8?.assumption).toMatch(/source/)
    expect(a8?.assumption).toMatch(/type/)
    expect(a8?.pinned).toMatch(/no source and no type column|NO source and NO type column/i)
    expect(a8?.pinned).toMatch(/JOIN/i)
    expect(a8?.failure).toBe('silent')
  })

  it('records A9 and A10 — the two jsonb interiors the UI reads', () => {
    const a9 = CONTRACT_ASSUMPTIONS.find((a) => a.id === 'A9')
    const a10 = CONTRACT_ASSUMPTIONS.find((a) => a.id === 'A10')
    expect(a9?.subject).toBe('proposals.evidence.observed_values')
    expect(a10?.subject).toBe('proposals.action.target_path')
    for (const assumption of [a9, a10]) {
      expect(assumption?.pinned).toMatch(/not its interior/i)
    }
  })
})

describe('no filter the dashboard sends is silently unchecked', () => {
  /**
   * The list of proposal filters, as data. If a filter is added to
   * `ProposalQuery` and to httpClient without being handled in filterGuard, the
   * loop below leaves it with neither a verification nor a warning and this
   * test fails.
   */
  const PROPOSAL_FILTERS: {
    param: keyof ProposalQuery
    query: ProposalQuery
    /** A row that CONTRADICTS the filter, where the row shape allows one. */
    violating: Record<string, unknown> | null
  }[] = [
    {
      param: 'status',
      query: { status: 'pending' },
      violating: { status: 'applied', conflict_id: 'c-1' },
    },
    {
      param: 'conflict_id',
      query: { conflict_id: 'c-1' },
      violating: { status: 'pending', conflict_id: 'other' },
    },
    // No proposal field can contradict these two — that IS A8.
    { param: 'source', query: { source: 'crm' }, violating: null },
    { param: 'type', query: { type: 'C6' }, violating: null },
  ]

  it.each(PROPOSAL_FILTERS)(
    '$param is either verified against the rows or warned about',
    ({ param, query, violating }) => {
      const rows = [
        {
          id: 'p-1',
          conflict_id: 'c-1',
          fingerprint: 'f',
          action: {},
          confidence: 0.5,
          evidence: {},
          rationale: null,
          status: 'pending',
          sensitive: false,
          created_run: 'run-0003',
          decided_by: null,
          decided_at: null,
          ...(violating ?? {}),
        },
      ] as unknown as Parameters<typeof checkProposalFilters>[1]

      const warnings = checkProposalFilters(query, rows)
      const mine = warnings.filter((w) => w.param === param)
      expect(mine).toHaveLength(1)
      expect(mine[0].kind).toBe(violating ? 'ignored' : 'unverifiable')
      expect(mine[0].detail).toContain('/api/proposals')
    },
  )

  it('stays quiet when a proposals page honours the filters it CAN prove', () => {
    const rows = [
      {
        id: 'p-1',
        conflict_id: 'c-1',
        status: 'pending',
      },
    ] as unknown as Parameters<typeof checkProposalFilters>[1]
    expect(
      checkProposalFilters({ status: 'pending', conflict_id: 'c-1' }, rows),
    ).toEqual([])
  })

  it('verifies all three conflicts filters against the rows, and stays quiet when they hold', () => {
    const rows = [
      { type: 'C6', sources: ['crm'], status: 'open' },
    ] as unknown as Parameters<typeof checkConflictFilters>[1]
    expect(
      checkConflictFilters({ type: 'C6', source: 'crm', status: 'open' }, rows),
    ).toEqual([])
    expect(
      checkConflictFilters({ type: 'C1', source: 'payments', status: 'x' }, rows).map(
        (w) => w.param,
      ),
    ).toEqual(['type', 'source', 'status'])
  })
})
