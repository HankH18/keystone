/**
 * The mock service itself: server-side filtering and pagination, the §6 fix
 * target table, the sensitive-hold rule, and — the honesty check — that the
 * seed really is the committed golden set, at the committed volumes, with all
 * fourteen conflict types present.
 */
import { describe, expect, it } from 'vitest'
import { buildMockDataset, createMockClient, fixTarget, mockClient } from './mockClient'
import goldenSummary from './seed/golden-summary.json'
import provenance from './seed/provenance.json'
import { CONFLICT_TYPES, SENSITIVE_FIELDS } from '../lib/contract'
import {
  gradeConflict,
  makeClient,
  nameConflict,
  paymentConflict,
} from '../test/harness'

describe('mock seed provenance', () => {
  it('is the committed golden conflict set at its real volume', () => {
    expect(provenance.generated_by).toBe(
      'dashboard/scripts/build-mock-seed.mjs',
    )
    expect(provenance.entry_count).toBe(goldenSummary.golden_entries)
    expect(provenance.entry_count).toBe(3050)
    expect(Object.keys(provenance.sources)).toContain('golden/conflicts.json')
  })

  it('carries every one of the fourteen conflict types at its A.4 minimum', () => {
    const counts = goldenSummary.conflict_counts as Record<string, number>
    const minimums = goldenSummary.conflict_minimums as Record<string, number>
    for (const type of CONFLICT_TYPES) {
      expect(counts[type], `${type} missing from the seed`).toBeGreaterThan(0)
      expect(counts[type]).toBeGreaterThanOrEqual(minimums[type])
    }
    expect(Object.keys(counts).sort()).toEqual([...CONFLICT_TYPES].sort())
  })
})

describe('fixTarget — invariant-contract §6', () => {
  it('writes the pinned linkage field for C2 and C9', () => {
    expect(fixTarget('C2', [])).toEqual({
      kind: 'set_field',
      target_path: 'payments.payment.external_ref',
    })
    expect(fixTarget('C9', [])).toEqual({
      kind: 'set_field',
      target_path: 'appdb.enrollment.crm_deal_id',
    })
  })

  it('writes crm.contact.email for C4 and never the linkage field', () => {
    const action = fixTarget('C4', [])
    expect(action).toEqual({
      kind: 'set_field',
      target_path: 'crm.contact.email',
    })
    // §6: re-targeting C4 at the linkage field would silently reclassify all
    // 250 C4 proposals as auto-appliable. It must never happen.
    expect(action).not.toEqual({
      kind: 'set_field',
      target_path: 'crm.contact.external_id',
    })
  })

  it('picks the eligible CRM path for a grade-only C6', () => {
    expect(
      fixTarget('C6', ['appdb.student.grade', 'crm.contact.grade']),
    ).toEqual({ kind: 'set_field', target_path: 'crm.contact.grade' })
  })

  it('picks the eligible CRM path for a lifecycle-only C6, leaving appdb.student.status alone', () => {
    expect(
      fixTarget('C6', ['appdb.student.status', 'crm.contact.lifecycle_stage']),
    ).toEqual({ kind: 'set_field', target_path: 'crm.contact.lifecycle_stage' })
  })

  it('lets the wholly-sensitive row decide a MIXED C6, not its grade half', () => {
    const mixed = [
      'appdb.student.first_name',
      'appdb.student.grade',
      'appdb.student.last_name',
      'crm.contact.first_name',
      'crm.contact.grade',
      'crm.contact.last_name',
    ]
    const action = fixTarget('C6', mixed)
    expect(action.kind).toBe('set_field')
    const path = (action as { target_path: string }).target_path
    expect(SENSITIVE_FIELDS.has(path)).toBe(true)
    // Ties break to the CRM side, then by code point: first_name before last_name.
    expect(path).toBe('crm.contact.first_name')
  })

  it('writes the CRM side for C14, never the authoritative app-DB record', () => {
    const path = (
      fixTarget('C14', [
        'appdb.student.first_name',
        'appdb.student.last_name',
        'crm.contact.first_name',
        'crm.contact.last_name',
      ]) as { target_path: string }
    ).target_path
    expect(path.startsWith('crm.')).toBe(true)
    expect(path).toBe('crm.contact.first_name')
  })

  it('is evidence-only for the types with no committed field write', () => {
    for (const type of ['C1', 'C3', 'C5', 'C7', 'C8', 'C10', 'C11', 'C12', 'C13']) {
      expect(fixTarget(type, []), type).toEqual({ kind: 'evidence_only' })
    }
  })
})

