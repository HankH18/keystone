/**
 * The API contract the dashboard is built against.
 *
 * ============================================================================
 * PINNED — taken verbatim from docs/DESIGN.md §HTTP API / §Dashboard ↔ API and
 * §Data models. Do not change these without changing DESIGN.md.
 * ============================================================================
 *   GET  /api/conflicts                (+ filters source/type/status, paginated)
 *   GET  /api/proposals                (+ filters)
 *   POST /api/proposals/{id}/approve
 *   POST /api/proposals/{id}/reject
 *   POST /api/proposals/{id}/apply     (approved only; auto path per R24)
 *   GET  /api/scorecard                (latest suite results, for reconciliation)
 *   Auth: header `X-Api-Key`, the committed ADMIN demo key.
 *   Errors: RFC7807-style {type, title, status, detail}.
 *   Row fields: the column names of DESIGN §Data models `conflicts` / `proposals`.
 *   proposals.status ∈ pending|approved|rejected|applied|rolled_back|sensitive_hold
 *
 * ============================================================================
 * ASSUMED — NOT promised by DESIGN.md. The service does not exist yet (T-5/T-7/
 * T-8 build it). Each of these is a shape this dashboard had to choose; every
 * one of them is isolated to this file plus src/lib/httpClient.ts, so the change
 * when the real API lands is local. They are also listed in dashboard/README.md.
 * ============================================================================
 *   A1  Pagination envelope `{items, page, page_size, total}`, request params
 *       `page` (1-based) and `page_size`.
 *   A2  `GET /api/conflicts/{id}` and `GET /api/proposals/{id}` exist. DESIGN
 *       pins the collection endpoints and the per-id POST actions but no per-id
 *       GET; the detail routes need one.
 *   A3  `/api/proposals` accepts a `conflict_id` filter, so a conflict's detail
 *       page can show its proposal. (DESIGN says "+ filters" without listing.)
 *   A4  The Scorecard body shape (see `Scorecard` below). DESIGN pins only
 *       "latest suite results for dashboard reconciliation".
 *   A5  approve/reject return the updated proposal. The UI does NOT depend on
 *       this: it refetches from the list/detail endpoints either way.
 *   A6  `conflicts.status` vocabulary — `open` and `escalated:oscillation`.
 *       DESIGN pins the column but not its values; `escalated:oscillation` is
 *       named in DESIGN §Reconciler. Unknown values render as a labelled badge
 *       rather than crashing, so a wider real vocabulary degrades gracefully.
 *   A7  `conflicts.sources` is a string array of source ids. DESIGN pins the
 *       jsonb column; contract §8 pins the value set.
 *   A8  `/api/proposals` accepts `source` and `type` filters. This is the ONE
 *       assumption here with a SILENT failure mode, and it is the reason
 *       `CONTRACT_ASSUMPTIONS` below exists as data rather than prose: the
 *       `proposals` table pinned in DESIGN §Data models has NO source and NO
 *       type column, so serving these two filters requires a JOIN to
 *       `conflicts` that the service may never implement. A service that
 *       ignores an unknown query param returns 200 with the UNFILTERED page —
 *       wrong results on a reviewer surface, not an error. Nothing on a
 *       proposal row can prove the filter was applied either, so the client
 *       cannot verify it the way it verifies the others; it warns loudly
 *       instead (src/lib/filterGuard.ts).
 *   A9  `evidence.observed_values` — a map of field path → observed value
 *       inside `evidence jsonb`. DESIGN pins the column, not its interior. The
 *       conflict detail reads this key to show "CRM says X, App DB says Y";
 *       absent, the observed-values column degrades to "—".
 *   A10 `action.target_path` — a source-qualified field path inside
 *       `action jsonb`. Same status: read defensively by `targetPath()`, and a
 *       missing or non-string value renders "evidence only — no field write".
 *
 * The machine-readable copy of this list is `CONTRACT_ASSUMPTIONS` below. It is
 * what the service tickets (T-5/T-7/T-8) have to answer to; a prose list in a
 * comment cannot be asserted on, and this one is.
 *
 * Anything NOT in the lists above is derived locally from committed documents
 * (see RULE_ID_BY_TYPE / CONFLICT_TYPE_LABEL below) rather than read off a
 * response body, precisely so the UI cannot depend on an unpromised field.
 */

