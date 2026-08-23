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
import { fieldClassification, targetPath } from '../lib/proposal'
import { PageHeading } from '../components/PageHeading'
import { ProposalActions } from '../components/ProposalActions'
import { StatusBadge } from '../components/StatusBadge'
import { PROPOSAL_STATUS_META, statusMeta } from '../lib/statusMeta'
import { Link } from '../lib/router'
import { useProposal } from '../lib/queries'

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
              <dd>
                {targetPath(proposal.data.action) ? (
                  <>
                    write <span className="ref">{targetPath(proposal.data.action)}</span>{' '}
                    ({fieldClassification(targetPath(proposal.data.action) as string)})
                  </>
                ) : (
                  'Evidence-only — this conflict type has no committed fix template that writes a field.'
                )}
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