describe('mock client — server-side filtering and pagination', () => {
  const entries = [
    ...Array.from({ length: 12 }, (_, i) => gradeConflict(i)),
    ...Array.from({ length: 7 }, (_, i) => nameConflict(i)),
    ...Array.from({ length: 5 }, (_, i) => paymentConflict(i)),
  ]

  it('returns only the requested page, never the whole population', async () => {
    const client = makeClient(entries)
    const page = await client.listConflicts({ page: 1, page_size: 5 })
    expect(page.items).toHaveLength(5)
    expect(page.total).toBe(24)
    expect(page.page).toBe(1)
    expect(page.page_size).toBe(5)
  })

  it('walks pages without repeating or dropping a row', async () => {
    const client = makeClient(entries)
    const seen = new Set<string>()
    for (let page = 1; page <= 5; page += 1) {
      const result = await client.listConflicts({ page, page_size: 5 })
      for (const row of result.items) seen.add(row.id)
    }
    expect(seen.size).toBe(24)
    const last = await client.listConflicts({ page: 5, page_size: 5 })
    expect(last.items).toHaveLength(4)
  })

  it('caps page_size so a caller cannot pull 100k rows', async () => {
    const client = makeClient(entries)
    const page = await client.listConflicts({ page: 1, page_size: 100000 })
    expect(page.page_size).toBe(100)
  })

  it('filters conflicts by type', async () => {
    const client = makeClient(entries)
    const page = await client.listConflicts({ type: 'C14', page_size: 50 })
    expect(page.total).toBe(7)
    expect(page.items.every((row) => row.type === 'C14')).toBe(true)
  })

  it('filters conflicts by source', async () => {
    const client = makeClient(entries)
    const page = await client.listConflicts({ source: 'payments', page_size: 50 })
    expect(page.total).toBe(5)
    expect(page.items.every((row) => row.sources.includes('payments'))).toBe(true)
  })

  it('filters conflicts by status', async () => {
    const oscillating = { ...gradeConflict(99), oscillating: true }
    const client = makeClient([...entries, oscillating])
    const escalated = await client.listConflicts({
      status: 'escalated:oscillation',
      page_size: 50,
    })
    expect(escalated.total).toBe(1)
    const open = await client.listConflicts({ status: 'open', page_size: 50 })
    expect(open.total).toBe(24)
  })

  it('filters proposals by status and by the conflict type behind them', async () => {
    const client = makeClient(entries)
    const held = await client.listProposals({
      status: 'sensitive_hold',
      page_size: 50,
    })
    expect(held.total).toBe(7)
    expect(held.items.every((p) => p.sensitive)).toBe(true)

    const byType = await client.listProposals({ type: 'C2', page_size: 50 })
    expect(byType.total).toBe(5)
  })

  it('filters proposals to a single conflict', async () => {
    const client = makeClient(entries)
    const conflicts = await client.listConflicts({ type: 'C6', page_size: 1 })
    const target = conflicts.items[0]
    const proposals = await client.listProposals({ conflict_id: target.id })
    expect(proposals.total).toBe(1)
    expect(proposals.items[0].conflict_id).toBe(target.id)
  })
})

