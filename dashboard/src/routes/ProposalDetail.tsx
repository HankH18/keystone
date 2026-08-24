/**
 * /proposals/:id — everything a reviewer needs to decide, on one screen:
 * the proposed fix and its target path, the confidence figure, the evidence
 * packet, the fingerprint, the status, and the approve / reject action.
 */
import {
  Confidence,
  ErrorState,
  EvidencePacket,
  Fingerprint,
  Loading,
} from '../components/common'
import {
  actionShape,
  fieldClassification,
  reversibility,
  writePaths,
} from '../lib/proposal'
import { PageHeading } from '../components/PageHeading'
import { ProposalActions } from '../components/ProposalActions'
import { StatusBadge } from '../components/StatusBadge'
import { PROPOSAL_STATUS_META, statusMeta } from '../lib/statusMeta'
import { Link } from '../lib/router'
import { useProposal } from '../lib/queries'
import type { AutoApplyVerdict, Proposal, ProposalEvent } from '../lib/contract'

/**
 * Every path the fix would write, each with its §6 classification.
 *
 * A list, not a single path, because `action.set` may name more than one
 * assignment and a nested assignment lands on every member it carries — and
 * showing only the first would be the same class of quiet wrongness A10 was.
 */
function ProposedFix({ action }: { action: Record<string, unknown> }) {
  const shape = actionShape(action)
  if (shape === 'unreadable') {
    return (
      <span className="evidence-empty" data-testid="action-unreadable">
        Unreadable — this action is not in the committed{' '}
        <span className="ref">{'{"set": {…}}'}</span> shape (migration 0007), so
        the dashboard will not guess at the field it writes. See the action
        record below.
      </span>
    )
  }
  const paths = writePaths(action)
  if (paths.length === 0) {
    return (
      <span data-testid="fix-evidence-only">
        Evidence-only — this conflict type has no committed fix template that
        writes a field, and its action is{' '}
        <span className="ref">{'{"set": {}}'}</span>.
      </span>
    )
  }
  return (
    <>
      <span>
        {paths.length === 1
          ? 'Writes one field:'
          : `Writes ${paths.length} fields:`}
      </span>
      <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.1rem' }}>
        {paths.map((path) => (
          <li key={path.display}>
            <span className="ref">{path.display}</span> (
            {fieldClassification(path.leaf)})
          </li>
        ))}
      </ul>
    </>
  )
}

/**
 * R24's gate, as the service evaluated it — every condition and what decided it.
 *
 * `GET /api/proposals/{id}` computes this (`review.py::get_proposal` →
 * `apply.evaluate_auto_apply`) and the dashboard used to throw it away, so the
 * one screen a reviewer asks "why was this not applied automatically?" on had
 * no answer. Rendered only when the service sends it, and read defensively:
 * this is an ADDITION to the assumed shape, not a promise.
 *
 * Pass/fail is carried by the WORDS "met" / "not met" and by the row's
 * `data-check-passed`, never by colour: R12's bar is that a colourblind
 * reviewer loses nothing.
 */
