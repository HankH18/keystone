/**
 * ===========================================================================
 * R24's TWO write paths, and the reversal ledger — driven through the real UI.
 * ===========================================================================
 *
 * WHY THIS FILE EXISTS. `POST /api/proposals/{id}/apply` is TWO code paths in
 * the service (`recon/api/review.py::apply_endpoint`):
 *
 *   default        — a reviewer has approved and is authorising the write
 *                    themselves. `recon.apply.apply_proposal`. No gate.
 *   `?auto=true`   — **R24**. `recon.apply.auto_apply` runs the ten-condition
 *                    gate FIRST and refuses with 409 plus every condition it
 *                    evaluated. A sensitive proposal is refused at any
 *                    confidence, before its confidence is even read.
 *
 * `src/lib/httpClient.ts` sent NO `auto` parameter, from anywhere, so the UI
 * button was the manual path 100% of the time and R24's guarded auto-apply — a
 * graded rubric line and a required demo beat — was unreachable from the
 * dashboard. The two tests named `asks the service for …` below fail against
 * that code: the first cannot find the control at all, the second proves the
 * manual button used to send an unnamed mode.
 *
 * The rule these tests pin, which matters more than either button existing:
 * **the UI never silently substitutes one path for the other.** A refused
 * auto-apply is rendered as a refusal carrying the server's own reason. It does
 * not fall back to the manual write, because that would convert the safety
 * property into a successful write and the demo would be showing the opposite
 * of what it claims.
 *
 * Fixtures are the SERVICE's shapes, cited, never the mock's:
 *   - `action`     `{"set": {…}}` only — migration 0007 `ck_proposals_action_vocabulary`.
 *   - `auto_apply` `apply.AutoApplyDecision.as_dict` (proposal_id/allowed/reason/detail/checks).
 *   - the 409      `review.py::apply_endpoint`'s `problem_body(...)` plus the
 *                  `auto_apply` member it attaches to the problem document.
 *   - `events`     `review.py::_event_row` — `event_id` / `txid` as STRINGS
 *                  (`bigint` columns, and a JSON number is an IEEE double in a
 *                  browser), and DIGESTS rather than the before/after documents,
 *                  because both columns are whole canonical records.
 *   - `rollback`   `RollbackResult.as_dict` on the 200, and
 *                  `_stale_reversal`'s evidence on the `rollback-not-on-top` 409.
 *                  Same member name, both outcomes.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderApp } from '../test/harness'
import { ApiError } from '../lib/api'
import type {
  Conflict,
  ConflictQuery,
  KeystoneApi,
  Page,
  Proposal,
  ProposalQuery,
  Scorecard,
} from '../lib/contract'

/** The accessible names a reviewer reads before they click. */
const AUTO_LABEL = 'Auto-apply (R24 gate)'
const MANUAL_LABEL = 'Apply approved fix'
const ROLLBACK_LABEL = 'Roll back this fix'

const FINGERPRINT = 'c'.repeat(64)
const CANONICAL = '84990991-6cb1-56b9-9511-0fae07ec1fa4'

function serviceConflict(overrides: Partial<Conflict> = {}): Conflict {
  return {
    id: '2994',
    fingerprint: FINGERPRINT,
    type: 'C9',
    entity_refs: ['appdb:enrollment:e-1', 'crm:deal:CRM-D-1'],
    sources: ['appdb', 'crm'],
    disagreeing_fields: [],
    observed_values: { 'appdb.enrollment.crm_deal_id': 'CRM-D-STALE' },
    status: 'open',
    first_seen_run: 'run-0001',
    last_seen_run: 'run-0003',
    ...overrides,
  }
}

function serviceProposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: '4001',
    conflict_id: '2994',
    fingerprint: FINGERPRINT,
    // C9's committed fix target. On AUTO_APPLY_ELIGIBLE, on no sensitive list.
    action: { set: { 'appdb.enrollment.crm_deal_id': null } },
    confidence: 1.0,
    evidence: { schema: 'keystone.evidence.v1', rule_id: 'R-009' },
    rationale: null,
    status: 'approved',
    sensitive: false,
    created_run: 'run-0003',
    decided_by: 'reviewer:admin',
    decided_at: '2026-08-22T10:00:00Z',
    conflict_type: 'C9',
    conflict_sources: ['appdb', 'crm'],
    ...overrides,
  }
}

