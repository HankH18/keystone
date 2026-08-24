/**
 * The routes, driven by bodies shaped like the REAL SERVICE — not like the mock.
 *
 * ===========================================================================
 * Why this file exists: every other suite here agrees with `src/mocks`.
 * ===========================================================================
 * The vitest suites and the Playwright a11y gate all run against
 * `src/mocks/mockClient.ts`, and the mock invented the DASHBOARD's shape rather
 * than the service's — `action.target_path` (which
 * `ck_proposals_action_vocabulary` forbids) and a top-level
 * `evidence.observed_values` (which `recon.reconciler.EvidencePacket.as_dict`
 * nests under `conflict`). So "207 green" was evidence that the dashboard
 * agreed with its own fiction.
 *
 * The fixtures below are copied from the service's own row builders, cited:
 *   - `action`      — `{"set": {...}}`, migration 0007 `ck_proposals_action_vocabulary`;
 *                     one top-level key, and `review.py::_proposal_row` returns
 *                     the whole dict.
 *   - `evidence`    — `reconciler.py::EvidencePacket.as_dict`, which nests
 *                     `observed_values` under `evidence.conflict`.
 *   - conflict row  — `review.py::_conflict_row`, whose `observed_values` is
 *                     TOP-LEVEL on the conflict itself.
 *   - proposal row  — `review.py::_proposal_row`, including the joined
 *                     `conflict_type` / `conflict_sources`.
 *   - `auto_apply`  — `review.py::get_proposal` →
 *                     `apply.evaluate_auto_apply(...).as_dict()`.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderApp } from '../test/harness'
import type {
  Conflict,
  ConflictQuery,
  KeystoneApi,
  Page,
  Proposal,
  ProposalQuery,
  Scorecard,
} from '../lib/contract'
import { ApiError } from '../lib/api'

const FINGERPRINT = 'a'.repeat(64)

function serviceConflict(overrides: Partial<Conflict> = {}): Conflict {
  return {
    id: '9001',
    fingerprint: FINGERPRINT,
    type: 'C6',
    entity_refs: ['appdb:student:student-1', 'crm:contact:CRM-000001'],
    sources: ['appdb', 'crm'],
    disagreeing_fields: ['appdb.student.grade', 'crm.contact.grade'],
    // review.py::_conflict_row — TOP-LEVEL on the conflict row.
    observed_values: {
      'appdb.student.grade': 'Grade 4',
      'crm.contact.grade': '5',
    },
    status: 'open',
    first_seen_run: 'run-0001',
    last_seen_run: 'run-0003',
    ...overrides,
  }
}

function serviceProposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: '4001',
    conflict_id: '9001',
    fingerprint: FINGERPRINT,
    // Exactly one top-level key. `target_path` here would be refused by the DB.
    action: { set: { 'crm.contact.grade': 'Grade 4' } },
    confidence: 0.91,
    evidence: {
      schema: 'keystone.evidence.v1',
      run_id: 'run-0003',
      generation: 3,
      conflict: {
        id: 9001,
        type: 'C6',
        rule_id: 'R-006',
        fingerprint: FINGERPRINT,
        entity_refs: ['appdb:student:student-1', 'crm:contact:CRM-000001'],
        sources_involved: ['appdb', 'crm'],
        disagreeing_fields: ['appdb.student.grade', 'crm.contact.grade'],
        observed_values: {
          'appdb.student.grade': 'Grade 4',
          'crm.contact.grade': '5',
        },
        person_key: 'person-1',
      },
      fix: {
        conflict_type: 'C6',
        target_path: 'crm.contact.grade',
        container: null,
        value: 'Grade 4',
        value_derivable: true,
        derivation: 'the app-DB side is authoritative for grade',
        action: { set: { 'crm.contact.grade': 'Grade 4' } },
      },
    },
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

const SCORECARD: Scorecard = {
  generated_at: '2026-08-22T09:00:00Z',
  run_id: 'run-0003',
  conflicts: { total: 1, by_type: { C6: 1 } },
  proposals: { total: 1, by_status: { pending: 1 } },
  checks: { sc_golden_key_unique: true },
}

interface Recorded {
  applied: string[]
  approved: string[]
}

/** A client that answers with the service's row shapes. No mock involved. */
function serviceClient(
  conflicts: Conflict[],
  proposals: Proposal[],
  recorded: Recorded = { applied: [], approved: [] },
): KeystoneApi {
  const page = <T,>(items: T[]): Page<T> => ({
    items,
    page: 1,
    page_size: 25,
    total: items.length,
  })
  const find = (id: string) => {
    const found = proposals.find((row) => row.id === id)
    if (!found) {
      throw new ApiError({
        type: 'about:blank',
        title: 'Not Found',
        status: 404,
        detail: `No proposal ${id}`,
      })
    }
    return found
  }
  const replace = (id: string, status: Proposal['status']): Proposal => {
    const index = proposals.findIndex((row) => row.id === id)
    const updated: Proposal = {
      ...find(id),
      status,
      decided_by: 'reviewer@keystone.example',
      decided_at: '2026-08-22T10:00:00Z',
    }
    proposals[index] = updated
    return updated
  }
  return {
    listConflicts: (query: ConflictQuery) =>
      Promise.resolve(
        page(
          conflicts.filter(
            (row) =>
              (!query.type || row.type === query.type) &&
              (!query.source || row.sources.includes(query.source)) &&
              (!query.status || row.status === query.status),
          ),
        ),
      ),
    getConflict: (id: string) => {
      const found = conflicts.find((row) => row.id === id)
      return found
        ? Promise.resolve(found)
        : Promise.reject(
            new ApiError({
              type: 'about:blank',
              title: 'Not Found',
              status: 404,
              detail: `No conflict ${id}`,
            }),
          )
    },
    listProposals: (query: ProposalQuery) =>
      Promise.resolve(
        page(
          proposals.filter(
            (row) =>
              (!query.conflict_id || row.conflict_id === query.conflict_id) &&
              (!query.status || row.status === query.status),
          ),
        ),
      ),
    getProposal: (id: string) => Promise.resolve(find(id)),
    approveProposal: (id: string) => {
      recorded.approved.push(id)
      return Promise.resolve(replace(id, 'approved'))
    },
    rejectProposal: (id: string) => Promise.resolve(replace(id, 'rejected')),
    applyProposal: (id: string) => {
      recorded.applied.push(id)
      return Promise.resolve(replace(id, 'applied'))
    },
    getScorecard: () => Promise.resolve(SCORECARD),
  }
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('A10 — the proposed fix, read off the action the database permits', () => {
  it('names the field the fix writes in the proposals list', async () => {
    renderApp(serviceClient([serviceConflict()], [serviceProposal()]), '/proposals')

    const table = await screen.findByRole('table', { name: /proposals/i })
    expect(within(table).getByTestId('fix-target')).toHaveTextContent(
      'write crm.contact.grade',
    )
    expect(within(table).queryByTestId('fix-evidence-only')).toBeNull()
  })

  it('names the field, and its §6 classification, on the proposal detail', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()]),
      '/proposals/4001',
    )

    const fix = await screen.findByTestId('proposed-fix')
    expect(fix).toHaveTextContent('crm.contact.grade')
    expect(fix).toHaveTextContent('auto-apply eligible')
  })

  it('RENDERS the apply control for an approved, non-sensitive field write (R24)', async () => {
    const user = userEvent.setup()
    const recorded: Recorded = { applied: [], approved: [] }
    const client = serviceClient(
      [serviceConflict()],
      [serviceProposal({ status: 'approved', decided_by: 'reviewer@keystone.example' })],
      recorded,
    )
    renderApp(client, '/proposals/4001')

    const apply = await screen.findByRole('button', { name: 'Apply approved fix' })
    await user.click(apply)
    expect(recorded.applied).toEqual(['4001'])
  })

  it('offers no apply control until the proposal is approved', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal({ status: 'pending' })]),
      '/proposals/4001',
    )
    await screen.findByRole('button', { name: 'Approve proposal' })
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
  })

  it('names the held path instead of the words "a sensitive field"', async () => {
    renderApp(
      serviceClient(
        [serviceConflict({ type: 'C14' })],
        [
          serviceProposal({
            action: { set: { 'crm.contact.first_name': 'joraui' } },
            sensitive: true,
            status: 'sensitive_hold',
            conflict_type: 'C14',
          }),
        ],
      ),
      '/proposals/4001',
    )

    const notice = await screen.findByTestId('sensitive-hold-notice')
    expect(notice).toHaveTextContent('crm.contact.first_name')
    expect(notice).not.toHaveTextContent('a sensitive field')
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
  })

  it('reports EVERY path a multi-assignment action writes, not just the first', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [
          serviceProposal({
            action: {
              set: {
                'crm.contact.grade': '5',
                'crm.contact.lifecycle_stage': 'customer',
              },
            },
          }),
        ],
      ),
      '/proposals/4001',
    )

    const fix = await screen.findByTestId('proposed-fix')
    expect(fix).toHaveTextContent('crm.contact.grade')
    expect(fix).toHaveTextContent('crm.contact.lifecycle_stage')
  })

  it('still calls {"set": {}} evidence-only, and offers no apply', async () => {
    renderApp(
      serviceClient(
        [serviceConflict({ type: 'C1', disagreeing_fields: [] })],
        [
          serviceProposal({
            action: { set: {} },
            status: 'approved',
            conflict_type: 'C1',
          }),
        ],
      ),
      '/proposals/4001',
    )

    expect(await screen.findByTestId('evidence-only-notice')).toHaveTextContent(
      /nothing to apply/i,
    )
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
  })

  it('says so LOUDLY when an action is not in the committed shape at all', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [
          serviceProposal({
            // The shape the dashboard used to assume. The DB refuses it, so if
            // one ever arrives the dashboard must not read it as "no field write".
            action: { target_path: 'crm.contact.grade' } as Record<string, unknown>,
            status: 'approved',
          }),
        ],
      ),
      '/proposals/4001',
    )

    expect(await screen.findByTestId('action-unreadable')).toHaveTextContent(
      /not in the committed .+ shape/i,
    )
    expect(screen.queryByTestId('evidence-only-notice')).toBeNull()
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
  })
})