function AutoApplyGate({ verdict }: { verdict: AutoApplyVerdict }) {
  const checks = Array.isArray(verdict.checks) ? verdict.checks : []
  return (
    <section className="panel" aria-labelledby="auto-apply-heading">
      <h2 id="auto-apply-heading">Auto-apply gate (R24)</h2>
      <p data-testid="auto-apply-verdict">
        <strong>
          {verdict.allowed
            ? 'Eligible for auto-apply.'
            : 'NOT eligible for auto-apply.'}
        </strong>{' '}
        <span className="ref">{verdict.reason}</span> — {verdict.detail}
      </p>
      {checks.length > 0 ? (
        <div className="table-scroll">
          <table
            className="data-table"
            data-testid="auto-apply-checks"
            aria-label="Auto-apply gate conditions"
          >
            <caption>
              Every condition R24 names, with the value that decided it.
            </caption>
            <thead>
              <tr>
                <th scope="col">Condition</th>
                <th scope="col">Held?</th>
                <th scope="col">What decided it</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((check) => (
                <tr key={check.check} data-check-passed={String(check.passed)}>
                  <th scope="row" className="ref">
                    {check.check}
                  </th>
                  <td>{check.passed ? 'met' : 'NOT met'}</td>
                  <td>{check.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  )
}

/** `—` for anything the service did not send, so a missing cell is not a crash. */
function cell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.join(', ')
  return String(value)
}

/**
 * The `proposal_events` reversal ledger — the record that proves the canonical
 * write was AUTHORISED and can be undone.
 *
 * R24 asks for "a recorded rollback path" and the rubric's guarded-automation
 * line asks for "fully logged & reversible". Both of those are properties of
 * this table, and until now it was visible only in `psql`: no endpoint exposed
 * it and nothing in the dashboard asked for it, so the one artifact that proves
 * the write was legitimate was the one artifact a reviewer could not see.
 *
 * THREE ABSENCES, THREE DIFFERENT FACTS. "The service did not send a ledger",
 * "the service sent an empty ledger" and "the service sent something that is
 * not a ledger" are distinct, and showing all three as one empty table would
 * report a MISSING ledger as a proposal that has never been written to — the
 * same class of quiet wrongness as reading `{"set": {}}` off an action the
 * database refuses. Each gets its own state and its own testid.
 *
 * `before` / `after` are not rendered because the service does not send them:
 * both columns are whole canonical records — the personal data — so
 * `review.py::_event_row` serves two sha256 digests and the field paths the
 * write moved instead. This table shows exactly that. The digest is what lets an
 * operator compare the ledger against the apply response, the audit row and
 * `entities.current::text`; the paths are what a reviewer actually reads.
 */
function ReversalLedger({ proposal }: { proposal: Proposal }) {
  const events = proposal.events

  if (events === undefined) {
    return (
      <p className="evidence-empty" data-testid="reversal-ledger-absent">
        This service build does not send the reversal ledger with a proposal, so
        the dashboard has nothing to show — <strong>not</strong> an empty ledger.
        The rows exist in <span className="ref">proposal_events</span>; the
        proposal detail response has no{' '}
        <span className="ref">events</span> member to render them from.
      </p>
    )
  }

  if (!Array.isArray(events)) {
    return (
      <p className="notice" data-testid="reversal-ledger-unreadable">
        <strong>The reversal ledger is unreadable.</strong> The service sent an{' '}
        <span className="ref">events</span> member that is not a list of events,
        so the dashboard will not guess at it. Treat this proposal&rsquo;s write
        history as unverified.
      </p>
    )
  }

  if (events.length === 0) {
    return (
      <p data-testid="reversal-ledger-empty">
        No canonical write has been authorised for this proposal yet. The ledger
        is empty, which is what an un-applied proposal looks like: nothing has
        reached <span className="ref">entities</span>, so there is nothing to
        reverse.
      </p>
    )
  }

  return (
    <div className="table-scroll">
      <table
        className="data-table"
        data-testid="reversal-ledger"
        aria-label="Reversal ledger for this proposal"
      >
        <caption>
          Every <span className="ref">proposal_events</span> row for this
          proposal, oldest first. The transaction id is what the ledger is FOR:
          the event, the canonical UPDATE and the status move share one
          transaction, which is what makes &ldquo;the write was
          authorised&rdquo; checkable from outside the database. The stored
          before/after documents are whole canonical records, so the service
          serves digests and the paths the write moved rather than the values.
        </caption>
        <thead>
          <tr>
            <th scope="col">Event</th>
            <th scope="col">Actor</th>
            <th scope="col">When</th>
            <th scope="col">Transaction</th>
            <th scope="col">Entity</th>
            <th scope="col">Fields moved</th>
            <th scope="col">Reversible?</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event: ProposalEvent, index: number) => (
            <tr
              key={`${cell(event.event_id)}-${index}`}
              data-event={cell(event.event)}
            >
              <th scope="row" className="ref">
                {cell(event.event)}
              </th>
              <td className="ref">{cell(event.actor)}</td>
              <td>{cell(event.ts)}</td>
              <td className="num">{cell(event.txid)}</td>
              <td className="ref truncate-id">{cell(event.canonical_id)}</td>
              <td className="ref">{cell(event.differing_paths)}</td>
              <td>
                {reversibility(event)}
                {event.before_digest ? (
                  <>
                    <br />
                    <span className="ref truncate-id">
                      {event.before_digest}
                    </span>
                  </>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ProposalDetailRoute({ id }: { id: string }) {
  const proposal = useProposal(id)

  return (
    <>
      <PageHeading>Proposal</PageHeading>
      <p className="page-intro ref">{id}</p>

      {proposal.isPending ? <Loading what="this proposal" /> : null}
      {proposal.isError ? (
        <ErrorState
          error={proposal.error}
          context="this proposal"
          onRetry={() => void proposal.refetch()}
        />
      ) : null}

      {proposal.data ? (
        <>
          <section className="panel" aria-labelledby="summary-heading">
            <h2 id="summary-heading">Summary</h2>
            <dl className="kv">
              <dt>Status</dt>
              <dd>
                <StatusBadge status={proposal.data.status} kind="proposal" />
                <div>
                  {
                    statusMeta(proposal.data.status, PROPOSAL_STATUS_META)
                      .description
                  }
                </div>
              </dd>

              <dt>Confidence</dt>
              <dd>
                <Confidence value={proposal.data.confidence} />
              </dd>

              <dt>Proposed fix</dt>
              <dd data-testid="proposed-fix">
                <ProposedFix action={proposal.data.action} />
              </dd>

              <dt>Sensitive field</dt>
              <dd>
                {proposal.data.sensitive
                  ? 'Yes — auto-apply is forbidden (invariant-contract §6, R15/R24).'
                  : 'No'}
              </dd>

              <dt>Fingerprint</dt>
              <dd>
                <Fingerprint value={proposal.data.fingerprint} />
              </dd>

              <dt>Conflict</dt>
              <dd>
                <Link
                  to={`/conflicts/${encodeURIComponent(proposal.data.conflict_id)}`}
                >
                  View the conflict this proposal answers
                </Link>
              </dd>

              <dt>Created on run</dt>
              <dd className="ref">{proposal.data.created_run}</dd>

              <dt>Decided</dt>
              <dd>
                {proposal.data.decided_by
                  ? `${proposal.data.decided_by} at ${proposal.data.decided_at ?? 'unknown time'}`
                  : 'Not yet decided'}
              </dd>
            </dl>
          </section>

          <ProposalActions proposal={proposal.data} />

          {proposal.data.auto_apply ? (
            <AutoApplyGate verdict={proposal.data.auto_apply} />
          ) : null}

          <section className="panel" aria-labelledby="ledger-heading">
            <h2 id="ledger-heading">Reversal ledger (R24)</h2>
            <ReversalLedger proposal={proposal.data} />
          </section>

          <section className="panel" aria-labelledby="rationale-heading">
            <h2 id="rationale-heading">Rationale</h2>
            {proposal.data.rationale ? (
              <p>{proposal.data.rationale}</p>
            ) : (
              <p className="evidence-empty">
                No rationale attached. The rationale is LLM-written text only and
                is skipped silently on failure or when the spend cap is hit — the
                proposal still lands (R17, DESIGN §Reconciler).
              </p>
            )}
          </section>

          <section className="panel" aria-labelledby="evidence-heading">
            <h2 id="evidence-heading">Evidence packet</h2>
            <EvidencePacket evidence={proposal.data.evidence} />
          </section>

          <section className="panel" aria-labelledby="action-json-heading">
            <h2 id="action-json-heading">Action record</h2>
            <EvidencePacket evidence={proposal.data.action} testId="action-record" />
          </section>
        </>
      ) : null}
    </>
  )
}