// ---------------------------------------------------------------------------
// Contract assumptions, as data
// ---------------------------------------------------------------------------

/**
 * How an assumption fails if the service does not honour it.
 *
 * `loud` — the dashboard shows an error or a warning; a reviewer cannot mistake
 * it for a correct screen.
 * `silent` — the dashboard would show WRONG DATA that looks right. Every entry
 * here must name the guard that turns it loud, or it is a defect.
 */
export type AssumptionFailureMode = 'loud' | 'silent'

export interface ContractAssumption {
  /** A1 … A10 — the ids used in this file's header and in dashboard/README.md. */
  id: string
  /** The endpoint or field the assumption is about. */
  subject: string
  /** What the dashboard assumes. */
  assumption: string
  /** What DESIGN.md actually pins, if anything. */
  pinned: string
  failure: AssumptionFailureMode
  /** What happens if the service does not honour it — and what makes it visible. */
  consequence: string
}

/**
 * The assumptions this dashboard makes about an API that does not exist yet.
 *
 * This is the list to carry into the service tickets. It is exported as data,
 * and asserted on in src/lib/contract.test.ts, so an assumption cannot be added
 * to the client without being added here.
 */
export const CONTRACT_ASSUMPTIONS: readonly ContractAssumption[] = [
  {
    id: 'A1',
    subject: 'GET /api/conflicts, GET /api/proposals',
    assumption:
      'Pagination envelope {items, page, page_size, total}; request params `page` (1-based) and `page_size`.',
    pinned: 'DESIGN pins "paginated" and nothing else.',
    failure: 'loud',
    consequence:
      'A different envelope leaves `items` undefined and the route renders its error state.',
  },
  {
    id: 'A2',
    subject: 'GET /api/conflicts/{id}, GET /api/proposals/{id}',
    assumption: 'A per-id GET exists for both collections.',
    pinned:
      'DESIGN pins the collection endpoints and the per-id POST actions, but no per-id GET.',
    failure: 'loud',
    consequence: 'A 404/405 renders the detail route error state.',
  },
  {
    id: 'A3',
    subject: 'GET /api/proposals?conflict_id=',
    assumption: 'A `conflict_id` filter, so a conflict detail can show its proposal.',
    pinned: 'DESIGN says "(+ filters)" without listing them.',
    failure: 'silent',
    consequence:
      'Ignored, it returns the unfiltered first page and the conflict detail would show SOMEONE ELSE\'S proposal. Guarded: `conflict_id` is on every proposal row, so filterGuard.ts verifies it and warns.',
  },
  {
    id: 'A4',
    subject: 'GET /api/scorecard',
    assumption:
      'Body shape {generated_at, run_id, conflicts:{total,by_type}, proposals:{total,by_status}, checks}.',
    pinned: 'DESIGN pins only "latest suite results for dashboard reconciliation".',
    failure: 'loud',
    consequence:
      'A different shape leaves the overview reporting Mismatch for every type, or its error state.',
  },
  {
    id: 'A5',
    subject: 'POST /api/proposals/{id}/approve|reject|apply',
    assumption: 'The updated proposal is returned in the response body.',
    pinned: 'DESIGN pins the endpoints, not their responses.',
    failure: 'loud',
    consequence:
      'Not depended on: the UI refetches the proposal after every decision, so a different body costs nothing.',
  },
  {
    id: 'A6',
    subject: 'conflicts.status',
    assumption: 'The vocabulary is `open` and `escalated:oscillation`.',
    pinned:
      'DESIGN pins the column and names `escalated:oscillation` in §Reconciler, but pins no value set.',
    failure: 'loud',
    consequence:
      'An unknown value renders as a labelled "unknown status" badge with the raw value, never a crash.',
  },
  {
    id: 'A7',
    subject: 'conflicts.sources',
    assumption: 'A JSON array of source id strings.',
    pinned: 'DESIGN pins the jsonb column; invariant-contract §8 pins the value set.',
    failure: 'loud',
    consequence: 'A non-array throws in the row renderer and the list shows its error state.',
  },
  {
    id: 'A8',
    subject: 'GET /api/proposals?source=&type=',
    assumption:
      '`/api/proposals` accepts `source` and `type` filters — the same two the conflicts endpoint takes.',
    pinned:
      'DESIGN says "(+ filters)" for proposals without listing them, and the `proposals` table has NO source and NO type column: serving these requires a JOIN to `conflicts`.',
    failure: 'silent',
    consequence:
      'If the service ignores them it returns 200 with the UNFILTERED page — wrong rows on a reviewer surface, with no error. A proposal row carries neither field, so the client cannot verify the filter was applied: filterGuard.ts raises an `unverifiable` warning banner whenever either filter is in use. THIS IS THE ASSUMPTION THE SERVICE TICKETS MUST ANSWER FIRST.',
  },
  {
    id: 'A9',
    subject: 'proposals.evidence.observed_values',
    assumption:
      '`evidence jsonb` contains an `observed_values` object keyed by source-qualified field path.',
    pinned: 'DESIGN pins the `evidence jsonb` column, not its interior.',
    failure: 'loud',
    consequence:
      'Read defensively: absent, the conflict detail shows "—" in the Observed values column. The rest of the packet is rendered generically, key by key, so no other key is assumed.',
  },
  {
    id: 'A10',
    subject: 'proposals.action.target_path',
    assumption:
      '`action jsonb` contains a `target_path` string naming the field a fix would write.',
    pinned: 'DESIGN pins the `action jsonb` column, not its interior.',
    failure: 'loud',
    consequence:
      'Read defensively by `targetPath()`: absent or non-string renders "evidence only — no field write" rather than guessing a path.',
  },
]

