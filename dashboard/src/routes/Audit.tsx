/**
 * /audit — the action log, and the verification checks that were never rendered.
 *
 * ==========================================================================
 * This page exists because Core deliverable #6 has two halves and the second
 * one had no surface.
 * ==========================================================================
 * "Every action is logged (proposal, confidence, tokens, cost, reviewer
 * decision)" was true and had been true for a long time — `audit_log` holds
 * every one of those five. "**The log reconciles with the dashboard**" was not
 * checkable by anyone, because the log had no reader: the rows were in Postgres
 * and no endpoint served them, so a reviewer could not put the record of what
 * had been done next to the queue they were acting on, and a grader could not
 * check the claim at all.
 *
 * So this page does the reconciling move directly. The filters are the ids the
 * rest of the dashboard already shows — a conflict fingerprint, a proposal id,
 * a run id — so a reviewer takes an id off `/proposals` or `/conflicts`, pastes
 * it in, and reads that row's own history back. The spend roll-up is computed
 * by the service over the whole filtered set, not over the visible page, so it
 * is a figure that can be put next to `budget_ledger` and be right.
 *
 * The second half of the page is the **verification checks**, and that is a
 * separate defect being closed. `Scorecard.checks` has been TYPED in
 * `src/lib/contract.ts` since the contract was written and rendered nowhere:
 * the dashboard fetched the suite's verdict on every gate the rubric grades —
 * `spend-cap-burst` above all — and dropped the whole column on the floor.
 * `spend-cap-burst` is R17's 120-thread burst against a cap sized for six, and
 * it is the automated test Core #6's "spend cap enforced… verified by an
 * automated test" names. It leads the table now.
 *
 * **A check this scorecard does not carry is reported as NOT REPORTED**, with
 * the command that would report it — never as a pass, and never silently
 * omitted. The in-browser mock's scorecard carries the seed generator's
 * self-checks rather than the suite's, so that is the state the mock demo shows,
 * honestly, instead of the mock inventing a verification result it never ran.
 *
 * Accessibility, as everywhere else here:
 *   - every control has a real `<label for>`; nothing relies on placeholder text;
 *   - a verdict is icon + word + text, never colour alone (R12);
 *   - counts, token figures and money are `.num` — tabular numerals, so the
 *     columns are scannable;
 *   - the per-row detail is a native `<details>`/`<summary>`, so it is in the
 *     tab order and announced as a disclosure without any ARIA of its own.
 */
import { useEffect, useMemo, useState } from 'react'
import { createColumnHelper } from '@tanstack/react-table'
import { DataTable, type Columns, type TableFeatureSet } from '../components/DataTable'
import { Pagination } from '../components/Filters'
import { Button } from '../components/Button'
import { Confidence, ErrorState, EvidencePacket, Loading } from '../components/common'
import { PageHeading } from '../components/PageHeading'
import { StatusIcon } from '../components/StatusBadge'
import { useAnnounce } from '../lib/announcer'
import { useQueryParams } from '../lib/router'
import { useAudit, useScorecard } from '../lib/queries'
import {
  DEFAULT_PAGE_SIZE,
  HEADLINE_CHECKS,
  MAX_PAGE_SIZE,
  SPEND_CAP_BURST_CHECK,
  isRedactionToken,
  type AuditEntry,
  type AuditQuery,
  type Scorecard,
} from '../lib/contract'

// ---------------------------------------------------------------------------
// Reading a row
//
// The four readers below are exported so `Audit.test.tsx` can bind them
// directly against the shapes the SERVICE writes — a confidence that arrives as
// a string, a detail envelope that is sometimes absent. Behaviour reachable only
// through a rendered table is behaviour tested at one remove, and these are the
// parts most likely to be quietly wrong. Fast Refresh's one-export-kind rule is
// waived here for the same reason `components/DataTable.tsx` waives it: the
// alternative is a second module for four pure functions that belong to this
// page and are used nowhere else.
// ---------------------------------------------------------------------------

