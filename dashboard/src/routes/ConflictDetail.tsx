/**
 * /conflicts/:id — the row detail the brief asks for:
 * the disagreeing SOURCES and FIELDS as source-qualified paths, the evidence
 * packet, the confidence as a tabular figure, the fingerprint, and the status —
 * plus the proposal's approve / reject action inline, so a reviewer never has
 * to leave the conflict to act on it.
 */
import {
  Confidence,
  DisagreeingFields,
  ErrorState,
  EvidencePacket,
  Fingerprint,
  Loading,
  RefList,
  SourceList,
} from '../components/common'
import { PageHeading } from '../components/PageHeading'
import { ProposalActions } from '../components/ProposalActions'
import { StatusBadge } from '../components/StatusBadge'
import { CONFLICT_STATUS_META, statusMeta } from '../lib/statusMeta'
import { Link } from '../lib/router'
import { useConflict, useProposals } from '../lib/queries'
import {
  CONFLICT_TYPE_LABEL,
  RULE_ID_BY_TYPE,
  type ConflictType,
} from '../lib/contract'

export function ConflictDetailRoute({ id }: { id: string }) {
  const conflict = useConflict(id)
  const proposals = useProposals(
    { conflict_id: id, page_size: 10 },
    { enabled: Boolean(conflict.data) },
  )
  const proposal = proposals.data?.items[0]

  const observed =
    proposal && typeof proposal.evidence === 'object' && proposal.evidence
      ? ((proposal.evidence as Record<string, unknown>).observed_values as
          | Record<string, unknown>
          | undefined)
      : undefined

  return (
    <>
      <PageHeading>Conflict</PageHeading>
      <p className="page-intro ref">{id}</p>

      {conflict.isPending ? <Loading what="this conflict" /> : null}
      {conflict.isError ? (
        <ErrorState
          error={conflict.error}
          context="this conflict"
          onRetry={() => void conflict.refetch()}
        />
      ) : null}

      {conflict.data ? (
        <>
          <section className="panel" aria-labelledby="conflict-summary-heading">
            <h2 id="conflict-summary-heading">
              {conflict.data.type} —{' '}
              {CONFLICT_TYPE_LABEL[conflict.data.type as ConflictType] ??
                'Unknown type'}
            </h2>
            <dl className="kv">
              <dt>Status</dt>
              <dd>
                <StatusBadge status={conflict.data.status} kind="conflict" />
                <div>
                  {statusMeta(conflict.data.status, CONFLICT_STATUS_META).description}
                </div>
              </dd>

              <dt>Rule</dt>
              <dd className="ref">
                {RULE_ID_BY_TYPE[conflict.data.type as ConflictType] ?? '—'}
              </dd>

              <dt>Disagreeing sources</dt>
              <dd data-testid="disagreeing-sources">
                <SourceList sources={conflict.data.sources} />
              </dd>

              <dt>Records involved</dt>
              <dd>
                <RefList refs={conflict.data.entity_refs} />
              </dd>

              <dt>Fingerprint</dt>
              <dd>
                <Fingerprint value={conflict.data.fingerprint} />
              </dd>

              <dt>Seen</dt>
              <dd className="ref">
                first {conflict.data.first_seen_run}, last{' '}
                {conflict.data.last_seen_run}
              </dd>
            </dl>
          </section>

          <section className="panel" aria-labelledby="fields-heading">
            <h2 id="fields-heading">Disagreeing fields</h2>
            <DisagreeingFields
              paths={conflict.data.disagreeing_fields}
              observed={observed}
            />
          </section>

          {proposals.isPending ? <Loading what="the proposal" /> : null}
          {proposals.isError ? (
            <ErrorState
              error={proposals.error}
              context="the proposal for this conflict"
              onRetry={() => void proposals.refetch()}
            />
          ) : null}

          {proposal ? (
            <>
              <section className="panel" aria-labelledby="proposal-heading">
                <h2 id="proposal-heading">Proposal</h2>
                <dl className="kv">
                  <dt>Status</dt>
                  <dd>
                    <StatusBadge status={proposal.status} kind="proposal" />
                  </dd>
                  <dt>Confidence</dt>
                  <dd>
                    <Confidence value={proposal.confidence} />
                  </dd>
                  <dt>Full record</dt>
                  <dd>
                    <Link to={`/proposals/${encodeURIComponent(proposal.id)}`}>
                      Open the full proposal
                    </Link>
                  </dd>
                </dl>
              </section>

              <ProposalActions proposal={proposal} />

              <section className="panel" aria-labelledby="conflict-evidence-heading">
                <h2 id="conflict-evidence-heading">Evidence packet</h2>
                <EvidencePacket evidence={proposal.evidence} />
              </section>
            </>
          ) : proposals.data ? (
            <p className="notice">
              No proposal exists for this conflict yet.
            </p>
          ) : null}
        </>
      ) : null}
    </>
  )
}
