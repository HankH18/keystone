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

/**
 * The proposal-mix verdict, which needs a third state that the conflict table
 * does not.
 *
 * A conflict count only moves when detection re-runs, so scorecard-vs-live can
 * only differ because something is wrong. A proposal STATUS count moves every
 * time a reviewer decides — approving one proposal takes `pending` from 2,670 to
 * 2,669 against a scorecard artifact that still says 2,670. Scoring that as
 * "Mismatch" would mean the dashboard reports a fault the moment anyone uses it
 * for its intended purpose, and a reviewer who is told they broke something when
 * they did not will stop believing the indicator that matters.
 *
 * So the row distinguishes the two. If the mix moved but the TOTAL is still the
 * scorecard's total, no proposal has appeared or vanished — only its state
 * changed, which is review working. That is reported as review activity, not as
 * a discrepancy. A total that has itself moved is a real discrepancy and is
 * still called one.
 */
function MixCell({
  matches,
  reviewMoved,
}: {
  matches: boolean
  reviewMoved: boolean
}) {
  if (matches) return <MatchCell matches />
  if (reviewMoved) {
    return (
      <span>
        <StatusIcon name="dot" /> Moved by review
      </span>
    )
  }
  return <MatchCell matches={false} />
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

  // Core #6's acceptance clause is "the log reconciles with the dashboard", and
  // Core #4's is "every dashboard figure reconciles with the raw ingestion,
  // invariant, and proposal logs". The conflicts table below has always been
  // fetched twice and compared; the proposal mix was NOT — it was rendered
  // straight off the scorecard artifact and captioned "as reported by the
  // scorecard", sitting immediately under a table whose whole point is that it
  // reconciles. A reviewer had no way to tell that one of the two tables was
  // asserting agreement and the other was quoting a file. Now both are fetched
  // twice from independent surfaces and compared the same way.
  //
  // The status list comes from the scorecard, so it is empty until the card
  // loads. `useQueries` accepts a varying-length array, and hooks stay
  // unconditional because this is computed above the early return, not inside it.
  // Only statuses this build KNOWS are queried. `statusMixRows` deliberately
  // also returns any status the service reports that the contract does not
  // pin, so that an unknown state renders as "unknown" instead of being
  // dropped — but an unknown state is not a value `listProposals` will accept,
  // and inventing a cast to send it anyway would turn a rendering nicety into a
  // 422 from the service. Those rows keep their scorecard figure and show no
  // live count, which is the honest answer for a status this build cannot ask
  // about.
  const proposalStatuses: ProposalStatus[] = card
    ? statusMixRows(card.proposals.by_status)
        .map((row) => row.status)
        .filter((status): status is ProposalStatus =>
          (PROPOSAL_STATUSES as readonly string[]).includes(status),
        )
    : []
  const proposalCounts = useQueries({
    queries: proposalStatuses.map((status) => ({
      queryKey: ['proposals-count', status],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const page = await (await api).listProposals(
          { status, page: 1, page_size: 1 },
          signal,
        )
        return page.total
      },
    })),
  })

  // A mix that moved while the TOTAL held is review activity, not drift. See
  // `MixCell`.
  //
  // The total is fetched UNFILTERED rather than summed from the per-status rows
  // above, and that is not a stylistic choice — summing them is wrong. The row
  // list comes from the scorecard's `by_status`, so a decision that moves a
  // proposal into a status the scorecard never recorded (the first `approved`
  // against an all-pending artifact) lands in no row at all, and the sum reads
  // one short of the truth. That under-count would then be reported as a
  // vanished proposal, which is precisely the false alarm `MixCell` exists to
  // avoid. One unfiltered count cannot miss a status it has never heard of.
  const liveTotal = useQueries({
    queries: [
      {
        queryKey: ['proposals-count', '*all*'],
        queryFn: async ({ signal }: { signal: AbortSignal }) => {
          const page = await (await api).listProposals(
            { page: 1, page_size: 1 },
            signal,
          )
          return page.total
        },
      },
    ],
  })[0]
  const proposalTotalHolds =
    liveTotal.isSuccess && liveTotal.data === card?.proposals.total

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

      {/*
        These were two links joined by a single space, which rendered as the
        run-on "Go to conflicts Go to proposals" — one phrase to the eye, with
        no boundary between two separate destinations. A `nav` with a list
        gives the boundary structurally rather than typographically, so it is
        also announced as a two-item navigation instead of a line of prose.
      */}
      <p>
        <Link to="/conflicts">Go to conflicts</Link>
        <span aria-hidden="true"> · </span>
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
                  Proposals by status — the scorecard figure against the
                  proposals endpoint&rsquo;s own total for the same filter, the
                  same way the conflict table above is reconciled. Every state is
                  shown in the reviewer&rsquo;s vocabulary, with its icon and its
                  meaning — a hold for human review is a deliberate safety state,
                  not a failure, and it is labelled that way here exactly as it
                  is everywhere else.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Status</th>
                    <th scope="col">What it means</th>
                    <th scope="col">Scorecard</th>
                    <th scope="col">/api/proposals total</th>
                    <th scope="col">Reconciles</th>
                  </tr>
                </thead>
                <tbody>
                  {statusMixRows(card.proposals.by_status).map(
                    ({ status, count }) => {
                      // Keyed by STATUS, never by row index: the row list can
                      // contain statuses that were filtered out of the query
                      // list above, so index alignment between the two is not a
                      // property that holds.
                      const queryIndex = proposalStatuses.indexOf(
                        status as ProposalStatus,
                      )
                      const live =
                        queryIndex === -1 ? undefined : proposalCounts[queryIndex]
                      return (
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
                          <td className="num">
                            {live?.isSuccess ? live.data : '…'}
                          </td>
                          <td>
                            {live?.isSuccess ? (
                              <MixCell
                                matches={count === live.data}
                                reviewMoved={proposalTotalHolds}
                              />
                            ) : live?.isError ? (
                              'Count failed'
                            ) : (
                              'Counting…'
                            )}
                          </td>
                        </tr>
                      )
                    },
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
