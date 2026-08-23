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
 *   - a filter the row shape cannot speak to     → `unverifiable` (A8).
 * Either way the reviewer sees a warning above the table instead of trusting a
 * filtered heading over unfiltered rows.
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
 * `source` and `type` are NOT on the row (A8) and can never be verified here —
 * they get an `unverifiable` warning whenever they are used.
 */
export function checkProposalFilters(
  query: ProposalQuery,
  items: readonly Proposal[],
): FilterWarning[] {
  const warnings: FilterWarning[] = []

  // Unverifiable warnings do NOT depend on the rows: an empty page is exactly
  // as unprovable as a full one, and staying quiet on it would hide A8 on the
  // one screen where a reviewer is most likely to trust an empty result.
  if (query.type) {
    warnings.push(unverifiable('/api/proposals', 'type', query.type, 'A8'))
  }
  if (query.source) {
    warnings.push(unverifiable('/api/proposals', 'source', query.source, 'A8'))
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