// ---------------------------------------------------------------------------
// Filter honesty
// ---------------------------------------------------------------------------

/**
 * A filter the dashboard sent that the response does not demonstrably honour.
 *
 * `ignored` — proven: a returned row contradicts the filter.
 * `unverifiable` — the returned rows cannot prove it either way (A8).
 *
 * Attached to a `Page` by the CLIENT (src/lib/filterGuard.ts). It never comes
 * from the service, and it is never confused with one: `warnings` is not part
 * of the response envelope the service is asked for.
 */
export interface FilterWarning {
  kind: 'ignored' | 'unverifiable'
  endpoint: '/api/conflicts' | '/api/proposals'
  param: 'source' | 'type' | 'status' | 'conflict_id'
  /** The value the dashboard asked to filter on. */
  value: string
  /** The `CONTRACT_ASSUMPTIONS` id this warning is evidence about. */
  assumption: string
  /** Reviewer-facing sentence. */
  detail: string
}

/** Source ids. contract §8: `sources_involved ⊆ {"crm","appdb","payments"}`. */
export const SOURCE_IDS = ['appdb', 'crm', 'payments'] as const
export type SourceId = (typeof SOURCE_IDS)[number]

export const SOURCE_LABEL: Record<SourceId, string> = {
  appdb: 'App DB',
  crm: 'CRM',
  payments: 'Payments',
}

/** The fourteen conflict types — invariant-contract §5.5, in catalogue order. */
export const CONFLICT_TYPES = [
  'C1',
  'C2',
  'C3',
  'C4',
  'C5',
  'C6',
  'C7',
  'C8',
  'C9',
  'C10',
  'C11',
  'C12',
  'C13',
  'C14',
] as const
export type ConflictType = (typeof CONFLICT_TYPES)[number]