const SCORECARD: Scorecard = {
  generated_at: '2026-08-22T09:00:00Z',
  run_id: 'run-0003',
  conflicts: { total: 1, by_type: { C9: 1 } },
  proposals: { total: 1, by_status: { approved: 1 } },
  checks: { sc_golden_key_unique: true },
}

/** Exactly what the UI asked the service to do, in order. */
interface Recorded {
  apply: { id: string; mode: string | undefined }[]
  rollback: string[]
}

/**
 * The 409 `review.py::apply_endpoint` returns when R24's gate refuses — the
 * strongest version of the demo beat: nine of ten conditions hold and only the
 * confidence floor does not.
 */
function refusal(): ApiError {
  return new ApiError({
    type: 'https://keystone.example/problems/auto-apply-refused',
    title: 'auto-apply refused',
    status: 409,
    detail:
      "R24's gate refused proposal 4001: confidence 0.9000 < 0.95 (R24)",
    auto_apply: {
      proposal_id: 4001,
      allowed: false,
      reason: 'confidence_floor',
      detail: 'confidence 0.9000 < 0.95 (R24)',
      checks: [
        {
          check: 'not_sensitive',
          passed: true,
          detail: "target 'crm.contact.grade' is not classified sensitive",
        },
        {
          check: 'approved_case_type',
          passed: true,
          detail: 'C6 is an approved case type',
        },
        {
          check: 'confidence_floor',
          passed: false,
          detail: 'confidence 0.9000 < 0.95 (R24)',
        },
      ],
    },
  })
}

/**
 * The 409 `review.py::_stale_reversal` answers when the reversal is not on top:
 * a later apply overwrote what this proposal's apply left, so undoing this one
 * out of order would silently discard an approved, applied, unreversed write.
 */
function notOnTop(): ApiError {
  return new ApiError({
    type: 'https://keystone.example/problems/rollback-not-on-top',
    title: 'not_on_top',
    status: 409,
    detail:
      'proposal 4001 is applied, but the canonical row no longer holds the value its ' +
      "apply left: the two differ at ['appdb.enrollment.crm_deal_id']. A later apply " +
      'is on top. Reverse the write on top first; the ledger is a stack.',
    rollback: {
      proposal_id: 4001,
      on_top: false,
      applied_after_digest: 'a'.repeat(64),
      current_digest: 'd'.repeat(64),
      differing_paths: 'appdb.enrollment.crm_deal_id',
    },
  })
}

interface ClientOptions {
  /** Reject the `?auto=true` call with the service's 409 refusal document. */
  refuseAuto?: boolean
  /** Omit `rollbackProposal` entirely — a client with no reversal capability. */
  withoutRollback?: boolean
  /** Reject the rollback with the `rollback-not-on-top` 409. */
  refuseRollback?: boolean
}

function serviceClient(
  conflicts: Conflict[],
  proposals: Proposal[],
  recorded: Recorded,
  options: ClientOptions = {},
): KeystoneApi {
  const page = <T,>(items: T[]): Page<T> => ({
    items,
    page: 1,
    page_size: 25,
    total: items.length,
  })
  const find = (id: string): Proposal => {
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
    const updated: Proposal = { ...find(id), status }
    proposals[index] = updated
    return updated
  }
  const client: KeystoneApi = {
    listConflicts: (query: ConflictQuery) =>
      Promise.resolve(
        page(conflicts.filter((row) => !query.type || row.type === query.type)),
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
            (row) => !query.status || row.status === query.status,
          ),
        ),
      ),
    getProposal: (id: string) => Promise.resolve(find(id)),
    approveProposal: (id: string) => Promise.resolve(replace(id, 'approved')),
    rejectProposal: (id: string) => Promise.resolve(replace(id, 'rejected')),
    applyProposal: (id: string, mode?: string) => {
      recorded.apply.push({ id, mode })
      if (mode === 'auto' && options.refuseAuto) return Promise.reject(refusal())
      return Promise.resolve(replace(id, 'applied'))
    },
    rollbackProposal: (id: string) => {
      recorded.rollback.push(id)
      if (options.refuseRollback) return Promise.reject(notOnTop())
      // The real 200: the updated proposal, plus `events` (now both legs) and
      // `rollback` — `RollbackResult.as_dict`, whose `byte_identical` is asserted
      // before the transaction ends and again by KS012 at COMMIT.
      const rolled = replace(id, 'rolled_back')
      return Promise.resolve({
        ...rolled,
        rollback: {
          proposal_id: 4001,
          canonical_id: CANONICAL,
          event_id: 92,
          applied_before_digest: BEFORE_DIGEST,
          restored_digest: BEFORE_DIGEST,
          byte_identical: true,
        },
      } as Proposal)
    },
    getScorecard: () => Promise.resolve(SCORECARD),
  }
  if (options.withoutRollback) delete client.rollbackProposal
  return client
}

