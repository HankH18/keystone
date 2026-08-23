/**
 * / — the overview.
 *
 * R11's last clause: "every figure reconciles with the raw ingestion/invariant/
 * proposal logs for the selected window". So this page does not decorate — it
 * RECONCILES. For each of the fourteen conflict types it puts the figure the
 * scorecard reports next to the `total` the conflicts endpoint reports for the
 * same filter, and says whether they match. A drift between the two shows up
 * here as a row that says "Mismatch", rather than being averaged into a chart.
 *
 * There is no chart on this page. A chart of fourteen counts would be less
 * precise than the fourteen counts, and would hide exactly the disagreement
 * this page exists to surface.
 */
import { useEffect } from 'react'
import { useQueries } from '@tanstack/react-query'
import { ErrorState, Loading } from '../components/common'
import { useAnnounce } from '../lib/announcer'
import { PageHeading } from '../components/PageHeading'
import { StatusBadge, StatusIcon } from '../components/StatusBadge'
import { Link } from '../lib/router'
import { useApi, useScorecard } from '../lib/queries'
import { PROPOSAL_STATUS_META, statusMeta } from '../lib/statusMeta'
import {
  CONFLICT_TYPES,
  CONFLICT_TYPE_LABEL,
  PROPOSAL_STATUSES,
  RULE_ID_BY_TYPE,
  type ConflictType,
  type ProposalStatus,
} from '../lib/contract'

/**
 * The status mix, in the PINNED order of DESIGN §Data models `proposals.status`
 * rather than whatever order the scorecard's JSON object happened to arrive in
 * — so the table reads the same on every run — followed by any status the
 * service reports that this build does not know about (which renders as an
 * explicitly "unknown" badge rather than being dropped on the floor).
 */
function statusMixRows(
  byStatus: Partial<Record<ProposalStatus, number>>,
): { status: string; count: number }[] {
  const known: { status: string; count: number }[] = PROPOSAL_STATUSES.filter(
    (status) => byStatus[status] !== undefined,
  ).map((status) => ({ status, count: byStatus[status] ?? 0 }))
  const extra = Object.entries(byStatus)
    .filter(
      ([status]) => !(PROPOSAL_STATUSES as readonly string[]).includes(status),
    )
    .map(([status, count]) => ({ status, count: count ?? 0 }))
  return [...known, ...extra]
}

/** Icon + text, never a bare colour, for the reconciliation verdict. */
function MatchCell({ matches }: { matches: boolean }) {
  return (
    <span className={matches ? 'match-ok' : 'match-bad'}>
      <StatusIcon name={matches ? 'check' : 'cross'} />{' '}
      {matches ? 'Match' : 'Mismatch'}
    </span>
  )
}