/**
 * Type → rule id and human name, invariant-contract §5.5. Derived from the
 * committed contract, NOT read off the API: the `conflicts` table pinned in
 * DESIGN §Data models has no `rule_id` column, so reading one would be
 * depending on a shape the contract does not promise.
 */
export const RULE_ID_BY_TYPE: Record<ConflictType, string> = {
  C1: 'R-001',
  C2: 'R-002',
  C3: 'R-003',
  C4: 'R-004',
  C5: 'R-005',
  C6: 'R-006',
  C7: 'R-007',
  C8: 'R-008',
  C9: 'R-009',
  C10: 'R-010',
  C11: 'R-011',
  C12: 'R-012',
  C13: 'R-013',
  C14: 'R-014',
}

export const CONFLICT_TYPE_LABEL: Record<ConflictType, string> = {
  C1: 'Paid but no deal',
  C2: 'Payment with no person',
  C3: 'Duplicate by email (in-source)',
  C4: 'Same person, different emails',
  C5: 'Record in one source only',
  C6: 'Field disagreement',
  C7: 'Enrolled but unpaid',
  C8: 'Dropped sibling',
  C9: 'Stale pointer',
  C10: 'Merge-collapsed record',
  C11: 'Duplicate payment',
  C12: 'Wrong-amount payment',
  C13: 'Refund not reflected',
  C14: 'Sensitive-field-only fix',
}

export function isConflictType(value: string): value is ConflictType {
  return (CONFLICT_TYPES as readonly string[]).includes(value)
}

/** PINNED: DESIGN §Data models, `proposals.status`. */
export const PROPOSAL_STATUSES = [
  'pending',
  'approved',
  'rejected',
  'applied',
  'rolled_back',
  'sensitive_hold',
] as const
export type ProposalStatus = (typeof PROPOSAL_STATUSES)[number]

/** ASSUMED (A6). */
export const CONFLICT_STATUSES = ['open', 'escalated:oscillation'] as const
export type ConflictStatus = string

/**
 * Source-qualified field paths classified sensitive by invariant-contract §6.
 * Used ONLY to explain a proposal in the UI ("this path is a sensitive field").
 * It never overrides the service: `proposal.sensitive` and `proposal.status`
 * are the authority, per §6 "classification wins over confidence".
 */
export const SENSITIVE_FIELDS: ReadonlySet<string> = new Set([
  'crm.contact.first_name',
  'crm.contact.last_name',
  'crm.contact.dob',
  'appdb.student.first_name',
  'appdb.student.last_name',
  'appdb.student.dob',
  'appdb.student.student_number',
  'payments.payment.payer_email',
  'payments.payment.payer_name',
  'appdb.enrollment.billing_owner_email',
  'crm.contact.email',
  'appdb.student.guardian_email',
  'appdb.student.guardian2_email',
  'appdb.enrollment.stage',
  'appdb.enrollment.deposit_paid_at',
  'appdb.student.status',
  'payments.payment.status',
  'crm.deal.stage',
  'crm.contact.marketing_consent',
  'appdb.student.communication_opt_out',
])

/** invariant-contract §6 AUTO_APPLY_ELIGIBLE. */
export const AUTO_APPLY_ELIGIBLE: ReadonlySet<string> = new Set([
  'appdb.enrollment.crm_deal_id',
  'payments.payment.external_ref',
  'crm.contact.external_id',
  'crm.contact.grade',
  'crm.contact.lifecycle_stage',
])

/**
 * COMPARED_FIELDS (§2.4) — the ONLY producer of `disagreeing_fields`. Used to
 * group a conflict's disagreeing paths back into the comparison ROWS the
 * contract reasons about, so the reviewer sees "grade: CRM says X, App DB says
 * Y" rather than a flat list of paths.
 */
export interface ComparedFieldRow {
  logical: string
  left: string
  right: string
}