function recorder(): Recorded {
  return { apply: [], rollback: [] }
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

// ---------------------------------------------------------------------------
// TASK 1 — the two paths, named, and never substituted for each other
// ---------------------------------------------------------------------------

describe('R24 — the dashboard can reach the GUARDED auto-apply path', () => {
  it('offers both writes as separate, labelled controls on an approved field write', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorder()),
      '/proposals/4001',
    )

    const auto = await screen.findByRole('button', { name: AUTO_LABEL })
    const manual = screen.getByRole('button', { name: MANUAL_LABEL })
    expect(auto).toBeEnabled()
    expect(manual).toBeEnabled()

    // A reviewer must be able to tell WHICH ONE THEY ARE DOING before clicking:
    // each control names its own authority, and the description is wired to the
    // button so a screen-reader user hears it on focus rather than having to go
    // hunting for the paragraph above.
    const autoDescription = auto.getAttribute('aria-describedby')
    const manualDescription = manual.getAttribute('aria-describedby')
    expect(autoDescription).toBeTruthy()
    expect(manualDescription).toBeTruthy()
    expect(autoDescription).not.toBe(manualDescription)
    expect(document.getElementById(autoDescription!)).toHaveTextContent(
      /gate/i,
    )
    expect(document.getElementById(manualDescription!)).toHaveTextContent(
      /does not run|without the gate|your authority/i,
    )
  })

  it('asks the service for the GATED path when auto-apply is pressed', async () => {
    const user = userEvent.setup()
    const recorded = recorder()
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorded),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: AUTO_LABEL }))

    // THE regression this file exists for: `httpClient.applyProposal` sent no
    // `auto` parameter from anywhere, so this recorded `mode: undefined` and
    // the service ran the UNGATED reviewer write instead of R24's gate.
    await waitFor(() =>
      expect(recorded.apply).toEqual([{ id: '4001', mode: 'auto' }]),
    )
  })

  it('asks the service for the REVIEWER-AUTHORISED path when manual apply is pressed', async () => {
    const user = userEvent.setup()
    const recorded = recorder()
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorded),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: MANUAL_LABEL }))

    // Named, not merely defaulted. The manual path must be an explicit choice
    // in the request too, so a future reader of the client cannot mistake
    // "no parameter" for "unspecified".
    await waitFor(() =>
      expect(recorded.apply).toEqual([{ id: '4001', mode: 'manual' }]),
    )
  })

  it("renders the server's own refusal reason and the conditions that failed", async () => {
    const user = userEvent.setup()
    const recorded = recorder()
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorded, {
        refuseAuto: true,
      }),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: AUTO_LABEL }))

    const panel = await screen.findByTestId('auto-apply-refusal')
    expect(panel).toHaveAttribute('role', 'alert')
    // The refusal IS the product demonstrating its safety property, so the
    // server's sentence is shown verbatim rather than as "request failed".
    expect(panel).toHaveTextContent('confidence 0.9000 < 0.95 (R24)')
    expect(panel).toHaveTextContent('confidence_floor')
    expect(panel).toHaveTextContent(/nothing was written/i)
    // Only the conditions that did NOT hold; the full ten-row gate table is
    // already on this page and does not need repeating inside an alert.
    expect(within(panel).queryByText(/approved_case_type/)).toBeNull()
    // Not colour: the row says so in words and carries a machine-readable flag.
    expect(panel).toHaveTextContent(/NOT met/)
    expect(
      within(panel)
        .getAllByRole('row')
        .filter((row) => row.getAttribute('data-check-passed') === 'false'),
    ).toHaveLength(1)
  })

  it('does NOT fall back to the manual write when the gate refuses', async () => {
    const user = userEvent.setup()
    const recorded = recorder()
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorded, {
        refuseAuto: true,
      }),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: AUTO_LABEL }))
    await screen.findByTestId('auto-apply-refusal')

    // Exactly one request, and it was the gated one. A silent fallback would
    // turn R24's refusal into a successful write — the demo would then be
    // showing the opposite of the property it claims.
    expect(recorded.apply).toEqual([{ id: '4001', mode: 'auto' }])
    // The manual control is still there to be chosen DELIBERATELY.
    expect(screen.getByRole('button', { name: MANUAL_LABEL })).toBeEnabled()
  })

  it('offers neither write while the proposal is only pending', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'pending', decided_by: null, decided_at: null })],
        recorder(),
      ),
      '/proposals/4001',
    )
    await screen.findByRole('button', { name: 'Approve proposal' })
    // `apply_writer` may only move `approved` -> `applied` (SQLSTATE KS004), so
    // BOTH paths are structurally unavailable — absent, not disabled.
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
  })

  it('offers neither write for a sensitive hold, at any confidence', async () => {
    renderApp(
      serviceClient(
        [serviceConflict({ type: 'C14' })],
        [
          serviceProposal({
            action: { set: { 'crm.contact.first_name': 'joraui' } },
            confidence: 1.0,
            sensitive: true,
            status: 'sensitive_hold',
            conflict_type: 'C14',
          }),
        ],
        recorder(),
      ),
      '/proposals/4001',
    )

    await screen.findByTestId('sensitive-hold-notice')
    expect(screen.queryByRole('button', { name: AUTO_LABEL })).toBeNull()
    expect(screen.queryByRole('button', { name: MANUAL_LABEL })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// TASK 2 — the reversal ledger and the recorded rollback path
// ---------------------------------------------------------------------------

const BEFORE_DIGEST = 'b'.repeat(64)

/** One `applied` row, exactly as `review.py::_event_row` serves it. */
const APPLIED_EVENTS = [
  {
    event_id: '91',
    event: 'applied',
    actor: 'system:auto-apply',
    ts: '2026-08-22T11:00:00Z',
    txid: '55123',
    canonical_id: CANONICAL,
    before_digest: BEFORE_DIGEST,
    after_digest: 'a'.repeat(64),
    differing_paths: 'appdb.enrollment.crm_deal_id',
  },
]

describe('R24 — the reversal ledger, on the screen that authorises the write', () => {
  it('renders each proposal_events row, its actor and whether it is reversible', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied', events: APPLIED_EVENTS })],
        recorder(),
      ),
      '/proposals/4001',
    )

    const ledger = await screen.findByTestId('reversal-ledger')
    expect(ledger).toHaveTextContent('applied')
    expect(ledger).toHaveTextContent('system:auto-apply')
    expect(ledger).toHaveTextContent('2026-08-22T11:00:00Z')
    // `txid` is the property the ledger is FOR: the event, the canonical UPDATE
    // and the status move share one transaction.
    expect(ledger).toHaveTextContent('55123')
    expect(ledger).toHaveTextContent('appdb.enrollment.crm_deal_id')
    // The before-image is what makes the write reversible, and it is reported in
    // WORDS with the digest that backs it — never the canonical record itself,
    // which is the personal data the service deliberately does not send.
    expect(ledger).toHaveTextContent(/before-image captured/i)
    expect(ledger).toHaveTextContent(BEFORE_DIGEST)
    expect(screen.queryByTestId('reversal-ledger-absent')).toBeNull()
    expect(screen.queryByTestId('reversal-ledger-empty')).toBeNull()
  })

  it('offers the rollback control on an applied proposal and calls the endpoint', async () => {
    const user = userEvent.setup()
    const recorded = recorder()
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied', events: APPLIED_EVENTS })],
        recorded,
      ),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: ROLLBACK_LABEL }))
    await waitFor(() => expect(recorded.rollback).toEqual(['4001']))
    await waitFor(() =>
      expect(screen.getAllByText('Rolled back').length).toBeGreaterThan(0),
    )
  })

  it('offers no rollback control on a proposal that was never applied', async () => {
    renderApp(
      serviceClient([serviceConflict()], [serviceProposal()], recorder()),
      '/proposals/4001',
    )
    await screen.findByRole('button', { name: MANUAL_LABEL })
    // `apply_writer` may only move `applied` -> `rolled_back`; there is no
    // write to reverse before there is a write.
    expect(screen.queryByRole('button', { name: ROLLBACK_LABEL })).toBeNull()
  })

  it('degrades to a labelled empty state when the service sends no events key', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied' })],
        recorder(),
      ),
      '/proposals/4001',
    )

    // Absent is NOT the same fact as empty, and conflating them is how a
    // missing ledger reads as a proposal with no recorded write.
    const absent = await screen.findByTestId('reversal-ledger-absent')
    expect(absent).toHaveTextContent(/does not (yet )?(expose|send)/i)
    expect(screen.queryByTestId('reversal-ledger')).toBeNull()
    expect(screen.queryByTestId('reversal-ledger-empty')).toBeNull()
  })

  it('says "no write authorised yet" when the service sends an EMPTY ledger', async () => {
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ events: [] })],
        recorder(),
      ),
      '/proposals/4001',
    )

    const empty = await screen.findByTestId('reversal-ledger-empty')
    expect(empty).toHaveTextContent(/no canonical write/i)
    expect(screen.queryByTestId('reversal-ledger-absent')).toBeNull()
  })

  it("reports the service's byte-identity claim after a successful reversal", async () => {
    const user = userEvent.setup()
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied', events: APPLIED_EVENTS })],
        recorder(),
      ),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: ROLLBACK_LABEL }))

    const note = await screen.findByTestId('rollback-receipt')
    // `rollback_proposal` copies `proposal_events.before` back column to column
    // inside the database, so byte-identity is a property of the statement.
    expect(note).toHaveTextContent(/byte-identical/i)
    expect(note).toHaveTextContent(BEFORE_DIGEST)
    await waitFor(() =>
      expect(screen.getByTestId('live-region')).toHaveTextContent(
        /restore was byte-identical/i,
      ),
    )
  })

  it('renders the not-on-top refusal with the paths the service named', async () => {
    const user = userEvent.setup()
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied', events: APPLIED_EVENTS })],
        recorder(),
        { refuseRollback: true },
      ),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: ROLLBACK_LABEL }))

    const panel = await screen.findByTestId('rollback-refusal')
    expect(panel).toHaveAttribute('role', 'alert')
    expect(panel).toHaveTextContent(/ledger is a stack/i)
    expect(panel).toHaveTextContent('appdb.enrollment.crm_deal_id')
    expect(panel).toHaveTextContent(/nothing was written/i)
    // A refusal is not the generic failure panel, and it is not a receipt.
    expect(screen.queryByTestId('rollback-receipt')).toBeNull()
    expect(screen.queryByTestId('decision-error')).toBeNull()
  })

  it('says a write with no captured before-image is NOT reversible', async () => {
    // `_PROPOSAL_EVENTS` computes `before_digest` as sha256(pe.before::text), and
    // SQL propagates NULL — so a null digest is the service saying there is
    // nothing to restore from. That must not read as "reversible" or as "—".
    renderApp(
      serviceClient(
        [serviceConflict()],
        [
          serviceProposal({
            status: 'applied',
            events: [{ ...APPLIED_EVENTS[0], before_digest: null }],
          }),
        ],
        recorder(),
      ),
      '/proposals/4001',
    )

    const ledger = await screen.findByTestId('reversal-ledger')
    expect(ledger).toHaveTextContent(/NOT reversible/)
    expect(ledger).not.toHaveTextContent(/before-image captured/i)
  })

  it('reports a rollback the service cannot serve, without pretending it worked', async () => {
    const user = userEvent.setup()
    renderApp(
      serviceClient(
        [serviceConflict()],
        [serviceProposal({ status: 'applied', events: APPLIED_EVENTS })],
        recorder(),
        { withoutRollback: true },
      ),
      '/proposals/4001',
    )

    await user.click(await screen.findByRole('button', { name: ROLLBACK_LABEL }))

    const error = await screen.findByTestId('decision-error')
    expect(error).toHaveTextContent(/rollback/i)
    expect(error).toHaveTextContent(/nothing was changed/i)
    // The status did not move on a hope.
    expect(screen.getAllByText('Applied').length).toBeGreaterThan(0)
  })
})