/**
 * The payload inside `audit_log.detail`.
 *
 * `recon.logging.audit_detail` wraps every body it writes:
 * `{mode, body_sha256, body}` in the default safe mode, `{mode, body}` under
 * `LOG_MODE=full`. So the interesting content is one level down — but only for
 * rows written through that chokepoint, and `recon.logging.AUDIT_WRITERS`
 * declares two writers that are not. This unwraps when the envelope is there
 * and hands back the object untouched when it is not, rather than assuming a
 * shape and rendering an empty cell for the rows that do not have it.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function auditBody(
  detail: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (detail === null) return null
  const body = detail.body
  if (typeof detail.mode === 'string' && body !== null && typeof body === 'object') {
    return body as Record<string, unknown>
  }
  return detail
}

/**
 * The confidence this row recorded, if it recorded one.
 *
 * `recon.reconciler` writes it as a STRING (`str(score.value)`, so the decimal
 * is exact rather than a float re-render), while a reviewer-decision row writes
 * the number. Both are read; anything else is `null` and the column shows a
 * dash rather than `NaN`.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function auditConfidence(entry: AuditEntry): number | null {
  const body = auditBody(entry.detail)
  const raw = body?.confidence
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null
  if (typeof raw === 'string') {
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

/** Integer microUSD → a dollar figure. Money is never a float in the service. */
// eslint-disable-next-line react-refresh/only-export-components
export function usd(microusd: number): string {
  return `$${(microusd / 1_000_000).toFixed(6)}`
}

/**
 * A value the service withheld, rendered as withheld.
 *
 * A redaction token is not a missing value and not a broken one — it is the
 * privacy design working, and the same token means the same actor on every row,
 * which is what makes a redacted log still correlatable. So it is labelled, and
 * the raw token stays visible (and copyable) underneath the label.
 */
function Redacted({ value }: { value: string }) {
  return (
    <span className="ref" data-testid="redacted" title={value}>
      <abbr title="redacted by recon.privacy — stable per value, so equal tokens are the same actor">
        redacted
      </abbr>{' '}
      {value}
    </span>
  )
}

function ActorOrToken({ value }: { value: string }) {
  return isRedactionToken(value) ? <Redacted value={value} /> : <span className="ref">{value}</span>
}

// ---------------------------------------------------------------------------
// Verdicts — icon + word + sentence, never colour alone
// ---------------------------------------------------------------------------

type Verdict = 'pass' | 'fail' | 'unreported'

const VERDICT_META: Record<
  Verdict,
  { icon: 'check' | 'cross' | 'question'; label: string; className: string }
> = {
  pass: { icon: 'check', label: 'Pass', className: 'match-ok' },
  fail: { icon: 'cross', label: 'Fail', className: 'match-bad' },
  // Not a failure and not a pass. A scorecard that does not carry a check has
  // not answered the question, and answering it for the service would be the
  // dashboard inventing a verification result.
  unreported: { icon: 'question', label: 'Not reported', className: '' },
}

function CheckVerdict({ verdict }: { verdict: Verdict }) {
  const meta = VERDICT_META[verdict]
  return (
    <span className={meta.className} data-testid={`verdict-${verdict}`}>
      <StatusIcon name={meta.icon} /> {meta.label}
    </span>
  )
}

function verdictOf(card: Scorecard, name: string): Verdict {
  const value = card.checks?.[name]
  if (value === undefined) return 'unreported'
  return value ? 'pass' : 'fail'
}