export function OverviewRoute() {
  const api = useApi()
  const scorecard = useScorecard()
  const announce = useAnnounce()

  // One cheap server-side count per type: page_size 1, so the client receives
  // 1 row and the `total` — never the population.
  const counts = useQueries({
    queries: CONFLICT_TYPES.map((type) => ({
      queryKey: ['conflicts-count', type],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const page = await (await api).listConflicts(
          { type, page: 1, page_size: 1 },
          signal,
        )
        return page.total
      },
    })),
  })

  const card = scorecard.data
  const allCountsLoaded = counts.every((query) => query.isSuccess)
  const mismatches =
    card && allCountsLoaded
      ? CONFLICT_TYPES.filter(
          (type, index) =>
            (card.conflicts.by_type[type] ?? 0) !== counts[index].data,
        )
      : []

  useEffect(() => {
    if (!card || !allCountsLoaded) return
    announce(
      mismatches.length === 0
        ? 'Reconciliation complete. Every conflict-type figure matches the scorecard.'
        : `Reconciliation complete. ${mismatches.length} conflict types do not match the scorecard.`,
    )
  }, [card, allCountsLoaded, mismatches.length, announce])

  return (
    <>
      <PageHeading>Overview</PageHeading>
      <p className="page-intro">
        Reconciliation for the latest run. Each figure below is fetched twice —
        once from <span className="ref">/api/scorecard</span> and once as the{' '}
        <span className="ref">total</span> of the matching{' '}
        <span className="ref">/api/conflicts</span> query — and compared. A row
        that does not match is a real discrepancy, not a rounding artefact.
      </p>

      <p>
        <Link to="/conflicts">Go to conflicts</Link>{' '}
        <Link to="/proposals">Go to proposals</Link>
      </p>

      {scorecard.isPending ? <Loading what="the scorecard" /> : null}
      {scorecard.isError ? (
        <ErrorState
          error={scorecard.error}
          context="the scorecard"
          onRetry={() => void scorecard.refetch()}
        />
      ) : null}

      {card ? (
        <>
          <section className="panel" aria-labelledby="run-heading">
            <h2 id="run-heading">Selected window</h2>
            <dl className="kv">
              <dt>Run</dt>
              <dd className="ref">{card.run_id}</dd>
              <dt>Scorecard generated</dt>
              <dd className="ref">{card.generated_at}</dd>
              <dt>Conflicts reported</dt>
              <dd className="num">{card.conflicts.total}</dd>
              <dt>Proposals reported</dt>
              <dd className="num">{card.proposals.total}</dd>
              <dt>Reconciliation</dt>
              <dd>
                {allCountsLoaded ? (
                  <MatchCell matches={mismatches.length === 0} />
                ) : (
                  'Counting…'
                )}
              </dd>
            </dl>
          </section>

          <section className="panel" aria-labelledby="reconcile-heading">
            <h2 id="reconcile-heading">Conflicts by type</h2>
            <div className="table-scroll" tabIndex={0} role="region" aria-label="Conflicts by type, reconciled">
              <table className="data-table">
                <caption>
                  Conflicts by type — the scorecard figure against the
                  conflicts endpoint&rsquo;s own total for the same filter.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Type</th>
                    <th scope="col">Rule</th>
                    <th scope="col">Scorecard</th>
                    <th scope="col">/api/conflicts total</th>
                    <th scope="col">Reconciles</th>
                  </tr>
                </thead>
                <tbody>
                  {CONFLICT_TYPES.map((type: ConflictType, index) => {
                    const expected = card.conflicts.by_type[type] ?? 0
                    const actual = counts[index].data
                    return (
                      <tr key={type}>
                        <th scope="row">
                          <Link to={`/conflicts?type=${type}`}>
                            {type} — {CONFLICT_TYPE_LABEL[type]}
                          </Link>
                        </th>
                        <td className="ref">{RULE_ID_BY_TYPE[type]}</td>
                        <td className="num">{expected}</td>
                        <td className="num">
                          {counts[index].isSuccess ? actual : '…'}
                        </td>
                        <td>
                          {counts[index].isSuccess ? (
                            <MatchCell matches={expected === actual} />
                          ) : counts[index].isError ? (
                            'Count failed'
                          ) : (
                            'Counting…'
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel" aria-labelledby="proposal-mix-heading">
            <h2 id="proposal-mix-heading">Proposals by status</h2>
            {/*
              The scroll region's label must differ from this section's heading:
              two landmarks with the same role AND the same accessible name is
              an axe `landmark-unique` violation, and a landmark list reading
              "Proposals by status / Proposals by status" tells a screen-reader
              user nothing about which is which.
            */}
            <div
              className="table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Proposals by status, scrollable table"
            >
              <table className="data-table">
                <caption>
                  Proposals by status, as reported by the scorecard for this run.
                  Every state is shown in the reviewer&rsquo;s vocabulary, with
                  its icon and its meaning — a hold for human review is a
                  deliberate safety state, not a failure, and it is labelled that
                  way here exactly as it is everywhere else.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Status</th>
                    <th scope="col">What it means</th>
                    <th scope="col">Proposals</th>
                  </tr>
                </thead>
                <tbody>
                  {statusMixRows(card.proposals.by_status).map(
                    ({ status, count }) => (
                      <tr key={status}>
                        {/*
                          The LABEL, never the raw enum key: a reviewer reads
                          "Held for human review", not `sensitive_hold`. The raw
                          value stays where it belongs — in the query string of
                          the link, which is the service's vocabulary.
                        */}
                        <th scope="row">
                          <Link to={`/proposals?status=${encodeURIComponent(status)}`}>
                            <StatusBadge status={status} kind="proposal" />
                          </Link>
                        </th>
                        <td>
                          {statusMeta(status, PROPOSAL_STATUS_META).description}
                        </td>
                        <td className="num">{count}</td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </>
  )
}
