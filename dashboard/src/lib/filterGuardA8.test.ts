/**
 * A8, after the service answered it.
 *
 * `filterGuard` used to raise an `unverifiable` alert for `source` and `type`
 * on /api/proposals UNCONDITIONALLY, because "a proposal row carries no
 * source and no type". That was true when it was written and it is not true
 * now: `service/recon/api/review.py::_proposal_row` adds
 * `conflict_type` and `conflict_sources` -- the joined conflict's `type` and
 * `sources` -- with the docstring "They exist so that A8 ... becomes verifiable
 * from the row by any client that wants to check it."
 *
 * So the honest rule has three arms, and all three are asserted here:
 *   1. the row proves the filter was applied  -> say NOTHING;
 *   2. the row CONTRADICTS the filter          -> `ignored`, the proven verdict;
 *   3. the row cannot speak to it at all       -> `unverifiable`, as before.
 *
 * Arm 3 is not a leftover: the mock client does not carry the joined members,
 * and neither would a service that serves the filter without the JOIN — which
 * is exactly the failure A8 names.
 */
import { describe, expect, it } from 'vitest'
import { checkProposalFilters } from './filterGuard'
import type { Proposal } from './contract'

function row(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: 'p-1',
    conflict_id: 'c-1',
    fingerprint: 'f'.repeat(64),
    action: { set: {} },
    confidence: 0.5,
    evidence: {},
    rationale: null,
    status: 'pending',
    sensitive: false,
    created_run: 'run-0003',
    decided_by: null,
    decided_at: null,
    conflict_type: 'C6',
    conflict_sources: ['appdb', 'crm'],
    ...overrides,
  }
}

/** A row as it arrives WITHOUT the JOIN — the shape A8 was written about. */
function unjoinedRow(overrides: Partial<Proposal> = {}): Proposal {
  const bare = row(overrides)
  delete (bare as unknown as Record<string, unknown>).conflict_type
  delete (bare as unknown as Record<string, unknown>).conflict_sources
  return bare
}

describe('A8 — verified from the joined members when the service sends them', () => {
  it('says nothing at all when every row proves the type filter was applied', () => {
    expect(checkProposalFilters({ type: 'C6' }, [row(), row({ id: 'p-2' })])).toEqual(
      [],
    )
  })

  it('says nothing at all when every row proves the source filter was applied', () => {
    expect(checkProposalFilters({ source: 'crm' }, [row()])).toEqual([])
    expect(
      checkProposalFilters({ source: 'crm', type: 'C6' }, [row()]),
    ).toEqual([])
  })

  it('proves an IGNORED type filter instead of merely doubting it', () => {
    const warnings = checkProposalFilters({ type: 'C6' }, [
      row(),
      row({ id: 'p-2', conflict_type: 'C14' }),
    ])
    expect(warnings).toHaveLength(1)
    expect(warnings[0]).toMatchObject({
      kind: 'ignored',
      param: 'type',
      value: 'C6',
      assumption: 'A8',
    })
    expect(warnings[0].detail).toMatch(/1 of the 2 rows/)
  })

  it('proves an IGNORED source filter from conflict_sources', () => {
    const warnings = checkProposalFilters({ source: 'payments' }, [
      row({ conflict_sources: ['appdb', 'crm'] }),
    ])
    expect(warnings).toHaveLength(1)
    expect(warnings[0]).toMatchObject({ kind: 'ignored', param: 'source' })
  })

  it('keeps warning `unverifiable` when the row cannot speak to the filter', () => {
    const warnings = checkProposalFilters({ source: 'crm', type: 'C6' }, [
      unjoinedRow(),
    ])
    expect(warnings.map((warning) => [warning.param, warning.kind])).toEqual([
      ['type', 'unverifiable'],
      ['source', 'unverifiable'],
    ])
  })

  it('warns `unverifiable` on an EMPTY page — nothing there proves anything', () => {
    const warnings = checkProposalFilters({ type: 'C6' }, [])
    expect(warnings).toHaveLength(1)
    expect(warnings[0].kind).toBe('unverifiable')
  })

  it('judges each filter on its own evidence, not on the other one', () => {
    // The type is verifiable and holds; the source member is missing entirely.
    const mixed = row()
    delete (mixed as unknown as Record<string, unknown>).conflict_sources
    const warnings = checkProposalFilters({ type: 'C6', source: 'crm' }, [mixed])
    expect(warnings.map((warning) => [warning.param, warning.kind])).toEqual([
      ['source', 'unverifiable'],
    ])
  })
})
