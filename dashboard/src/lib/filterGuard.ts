/**
 * Filter honesty: prove the service applied the filters it was sent, or say so.
 *
 * WHY THIS EXISTS. DESIGN §HTTP API pins `GET /api/proposals` "(+ filters)"
 * without listing them, and the `proposals` table it pins has no `source` and
 * no `type` column — serving those two filters needs a JOIN to `conflicts`
 * (CONTRACT_ASSUMPTIONS A8). A service that does not implement a query param
 * does not usually reject it: it ignores it and answers 200 with the
 * UNFILTERED page. On a reviewer surface that is the worst possible failure —
 * the screen says "Proposals · source = Payments" above rows from every source,
 * and nothing anywhere is red.
 *
 * So every list response is checked against the query that produced it:
 *   - a returned row that CONTRADICTS a filter   → `ignored` (proven wrong);
 *   - a filter the row shape cannot speak to     → `unverifiable` (A8);
 *   - a filter every row PROVES was applied      → nothing at all.
 * The reviewer sees a warning above the table instead of trusting a filtered
 * heading over unfiltered rows — and does NOT see one over rows the response
 * proved are filtered.
 *
 * That third arm is new, and it is why this module no longer says "`source` and
 * `type` can never be verified here". `recon/api/review.py::_proposal_row`
 * attaches the joined `conflict_type` and `conflict_sources` for exactly this
 * purpose ("so that A8 ... becomes verifiable from the row by any client that
 * wants to check it"). A row that carries them is checked; a row that does not
 * is still unprovable and still warns.
 *
 * The check is pure and idempotent, so it can run in the HTTP client (where the
 * real service's answer arrives) and again in the query hooks (which cover any
 * client, mock included) without producing anything different.
 */
import type {
  Conflict,
  ConflictQuery,
  FilterWarning,
  Page,
  Proposal,
  ProposalQuery,
} from './contract'

function ignored(
  endpoint: FilterWarning['endpoint'],
  param: FilterWarning['param'],
  value: string,
  offending: number,
  total: number,
  assumption: string,
): FilterWarning {
  return {
    kind: 'ignored',
    endpoint,
    param,
    value,
    assumption,
    detail:
      `${endpoint} was asked for ${param}=${value}, but ${offending} of the ${total} rows it ` +
      `returned do not match. The service is ignoring or mis-applying this filter, so what is ` +
      `on screen is NOT the filtered set. Do not review from it.`,
  }
}

function unverifiable(
  endpoint: FilterWarning['endpoint'],
  param: FilterWarning['param'],
  value: string,
  assumption: string,
): FilterWarning {
  return {
    kind: 'unverifiable',
    endpoint,
    param,
    value,
    assumption,
    detail:
      `${endpoint} was asked for ${param}=${value}, but a proposal row carries no ${param}: ` +
      `DESIGN §Data models gives the proposals table no ${param} column, so serving this filter ` +
      `requires a JOIN to conflicts that the service may not implement (assumption ${assumption}). ` +
      `The dashboard cannot confirm these rows are filtered. Confirm against /conflicts before acting.`,
  }
}

/**
 * The joined conflict members `review.py::_proposal_row` attaches, if present.
 *
 * A8's original wording — "nothing on a proposal row can prove the filter was
 * applied" — was true of the pinned column list and is no longer true of the
 * response: the service adds `conflict_type` and `conflict_sources` for exactly
 * this purpose. Read defensively, one row at a time, because "the service sends
 * them" is not something a client may assume: a row that lacks them is
 * `unprovable`, which is a different verdict from `contradicts`.
 */
type Verdict = 'holds' | 'contradicts' | 'unprovable'

function typeVerdict(row: Proposal, wanted: string): Verdict {
  const value = (row as { conflict_type?: unknown }).conflict_type
  if (typeof value !== 'string') return 'unprovable'
  return value === wanted ? 'holds' : 'contradicts'
}

function sourceVerdict(row: Proposal, wanted: string): Verdict {
  const value = (row as { conflict_sources?: unknown }).conflict_sources
  if (!Array.isArray(value)) return 'unprovable'
  return value.includes(wanted) ? 'holds' : 'contradicts'
}

/**
 * One A8 filter, judged against whatever the rows can actually show.
 *
 * A single contradicting row proves the filter was ignored, and that outranks
 * every other row. Otherwise, a single row that cannot speak to the filter
 * leaves the page unproven — silence there would be the dashboard vouching for
 * a filter on the strength of the rows that happened to carry the evidence.
 */
