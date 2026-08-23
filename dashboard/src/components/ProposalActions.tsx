/**
 * Approve / reject (and, where the contract permits it, apply).
 *
 * The rule that is graded here: a `sensitive_hold` proposal NEVER gets an
 * auto-apply affordance. That is structural, not a disabled button — the Apply
 * control is not rendered at all when the proposal is sensitive, because a
 * disabled control still tells a reviewer "this is a thing you could do".
 * invariant-contract §6 / R15 / R24: classification is a pure function of the
 * target field path and wins over confidence.
 *
 * Evidence-only proposals (C1, C3, C5, C7, C8, C10, C11, C12, C13) write no
 * field at all, so they get no Apply control either.
 */
import { useState } from 'react'
import { Button } from './Button'
import { useAnnounce } from '../lib/announcer'
import { describeError, targetPath } from '../lib/proposal'
import { PROPOSAL_STATUS_META } from '../lib/statusMeta'
import { useDecision, type DecisionKind } from '../lib/queries'
import type { Proposal } from '../lib/contract'

const DECIDED: ReadonlySet<string> = new Set([
  'approved',
  'rejected',
  'applied',
  'rolled_back',
])

export function ProposalActions({ proposal }: { proposal: Proposal }) {
  const decision = useDecision()
  const announce = useAnnounce()
  const [lastAction, setLastAction] = useState<DecisionKind | null>(null)

  const isSensitive = proposal.sensitive || proposal.status === 'sensitive_hold'
  const writesAField = targetPath(proposal.action) !== null
  const canApply =
    !isSensitive && writesAField && proposal.status === 'approved'

  const run = (kind: DecisionKind) => {
    setLastAction(kind)
    decision.mutate(
      { id: proposal.id, kind },
      {
        onSuccess: (updated) =>
          announce(
            `Proposal ${proposal.id} is now ${PROPOSAL_STATUS_META[updated.status]?.label ?? updated.status}.`,
          ),
        onError: (error) =>
          announce(
            `Could not ${kind} proposal ${proposal.id}. ${describeError(error).title}.`,
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
          <span className="ref">{targetPath(proposal.action) ?? 'a sensitive field'}</span>,
          which invariant-contract §6 classifies as sensitive. It is held by
          design, not blocked by a failure, and it can never be auto-applied at
          any confidence. A person decides.
        </p>
      ) : null}

      {DECIDED.has(proposal.status) ? (
        <p data-testid="decided-notice">
          Decided
          {proposal.decided_by ? ` by ${proposal.decided_by}` : ''}
          {proposal.decided_at ? ` at ${proposal.decided_at}` : ''}. Current
          status: {PROPOSAL_STATUS_META[proposal.status]?.label ?? proposal.status}.
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
        {canApply ? (
          <Button onClick={() => run('apply')} inert={decision.isPending}>
            Apply approved fix
          </Button>
        ) : null}
        {decision.isPending ? <span>Sending decision…</span> : null}
      </div>

      {!isSensitive && !writesAField ? (
        <p className="page-intro" data-testid="evidence-only-notice">
          This proposal writes no field — it is an evidence-only escalation for
          human review, so there is nothing to apply.
        </p>
      ) : null}

      {decision.isError ? (
        <div className="alert" role="alert" data-testid="decision-error">
          <h3>Could not {lastAction ?? 'record'} this proposal</h3>
          <p>
            <strong>{describeError(decision.error).title}</strong> —{' '}
            {describeError(decision.error).detail}
          </p>
          <p>
            Nothing was changed. The proposal is still{' '}
            {PROPOSAL_STATUS_META[proposal.status]?.label ?? proposal.status}.
          </p>
          <Button onClick={() => lastAction && run(lastAction)}>
            Try again
          </Button>
        </div>
      ) : null}
    </section>
  )
}