describe("R24 — the service's auto-apply verdict, on the screen that asks for it", () => {
  it('names every condition, whether it held, and what decided it', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [
          serviceProposal({
            status: 'pending',
            auto_apply: {
              proposal_id: 4001,
              allowed: false,
              reason: 'not_approved',
              detail: 'a proposal is auto-appliable only from `approved`',
              checks: [
                {
                  check: 'writes_a_field',
                  passed: true,
                  detail: 'the action writes crm.contact.grade',
                },
                {
                  check: 'confidence_at_or_above_threshold',
                  passed: true,
                  detail: '0.91 >= 0.95 is false',
                },
                {
                  check: 'rollback_path_known',
                  passed: false,
                  detail: 'no entity row to capture a before-image from',
                },
              ],
            },
          }),
        ],
      ),
      '/proposals/4001',
    )

    expect(await screen.findByTestId('auto-apply-verdict')).toHaveTextContent(
      /NOT eligible for auto-apply/,
    )
    const table = screen.getByTestId('auto-apply-checks')
    expect(table).toHaveTextContent('rollback_path_known')
    expect(table).toHaveTextContent('no entity row to capture a before-image')
    // Pass/fail is carried by WORDS, so a colourblind reviewer loses nothing.
    expect(table).toHaveTextContent('NOT met')
    expect(table).toHaveTextContent('met')
    expect(
      within(table).getAllByRole('row').filter((row) =>
        row.getAttribute('data-check-passed') === 'false',
      ),
    ).toHaveLength(1)
  })

  it('renders no gate panel at all when the service does not send one', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()]),
      '/proposals/4001',
    )
    await screen.findByRole('button', { name: 'Approve proposal' })
    expect(screen.queryByTestId('auto-apply-verdict')).toBeNull()
  })
})

describe('A9 — observed values, read from where the service actually puts them', () => {
  it('shows "CRM says X, App DB says Y" from the conflict row', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()]),
      '/conflicts/9001',
    )

    const fields = await screen.findByTestId('disagreeing-fields')
    expect(fields).toHaveTextContent('5')
    expect(fields).toHaveTextContent('Grade 4')
    expect(fields).not.toHaveTextContent('— ≠ —')
  })

  it('falls back to evidence.conflict.observed_values when the row omits it', async () => {
    const bare = serviceConflict()
    delete (bare as unknown as Record<string, unknown>).observed_values
    renderApp(serviceClient([bare], [serviceProposal()]), '/conflicts/9001')

    // `waitFor`, not `findBy`: the fallback lives on the PROPOSAL, which is a
    // second request, so the table exists before the values do. The row's own
    // copy — the preferred path above — has no such gap, which is one more
    // reason to prefer it.
    await waitFor(() =>
      expect(screen.getByTestId('disagreeing-fields')).toHaveTextContent(
        'Grade 4',
      ),
    )
    expect(screen.getByTestId('disagreeing-fields')).not.toHaveTextContent(
      '— ≠ —',
    )
  })
})