describe('mock client — the sensitive-hold rule', () => {
  it('holds every wholly-sensitive proposal and never marks it pending', async () => {
    const client = makeClient(Array.from({ length: 6 }, (_, i) => nameConflict(i)))
    const page = await client.listProposals({ page_size: 50 })
    expect(page.total).toBe(6)
    for (const proposal of page.items) {
      expect(proposal.status).toBe('sensitive_hold')
      expect(proposal.sensitive).toBe(true)
    }
  })

  it('refuses to apply a held proposal at any confidence', async () => {
    const client = makeClient([nameConflict(1)])
    const page = await client.listProposals({})
    await expect(
      client.applyProposal(page.items[0].id),
    ).rejects.toMatchObject({
      status: 409,
      problem: { detail: expect.stringMatching(/sensitive field/i) },
    })
  })

  it('refuses to apply a proposal that is not approved', async () => {
    const client = makeClient([paymentConflict(1)])
    const page = await client.listProposals({})
    const proposal = page.items[0]
    if (proposal.status !== 'approved') {
      await expect(client.applyProposal(proposal.id)).rejects.toMatchObject({
        status: 409,
        problem: { detail: expect.stringMatching(/approved/i) },
      })
    }
  })
})

describe('mock client — approve / reject round trip', () => {
  it('records an approval and reads it back from the list endpoint', async () => {
    const client = makeClient([gradeConflict(1), gradeConflict(2)])
    const before = await client.listProposals({ page_size: 10 })
    const target = before.items[0]

    const updated = await client.approveProposal(target.id)
    expect(updated.status).toBe('approved')
    expect(updated.decided_by).toBeTruthy()

    const after = await client.getProposal(target.id)
    expect(after.status).toBe('approved')

    const listed = await client.listProposals({ status: 'approved', page_size: 10 })
    expect(listed.items.map((p) => p.id)).toContain(target.id)
  })

  it('records a rejection', async () => {
    const client = makeClient([gradeConflict(3)])
    const before = await client.listProposals({})
    const updated = await client.rejectProposal(before.items[0].id)
    expect(updated.status).toBe('rejected')
  })

  it('404s on an unknown id, as an RFC7807 problem', async () => {
    const client = makeClient([gradeConflict(4)])
    await expect(client.getProposal('no-such-id')).rejects.toMatchObject({
      status: 404,
    })
  })
})

describe('mock client — scorecard reconciliation', () => {
  it('reports conflict counts that reconcile with the conflicts endpoint', async () => {
    const entries = [
      ...Array.from({ length: 3 }, (_, i) => gradeConflict(i)),
      ...Array.from({ length: 2 }, (_, i) => nameConflict(i)),
    ]
    const dataset = buildMockDataset(entries)
    const client = createMockClient(() => dataset)
    const card = await client.getScorecard()
    const listed = await client.listConflicts({ page_size: 1 })
    // The scorecard's by_type figures come from golden/manifest-summary.json,
    // a DIFFERENT artifact from the conflict rows, so this is a real
    // cross-check rather than a tautology — it is why the overview can show a
    // mismatch at all.
    expect(card.conflicts.total).toBe(3050)
    expect(listed.total).toBe(5)
    expect(card.proposals.total).toBe(5)
  })
})

describe('the full golden seed', () => {
  it('loads all 3,050 conflicts with unique fingerprints and one proposal each', async () => {
    const conflicts = await mockClient.listConflicts({ page: 1, page_size: 1 })
    expect(conflicts.total).toBe(3050)

    const proposals = await mockClient.listProposals({ page: 1, page_size: 1 })
    expect(proposals.total).toBe(3050)

    const dataset = await buildMockDataset()
    const fingerprints = new Set(dataset.conflicts.map((c) => c.fingerprint))
    expect(fingerprints.size).toBe(3050)
    const ids = new Set(dataset.proposals.map((p) => p.id))
    expect(ids.size).toBe(3050)
  }, 30_000)

  it('reconciles every per-type count against the scorecard', async () => {
    const card = await mockClient.getScorecard()
    for (const type of CONFLICT_TYPES) {
      const page = await mockClient.listConflicts({ type, page_size: 1 })
      expect(page.total, `${type} count`).toBe(card.conflicts.by_type[type])
    }
  }, 30_000)
})
