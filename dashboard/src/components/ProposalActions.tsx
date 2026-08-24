/**
 * Approve / reject, the TWO apply paths R24 distinguishes, and the reversal.
 *
 * ===========================================================================
 * WHY THERE ARE TWO APPLY BUTTONS AND NOT ONE
 * ===========================================================================
 * `POST /api/proposals/{id}/apply` is two code paths in the service
 * (`recon/api/review.py::apply_endpoint`), separated by `?auto=true`:
 *
 *   AUTO   — R24's gate runs first: not sensitive, an approved case type, the
 *            target on §6's allowlist, the write set equal to the committed fix
 *            target, confidence ≥ 0.95, complete evidence, a recorded rollback
 *            path, and an appliable status. Any one of those failing is a 409
 *            naming the condition. The MACHINE authorised the write.
 *   MANUAL — `recon.apply.apply_proposal`. A reviewer approved it and is
 *            authorising the write themselves. The gate does not run. The
 *            PERSON authorised the write.
 *
 * `httpClient.applyProposal` used to send no `auto` parameter from anywhere, so
 * this screen's one Apply button was always the manual path and R24's guarded
 * auto-apply could not be reached from the UI at all.
 *
 * Both are offered, each labelled with its own authority and each wired to its
 * own description through `aria-describedby`, because the two are not
 * interchangeable and a reviewer has to know which one they are about to do.
 * The alternative — default to auto and fall back to manual when the gate
 * refuses — was rejected outright: it would turn R24's refusal into a
 * successful write, so the demo would be showing the exact opposite of the
 * safety property it claims, and the reviewer would never see the refusal.
 *
 * ===========================================================================
 * WHAT IS STRUCTURAL HERE, NOT STYLISTIC
 * ===========================================================================
 * A `sensitive_hold` proposal gets NO apply affordance of either kind — not a
 * disabled one, an absent one, because a disabled control still tells a
 * reviewer "this is a thing you could do". invariant-contract §6 / R15 / R24:
 * classification is a pure function of the target path and beats confidence.
 * Evidence-only proposals (`{"set": {}}`) write no field, so they get none
 * either. Neither path is offered before `approved`: `apply_writer` may only
 * move `approved` → `applied` (SQLSTATE KS004), so both would be refused.
 *
 * WHAT "WRITES A FIELD" IS READ FROM. `action.set` — the only interior
 * `ck_proposals_action_vocabulary` (migration 0007) permits — via
 * `writePaths()`. This used to read `action.target_path`, a key the database
 * REFUSES, so `writesAField` was `false` for every proposal the service could
 * ever write and the Apply control never rendered at all.
 */
import { useState } from 'react'
import { Button } from './Button'
import { useAnnounce } from '../lib/announcer'
import {
  actionShape,
  describeError,
  failedChecks,
  refusedGate,
  refusedRollback,
  rollbackReceipt,
  writePaths,
} from '../lib/proposal'
import { PROPOSAL_STATUS_META } from '../lib/statusMeta'
import {
  DECISION_RESULT_STATUS,
  useDecision,
  type DecisionKind,
} from '../lib/queries'
import type {
  AutoApplyVerdict,
  Proposal,
  RollbackReceipt,
} from '../lib/contract'

const DECIDED: ReadonlySet<string> = new Set([
  'approved',
  'rejected',
  'applied',
  'rolled_back',
])

/** What the reviewer just asked for, in words, for the live region and alerts. */
const ACTION_LABEL: Record<DecisionKind, string> = {
  approve: 'approve',
  reject: 'reject',
  apply: 'apply',
  'auto-apply': 'auto-apply',
  rollback: 'roll back',
}

function statusLabel(status: string): string {
  return PROPOSAL_STATUS_META[status as Proposal['status']]?.label ?? status
}