function checkJoinedFilter(
  param: 'source' | 'type',
  value: string,
  items: readonly Proposal[],
  verdictOf: (row: Proposal, wanted: string) => Verdict,
): FilterWarning | null {
  const verdicts = items.map((item) => verdictOf(item, value))
  const offending = verdicts.filter((verdict) => verdict === 'contradicts').length
  if (offending > 0) {
    return ignored('/api/proposals', param, value, offending, items.length, 'A8')
  }
  if (verdicts.some((verdict) => verdict === 'unprovable') || items.length === 0) {
    return unverifiable('/api/proposals', param, value, 'A8')
  }
  return null
}

/** Conflicts: every filter the dashboard sends is verifiable from the row. */
export function checkConflictFilters(
  query: ConflictQuery,
  items: readonly Conflict[],
): FilterWarning[] {
  const warnings: FilterWarning[] = []
  const total = items.length
  if (total === 0) return warnings

  if (query.type) {
    const bad = items.filter((item) => item.type !== query.type).length
    if (bad > 0) {
      warnings.push(ignored('/api/conflicts', 'type', query.type, bad, total, 'PINNED'))
    }
  }
  const source = query.source
  if (source) {
    const bad = items.filter(
      (item) => !Array.isArray(item.sources) || !item.sources.includes(source),
    ).length
    if (bad > 0) {
      warnings.push(ignored('/api/conflicts', 'source', source, bad, total, 'A7'))
    }
  }
  if (query.status) {
    const bad = items.filter((item) => item.status !== query.status).length
    if (bad > 0) {
      warnings.push(ignored('/api/conflicts', 'status', query.status, bad, total, 'A6'))
    }
  }
  return warnings
}

/**
 * Proposals: `status` and `conflict_id` are on the row and are verified.
 *
 * `source` and `type` are not COLUMNS of `proposals` (A8), but the service
 * attaches the joined `conflict_type` / `conflict_sources` precisely so a
 * client can check them — so they are verified from those members when the row
 * carries them, and fall back to the `unverifiable` warning when it does not.
 * Crying `unverifiable` over a filter the response demonstrably honoured is not
 * caution, it is a red alert box over correct data, and a reviewer who learns
 * to ignore one banner ignores the next one too.
 */
export function checkProposalFilters(
  query: ProposalQuery,
  items: readonly Proposal[],
): FilterWarning[] {
  const warnings: FilterWarning[] = []

  // These two do NOT short-circuit on an empty page: an empty page proves
  // nothing, and staying quiet on it would hide A8 on the one screen where a
  // reviewer is most likely to trust an empty result.
  if (query.type) {
    const warning = checkJoinedFilter('type', query.type, items, typeVerdict)
    if (warning) warnings.push(warning)
  }
  if (query.source) {
    const warning = checkJoinedFilter('source', query.source, items, sourceVerdict)
    if (warning) warnings.push(warning)
  }

  const total = items.length
  if (total === 0) return warnings

  if (query.status) {
    const bad = items.filter((item) => item.status !== query.status).length
    if (bad > 0) {
      warnings.push(
        ignored('/api/proposals', 'status', query.status, bad, total, 'PINNED'),
      )
    }
  }
  if (query.conflict_id) {
    const bad = items.filter((item) => item.conflict_id !== query.conflict_id).length
    if (bad > 0) {
      warnings.push(
        ignored('/api/proposals', 'conflict_id', query.conflict_id, bad, total, 'A3'),
      )
    }
  }
  return warnings
}

/** Attach the warnings for a conflicts page. Pure; safe to re-apply. */
export function guardConflictPage(
  query: ConflictQuery,
  page: Page<Conflict>,
): Page<Conflict> {
  const warnings = checkConflictFilters(query, page?.items ?? [])
  return warnings.length > 0 ? { ...page, warnings } : stripWarnings(page)
}

/** Attach the warnings for a proposals page. Pure; safe to re-apply. */
export function guardProposalPage(
  query: ProposalQuery,
  page: Page<Proposal>,
): Page<Proposal> {
  const warnings = checkProposalFilters(query, page?.items ?? [])
  return warnings.length > 0 ? { ...page, warnings } : stripWarnings(page)
}

/**
 * A page with nothing wrong carries no `warnings` key — including when the
 * value came from somewhere that put one there. A warning is only ever this
 * module's own current verdict, never a stale or forged one.
 */
function stripWarnings<T>(page: Page<T>): Page<T> {
  if (!page || page.warnings === undefined) return page
  const rest = { ...page }
  delete rest.warnings
  return rest
}