export const COMPARED_FIELDS: readonly ComparedFieldRow[] = [
  {
    logical: 'name_first',
    left: 'crm.contact.first_name',
    right: 'appdb.student.first_name',
  },
  {
    logical: 'name_last',
    left: 'crm.contact.last_name',
    right: 'appdb.student.last_name',
  },
  { logical: 'dob', left: 'crm.contact.dob', right: 'appdb.student.dob' },
  { logical: 'grade', left: 'crm.contact.grade', right: 'appdb.student.grade' },
  {
    logical: 'stage',
    left: 'crm.deal.stage',
    right: 'appdb.enrollment.stage',
  },
  {
    logical: 'lifecycle',
    left: 'crm.contact.lifecycle_stage',
    right: 'appdb.student.status',
  },
]

/** A record reference, `"{source}:{entity_type}:{natural_key}"` (§5.4). */
export interface ParsedRef {
  raw: string
  source: string
  entityType: string
  key: string
}

export function parseRef(raw: string): ParsedRef {
  const [source = '', entityType = '', ...rest] = raw.split(':')
  return { raw, source, entityType, key: rest.join(':') }
}

// ---------------------------------------------------------------------------
// Response shapes
// ---------------------------------------------------------------------------

/** ASSUMED (A1). */
export interface Page<T> {
  items: T[]
  page: number
  page_size: number
  total: number
  /**
   * CLIENT-SIDE ONLY — never sent by the service. Filters the dashboard asked
   * for that this response does not demonstrably honour. See filterGuard.ts.
   */
  warnings?: FilterWarning[]
}

/** PINNED column names — DESIGN §Data models `conflicts`. */
export interface Conflict {
  id: string
  fingerprint: string
  type: ConflictType
  entity_refs: string[]
  sources: SourceId[]
  disagreeing_fields: string[]
  status: ConflictStatus
  first_seen_run: string
  last_seen_run: string
}

/** PINNED column names — DESIGN §Data models `proposals`. */
export interface Proposal {
  id: string
  conflict_id: string
  fingerprint: string
  /** `action jsonb` — DESIGN pins the column, not its interior. Rendered generically. */
  action: Record<string, unknown>
  confidence: number
  /** `evidence jsonb` — same: rendered generically, key by key. */
  evidence: Record<string, unknown>
  rationale: string | null
  status: ProposalStatus
  sensitive: boolean
  created_run: string
  decided_by: string | null
  decided_at: string | null
}

/** ASSUMED (A4). */
export interface Scorecard {
  generated_at: string
  run_id: string
  conflicts: {
    total: number
    by_type: Partial<Record<ConflictType, number>>
  }
  proposals: {
    total: number
    by_status: Partial<Record<ProposalStatus, number>>
  }
  checks: Record<string, boolean>
}

export interface ConflictQuery {
  source?: SourceId
  type?: ConflictType
  status?: string
  page?: number
  page_size?: number
}

export interface ProposalQuery {
  source?: SourceId
  type?: ConflictType
  status?: ProposalStatus
  conflict_id?: string
  page?: number
  page_size?: number
}

/** Hard cap. R11's non-goal is explicit: never load 100k rows client-side. */
export const MAX_PAGE_SIZE = 100
export const DEFAULT_PAGE_SIZE = 25

/** The interface both the real HTTP client and the mock implement. */
export interface KeystoneApi {
  listConflicts(query: ConflictQuery, signal?: AbortSignal): Promise<Page<Conflict>>
  getConflict(id: string, signal?: AbortSignal): Promise<Conflict>
  listProposals(query: ProposalQuery, signal?: AbortSignal): Promise<Page<Proposal>>
  getProposal(id: string, signal?: AbortSignal): Promise<Proposal>
  approveProposal(id: string): Promise<Proposal>
  rejectProposal(id: string): Promise<Proposal>
  applyProposal(id: string): Promise<Proposal>
  getScorecard(signal?: AbortSignal): Promise<Scorecard>
}