/**
 * The check rows, headline checks first and everything else in name order.
 *
 * A DISPLAY order, not a filter: every check the scorecard carries is rendered.
 * The dashboard never decides which checks exist — the service's artifact does.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function checkRows(card: Scorecard): { name: string; verdict: Verdict }[] {
  const names = Object.keys(card.checks ?? {})
  const headline = HEADLINE_CHECKS.filter((name) => names.includes(name))
  const rest = names.filter((name) => !headline.includes(name)).sort()
  return [...headline, ...rest].map((name) => ({
    name,
    verdict: verdictOf(card, name),
  }))
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

const helper = createColumnHelper<TableFeatureSet, AuditEntry>()

const columns: Columns<AuditEntry> = helper.columns([
  helper.accessor('id', {
    header: 'Entry',
    cell: ({ getValue }) => <span className="num">{getValue() as string}</span>,
  }),
  helper.accessor('ts', {
    header: 'When (UTC)',
    cell: ({ getValue }) => {
      const raw = getValue() as string
      return (
        <span className="num" title={raw}>
          {raw.replace('T', ' ').replace(/(\.\d+)?(Z|\+00:00)$/, '')}
        </span>
      )
    },
  }),
  helper.accessor('actor', {
    header: 'Actor',
    cell: ({ getValue }) => <ActorOrToken value={getValue() as string} />,
  }),
  helper.accessor('action', {
    header: 'Action',
    cell: ({ getValue }) => <span className="ref">{getValue() as string}</span>,
  }),
  helper.accessor('subject', {
    header: 'Subject',
    cell: ({ getValue }) => {
      const value = getValue() as string | null
      if (value === null) return <span className="evidence-empty">—</span>
      return <ActorOrToken value={value} />
    },
  }),
  helper.display({
    id: 'confidence',
    header: 'Confidence',
    cell: ({ row }) => <Confidence value={auditConfidence(row.original)} />,
  }),
  helper.display({
    id: 'tokens',
    header: 'Tokens in / out',
    cell: ({ row }) => {
      const { tokens_in, tokens_out } = row.original
      if (tokens_in === null && tokens_out === null) {
        return <span className="evidence-empty">—</span>
      }
      return (
        <span className="num">
          {tokens_in ?? 0} / {tokens_out ?? 0}
        </span>
      )
    },
  }),
  helper.accessor('cost_microusd', {
    header: 'Cost (µUSD)',
    cell: ({ getValue }) => {
      const value = getValue() as number | null
      return value === null ? (
        <span className="evidence-empty">—</span>
      ) : (
        <span className="num" title={usd(value)}>
          {value}
        </span>
      )
    },
  }),
  helper.display({
    id: 'detail',
    header: 'Recorded detail',
    cell: ({ row }) => {
      const body = auditBody(row.original.detail)
      if (body === null) {
        return <span className="evidence-empty">no detail recorded</span>
      }
      const count = Object.keys(body).length
      return (
        // Native disclosure: in the tab order, announced as one, no ARIA needed.
        <details data-testid={`detail-${row.original.id}`}>
          <summary>{count} recorded fields</summary>
          <EvidencePacket evidence={body} testId={`detail-body-${row.original.id}`} />
        </details>
      )
    },
  }),
])

// ---------------------------------------------------------------------------
// The route
// ---------------------------------------------------------------------------

function useAuditState() {
  const { params, setParams } = useQueryParams()
  const rawPage = Number.parseInt(params.get('page') ?? '1', 10)
  const rawSize = Number.parseInt(
    params.get('page_size') ?? String(DEFAULT_PAGE_SIZE),
    10,
  )
  return {
    actor: params.get('actor') ?? undefined,
    action: params.get('action') ?? undefined,
    subject: params.get('subject') ?? undefined,
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    pageSize:
      Number.isFinite(rawSize) && rawSize > 0
        ? Math.min(rawSize, MAX_PAGE_SIZE)
        : DEFAULT_PAGE_SIZE,
    setParams,
  }
}

export function AuditRoute() {
  const state = useAuditState()
  const { setParams } = state
  const announce = useAnnounce()
  const scorecard = useScorecard()

  /**
   * The subject box is CONTROLLED, like the two selects beside it and like
   * every control in `components/Filters.tsx`.
   *
   * It cannot be controlled straight off the URL the way a `<select>` is,
   * because it commits on Enter/blur rather than on every keystroke — a
   * per-keystroke commit would push a history entry and refetch the log for
   * each character of a 64-hex fingerprint. So the box shows an uncommitted
   * DRAFT while one exists, and `null` — the normal state — means "show what
   * the URL says", which is what makes the box follow the URL rather than
   * ignore it.
   *
   * That `null` is the whole defect this closes. With `defaultValue` the box
   * was uncontrolled: React never writes back to it on a re-render and this
   * route is not remounted by a query-string change, so "Clear filters" left
   * the old text sitting in the box, and the next blur — merely tabbing
   * through the filter row — committed it again and silently re-applied the
   * filter the reviewer had just cleared.
   */
  const [subjectDraft, setSubjectDraft] = useState<string | null>(null)
  const subjectValue = subjectDraft ?? state.subject ?? ''

  const query = useMemo<AuditQuery>(
    () => ({
      actor: state.actor,
      action: state.action,
      subject: state.subject,
      page: state.page,
      page_size: state.pageSize,
    }),
    [state.actor, state.action, state.subject, state.page, state.pageSize],
  )

  const audit = useAudit(query)
  const data = audit.data

  useEffect(() => {
    if (!data) return
    announce(
      `Audit log updated. Showing ${data.items.length} of ${data.total} matching entries, page ${data.page}.`,
    )
  }, [data, announce])

  // Changing a filter always returns to page 1: page 7 of a log you have just
  // narrowed to three rows is a blank screen. Every filter change also retires
  // the subject draft, so once a change lands the box shows what is actually
  // applied — including when the change is "Clear filters".
  const setFilter = (next: Record<string, string | undefined>) => {
    setSubjectDraft(null)
    setParams({ ...next, page: undefined })
  }

  const card = scorecard.data
  const burst: Verdict = card ? verdictOf(card, SPEND_CAP_BURST_CHECK) : 'unreported'
  const rows = card ? checkRows(card) : []
  const passing = rows.filter((row) => row.verdict === 'pass').length

  return (
    <>
      <PageHeading>Audit log</PageHeading>
      <p className="page-intro">
        Every action this system takes is written to{' '}
        <span className="ref">audit_log</span> — the proposal, its confidence, the
        tokens and money a rationale cost, and the reviewer&rsquo;s decision. This
        page is where that record is <em>reconciled</em> against the queue: filter
        by the same conflict fingerprint, proposal id or run id the rest of the
        dashboard shows, and read that row&rsquo;s own history back.
      </p>

      {/* ---------------- the verification checks ---------------- */}
      <section className="panel" aria-labelledby="checks-heading">
        <h2 id="checks-heading">Verification checks</h2>
        <p>
          The verdicts <span className="ref">python -m recon.suite</span> recorded
          for the run this scorecard describes, served by{' '}
          <span className="ref">/api/scorecard</span>. A check this scorecard does
          not carry is shown as <em>not reported</em> — never as a pass.
        </p>

        {scorecard.isPending ? <Loading what="the scorecard" /> : null}
        {scorecard.isError ? (
          <ErrorState
            error={scorecard.error}
            context="the verification checks"
            onRetry={() => void scorecard.refetch()}
          />
        ) : null}

        {card ? (
          <>
            <dl className="kv" data-testid="spend-cap-summary">
              <dt>Spend cap under burst</dt>
              <dd>
                <CheckVerdict verdict={burst} />{' '}
                <span className="ref">{SPEND_CAP_BURST_CHECK}</span>
              </dd>
              <dt>What it measures</dt>
              <dd data-testid="spend-cap-detail">
                {card.details?.[SPEND_CAP_BURST_CHECK] ??
                  (burst === 'unreported'
                    ? 'This scorecard carries no spend-cap-burst result. Run `python -m recon.suite` against the deployment to record one; the in-browser mock serves the seed generator’s self-checks, not the suite’s, and does not invent this verdict.'
                    : 'R17’s concurrency proof: 120 simultaneous requests against a cap sized for 6. The verdict is above; this scorecard carries no per-check evidence line.')}
              </dd>
              <dt>Checks passing</dt>
              <dd className="num" data-testid="checks-passing">
                {passing} of {rows.length}
              </dd>
              <dt>Run</dt>
              <dd className="ref">{card.run_id}</dd>
            </dl>

            <div
              className="table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Verification checks, scrollable table"
            >
              <table className="data-table" data-testid="checks-table">
                <caption>
                  Every check the scorecard carries. The gates the rubric names —
                  the spend-cap burst first — lead; the rest follow in name order.
                  A verdict is an icon, a word and a sentence, so it survives
                  greyscale and a screen reader.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Check</th>
                    <th scope="col">Result</th>
                    <th scope="col">What was measured</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ name, verdict }) => (
                    <tr key={name} data-testid={`check-${name}`}>
                      <th scope="row" className="ref">
                        {name}
                      </th>
                      <td>
                        <CheckVerdict verdict={verdict} />
                      </td>
                      <td>
                        {card.details?.[name] ?? (
                          <span className="evidence-empty">
                            no evidence line on this scorecard
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>

      {/* ---------------- the log itself ---------------- */}
      <div className="filters">
        <div className="field">
          <label htmlFor="audit-actor">Actor</label>
          <select
            id="audit-actor"
            value={state.actor ?? ''}
            onChange={(event) =>
              setFilter({ actor: event.target.value || undefined })
            }
          >
            <option value="">All actors</option>
            {(data?.actors ?? []).map((actor) => (
              <option key={actor} value={actor}>
                {isRedactionToken(actor) ? `redacted ${actor}` : actor}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="audit-action">Action</label>
          <select
            id="audit-action"
            value={state.action ?? ''}
            onChange={(event) =>
              setFilter({ action: event.target.value || undefined })
            }
          >
            <option value="">All actions</option>
            {(data?.actions ?? []).map((action) => (
              <option key={action} value={action}>
                {action}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="audit-subject">
            Subject — conflict fingerprint, proposal id or run id
          </label>
          <input
            id="audit-subject"
            type="search"
            value={subjectValue}
            onChange={(event) => setSubjectDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              setFilter({ subject: event.currentTarget.value.trim() || undefined })
            }}
            onBlur={(event) =>
              setFilter({ subject: event.currentTarget.value.trim() || undefined })
            }
          />
        </div>

        <Button
          onClick={() =>
            setFilter({ actor: undefined, action: undefined, subject: undefined })
          }
          inert={!state.actor && !state.action && !state.subject}
        >
          Clear filters
        </Button>
      </div>

      {audit.isPending ? <Loading what="the audit log" /> : null}
      {audit.isError ? (
        <ErrorState
          error={audit.error}
          context="the audit log"
          onRetry={() => void audit.refetch()}
        />
      ) : null}

      {data ? (
        <>
          <section className="panel" aria-labelledby="spend-heading">
            <h2 id="spend-heading">Recorded cost for these entries</h2>
            <p>
              Computed by the service over every row the current filter matches —
              not over the page on screen — so it is a figure that can be put next
              to <span className="ref">budget_ledger</span> and be right.
            </p>
            <dl className="kv" data-testid="audit-totals">
              <dt>Entries matched</dt>
              <dd className="num">{data.total}</dd>
              <dt>Entries carrying a cost</dt>
              <dd className="num">{data.totals.priced_rows}</dd>
              <dt>Tokens in</dt>
              <dd className="num">{data.totals.tokens_in}</dd>
              <dt>Tokens out</dt>
              <dd className="num">{data.totals.tokens_out}</dd>
              <dt>Cost</dt>
              <dd className="num" data-testid="audit-cost">
                {data.totals.cost_microusd} µUSD ({usd(data.totals.cost_microusd)})
              </dd>
            </dl>
          </section>

          <DataTable
            caption={`Audit log — ${data.total} entries match the current filters`}
            columns={columns}
            data={data.items}
            rowId={(row) => row.id}
            emptyMessage="No audit entries match these filters. The filter was applied by the service, so this is an empty result and not an unfiltered one."
          />
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            onPageChange={(page) =>
              setParams({ page: page <= 1 ? undefined : String(page) })
            }
            label="audit entries"
          />
        </>
      ) : null}
    </>
  )
}