/**
 * R24's refusal, rendered as the product working rather than as a failure.
 *
 * The server's own sentence is shown verbatim — "confidence 0.9000 < 0.95
 * (R24)" — because a generic "409 Conflict" tells the reviewer nothing about
 * WHY the machine would not take this write, and that reason is the whole point
 * of having a gate. Only the conditions that did NOT hold are listed here; the
 * full table of all of them is already on this page (`AutoApplyGate`).
 *
 * Pass/fail is carried by the words "NOT met" and by `data-check-passed`, never
 * by colour (R12).
 */
function AutoApplyRefusal({
  verdict,
  detail,
  status,
}: {
  verdict: AutoApplyVerdict
  detail: string
  status: Proposal['status']
}) {
  const failed = failedChecks(verdict)
  return (
    <div className="alert" role="alert" data-testid="auto-apply-refusal">
      <h3>Auto-apply refused — R24&rsquo;s gate held</h3>
      <p>
        <strong className="ref">{verdict.reason}</strong> — {detail}
      </p>
      {failed.length > 0 ? (
        <div className="table-scroll">
          <table
            className="data-table"
            data-testid="auto-apply-refusal-checks"
            aria-label="Auto-apply conditions that did not hold"
          >
            <caption>
              The condition{failed.length === 1 ? '' : 's'} that stopped the
              write. Every other condition R24 names is in the gate table on
              this page.
            </caption>
            <thead>
              <tr>
                <th scope="col">Condition</th>
                <th scope="col">Held?</th>
                <th scope="col">What decided it</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((check) => (
                <tr key={check.check} data-check-passed="false">
                  <th scope="row" className="ref">
                    {check.check}
                  </th>
                  <td>NOT met</td>
                  <td>{check.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p>
        Nothing was written. The proposal is still {statusLabel(status)}. A
        reviewer may still apply it manually — that is a different authority,
        and this dashboard will not take it on your behalf.
      </p>
    </div>
  )
}

/**
 * The reversal refused because it is not on top of the stack.
 *
 * `review.py::_stale_reversal`: the canonical row no longer holds what this
 * proposal's apply left, so a later apply is on top and reversing out of order
 * would silently discard an approved, applied, unreversed write. Refused here
 * AND by `KS012` at COMMIT — the UI is showing a decision the database also
 * enforces, not a UI-side opinion.
 *
 * The paths are named; the values are not. That is the service's choice
 * (`keystone_differing_paths`) and this panel keeps it.
 */
function RollbackRefusal({
  receipt,
  detail,
}: {
  receipt: RollbackReceipt
  detail: string
}) {
  // `text`, comma-joined by `string_agg` — rendered verbatim, never split. In
  // two degenerate cases the function returns a whole sentence instead of paths.
  const paths = receipt.differing_paths
  return (
    <div className="alert" role="alert" data-testid="rollback-refusal">
      <h3>Rollback refused — this reversal is not on top</h3>
      <p>{detail}</p>
      {paths ? (
        <p>
          The stored value and the canonical row differ at{' '}
          <span className="ref">{paths}</span>. Reverse the write on top first —
          the ledger is a stack.
        </p>
      ) : null}
      <p>
        Nothing was written. The digests are on the response so an operator can
        see <em>which</em> stored value is which without being handed either.
      </p>
    </div>
  )
}

/**
 * A reversal that landed, and the one claim worth putting on the screen:
 * `byte_identical`.
 *
 * `recon.apply.rollback_proposal` copies `proposal_events.before` back column to
 * column INSIDE the database — nothing is parsed, re-serialized or reassembled
 * from field values — so byte-identity is a property of the statement rather
 * than a claim about the merge being invertible. It is asserted before the
 * transaction ends and again by `KS012` at COMMIT, which is why a 200 can say it
 * outright.
 */
function RollbackReceiptNote({ receipt }: { receipt: RollbackReceipt }) {
  return (
    <p className="notice" data-testid="rollback-receipt">
      <strong>
        {receipt.byte_identical === true
          ? 'Reversed, byte-identical.'
          : receipt.byte_identical === false
            ? 'Reversed, but NOT byte-identical.'
            : 'Reversed.'}
      </strong>{' '}
      {receipt.byte_identical === true
        ? 'The canonical row now holds exactly the bytes the apply captured — ' +
          'checked by digest before the transaction ended and again by SQLSTATE ' +
          'KS012 at COMMIT.'
        : receipt.byte_identical === false
          ? 'The restored row does not match the digest the apply captured. ' +
            'Treat this entity as suspect and read the ledger below.'
          : 'The service did not state whether the restore was byte-identical.'}
      {receipt.restored_digest ? (
        <>
          {' '}
          Restored digest{' '}
          <span className="ref truncate-id">{receipt.restored_digest}</span>.
        </>
      ) : null}
    </p>
  )
}

export function ProposalActions({ proposal }: { proposal: Proposal }) {
  const decision = useDecision()
  const announce = useAnnounce()
  const [lastAction, setLastAction] = useState<DecisionKind | null>(null)

  const isSensitive = proposal.sensitive || proposal.status === 'sensitive_hold'
  // A10: the write set comes from `action.set` — the ONLY interior migration
  // 0007 permits. Reading a `target_path` sibling (which the database refuses)
  // made this `false` for every proposal ever written, so the apply control
  // below could never render and R24 was unreachable from the UI.
  const written = writePaths(proposal.action)
  const shape = actionShape(proposal.action)
  const writesAField = written.length > 0
  const heldPaths = written.map((path) => path.display).join(', ')
  const canApply =
    !isSensitive && writesAField && proposal.status === 'approved'
  // The reversal leg. `apply_writer` may only move `applied` → `rolled_back`
  // (SQLSTATE KS004), so this is the one status from which a rollback is a real
  // operation rather than a request the database would refuse.
  const canRollback = proposal.status === 'applied'
  const refusal = decision.isError ? refusedGate(decision.error) : null
  const staleReversal = decision.isError ? refusedRollback(decision.error) : null
  const receipt =
    decision.isSuccess && lastAction === 'rollback'
      ? rollbackReceipt(decision.data)
      : null

  const run = (kind: DecisionKind) => {
    setLastAction(kind)
    decision.mutate(
      { id: proposal.id, kind },
      {
        onSuccess: (updated) => {
          const done = rollbackReceipt(updated)
          announce(
            `Proposal ${proposal.id} is now ${statusLabel(
              // The rollback endpoint's response body is not pinned, so it may
              // carry no row. Announce the status the decision moves to rather
              // than saying nothing.
              updated?.status ?? DECISION_RESULT_STATUS[kind],
            )}.` +
              (kind === 'rollback' && done?.byte_identical === true
                ? ' The restore was byte-identical.'
                : ''),
          )
        },
        onError: (error) =>
          announce(
            `Could not ${ACTION_LABEL[kind]} proposal ${proposal.id}. ${describeError(error).title}.`,
          ),
      },
    )
  }

  return (
    <section className="panel" aria-labelledby="actions-heading">
      <h2 id="actions-heading">Reviewer action</h2>

      {isSensitive ? (
        <p className="notice" data-testid="sensitive-hold-notice">
          <strong>Held for human review.</strong> This proposal writes{' '}
          <span className="ref">{heldPaths || 'a sensitive field'}</span>, which
          invariant-contract §6 classifies as sensitive. It is held by design,
          not blocked by a failure, and it can never be auto-applied at any
          confidence. A person decides.
        </p>
      ) : null}

      {DECIDED.has(proposal.status) ? (
        <p data-testid="decided-notice">
          Decided
          {proposal.decided_by ? ` by ${proposal.decided_by}` : ''}
          {proposal.decided_at ? ` at ${proposal.decided_at}` : ''}. Current
          status: {statusLabel(proposal.status)}.
        </p>
      ) : null}

      <div className="actions">
        <Button
          className="button button-primary"
          onClick={() => run('approve')}
          inert={decision.isPending || proposal.status === 'approved'}
        >
          Approve proposal
        </Button>
        <Button
          className="button button-danger"
          onClick={() => run('reject')}
          inert={decision.isPending || proposal.status === 'rejected'}
        >
          Reject proposal
        </Button>
        {decision.isPending ? <span>Sending decision…</span> : null}
      </div>

      {canApply ? (
        <div data-testid="apply-choice">
          <h3 id="apply-choice-heading">
            Write this fix — two different authorities
          </h3>
          <div
            className="actions"
            role="group"
            aria-labelledby="apply-choice-heading"
          >
            <Button
              onClick={() => run('auto-apply')}
              inert={decision.isPending}
              aria-describedby="auto-apply-help"
            >
              Auto-apply (R24 gate)
            </Button>
            <Button
              onClick={() => run('apply')}
              inert={decision.isPending}
              aria-describedby="manual-apply-help"
            >
              Apply approved fix
            </Button>
          </div>
          <dl className="kv">
            <dt>Auto-apply (R24 gate)</dt>
            <dd id="auto-apply-help">
              <strong>The machine authorises the write.</strong> R24&rsquo;s gate
              runs first — never sensitive, an approved case type, the target on
              §6&rsquo;s allowlist, the write set equal to the committed fix
              target, confidence at or above 0.95, complete evidence, and a
              recorded rollback path. If one condition does not hold the write is
              refused and the failing condition is named on this screen.
            </dd>
            <dt>Apply approved fix</dt>
            <dd id="manual-apply-help">
              <strong>You authorise the write, as the reviewer.</strong> This
              does not run the gate: it applies the fix you already approved,
              signed with your key. Use it when the gate refuses for a reason you
              have read and judged acceptable.
            </dd>
          </dl>
        </div>
      ) : null}

      {canRollback ? (
        <div data-testid="rollback-control">
          <h3 id="rollback-heading">Reverse this write</h3>
          <p id="rollback-help">
            R24 requires a <em>recorded</em> rollback path, and this is it. The
            apply stored the canonical row&rsquo;s exact bytes in the reversal
            ledger below; rolling back copies them straight back inside the
            database and records a second <span className="ref">rolled_back</span>{' '}
            event. It does not erase the first event — the ledger keeps both legs.
          </p>
          <div className="actions">
            <Button
              onClick={() => run('rollback')}
              inert={decision.isPending}
              aria-describedby="rollback-help"
            >
              Roll back this fix
            </Button>
          </div>
        </div>
      ) : null}

      {!isSensitive && shape === 'evidence-only' ? (
        <p className="page-intro" data-testid="evidence-only-notice">
          This proposal writes no field — its action is{' '}
          <span className="ref">{'{"set": {}}'}</span>, the evidence-only
          escalation for human review, so there is nothing to apply.
        </p>
      ) : null}

      {shape === 'unreadable' ? (
        <p className="notice" data-testid="action-unreadable-notice">
          <strong>This proposal&rsquo;s action is unreadable.</strong> It is not
          in the committed <span className="ref">{'{"set": {…}}'}</span> shape
          that migration 0007 permits, so the dashboard will not guess what it
          writes — and offers no apply. Read the action record below and treat
          the row as suspect.
        </p>
      ) : null}

      {receipt ? <RollbackReceiptNote receipt={receipt} /> : null}

      {refusal ? (
        <AutoApplyRefusal
          verdict={refusal}
          detail={describeError(decision.error).detail}
          status={proposal.status}
        />
      ) : staleReversal ? (
        <RollbackRefusal
          receipt={staleReversal}
          detail={describeError(decision.error).detail}
        />
      ) : decision.isError ? (
        <div className="alert" role="alert" data-testid="decision-error">
          <h3>
            Could not {lastAction ? ACTION_LABEL[lastAction] : 'record'} this
            proposal
          </h3>
          <p>
            <strong>{describeError(decision.error).title}</strong> —{' '}
            {describeError(decision.error).detail}
          </p>
          <p>
            Nothing was changed. The proposal is still{' '}
            {statusLabel(proposal.status)}.
          </p>
          <Button onClick={() => lastAction && run(lastAction)}>
            Try again
          </Button>
        </div>
      ) : null}
    </section>
  )
}
