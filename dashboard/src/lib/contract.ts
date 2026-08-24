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
 *                                      `?auto=true` selects R24's gated write;
 *                                      the default is the reviewer's own.
 *   GET  /api/scorecard                (latest suite results, for reconciliation)
 *   Auth: header `X-Api-Key`, the committed ADMIN demo key.
 *   Errors: RFC7807-style {type, title, status, detail}.
 *   Row fields: the column names of DESIGN §Data models `conflicts` / `proposals`.
 *   proposals.status ∈ pending|approved|rejected|applied|rolled_back|sensitive_hold
 *
 * ============================================================================
 * ASSUMED — NOT promised by DESIGN.md. Each of these is a shape this dashboard
 * had to choose while the service was still being built (T-5/T-7/T-8); every one
 * of them is isolated to this file plus src/lib/httpClient.ts, so the change when
 * the real API landed was local. They are also listed in dashboard/README.md.
 *
 * THE SERVICE NOW EXISTS, and the list is kept as the RECORD of what was assumed
 * and how each one was answered — including the two that were answered by being
 * proved wrong (A9, A10). `service/tests/api/test_contract_assumptions.py` reads
 * this list as data and requires a verdict for every id in it, so the entries
 * below are load-bearing and are not history to be tidied away.
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
 *   A8  `/api/proposals` accepts `source` and `type` filters. The `proposals`
 *       table pinned in DESIGN §Data models has NO source and NO type column,
 *       so serving these two filters requires a JOIN to `conflicts`, and a
 *       service that ignores an unknown query param returns 200 with the
 *       UNFILTERED page — wrong results on a reviewer surface, not an error.
 *       ANSWERED: `recon/api/review.py` serves both through the JOIN, 422s an
 *       unknown value, and puts `conflict_type` / `conflict_sources` on every
 *       proposal row so the filter becomes VERIFIABLE from the row. filterGuard
 *       verifies from those two members when they are present and falls back to
 *       the `unverifiable` warning when they are not.
 *   A9  observed values — a map of field path → observed value. NOT at
 *       `evidence.observed_values`, which is where this dashboard used to look
 *       and where nothing ever wrote one. The service puts it in two places:
 *       TOP-LEVEL on the conflict row (`review.py::_conflict_row`) and nested
 *       at `evidence.conflict.observed_values`
 *       (`reconciler.py::EvidencePacket.as_dict`). The conflict detail prefers
 *       the row's copy and falls back to the packet; absent both, the
 *       observed-values column degrades to "—".
 *   A10 the field a fix would write. NOT `action.target_path`: migration 0007's
 *       `ck_proposals_action_vocabulary` is a VALIDATED CHECK requiring
 *       `action - 'set' = '{}'::jsonb`, so `action` has EXACTLY ONE top-level
 *       key, named `set`, and a `target_path` sibling is refused by the
 *       database. The write set is read off `action.set` by `writePaths()` —
 *       every key it names plus every member a nested assignment carries, since
 *       `entities.current` nests one object (`survived`) whose members are
 *       themselves contract paths. `{"set": {}}` is the evidence-only proposal;
 *       an action with no object-valued `set` is reported UNREADABLE, loudly,
 *       rather than being mistaken for one.
 *
 * The machine-readable copy of this list is `CONTRACT_ASSUMPTIONS` below. It is
 * what the service tickets (T-5/T-7/T-8) have to answer to; a prose list in a
 * comment cannot be asserted on, and this one is.
 *
 * Anything NOT in the lists above is derived locally from committed documents
 * (see RULE_ID_BY_TYPE / CONFLICT_TYPE_LABEL below) rather than read off a
 * response body, precisely so the UI cannot depend on an unpromised field.
 *
 * ============================================================================
 * TWO SHAPES THAT ARE NOT IN THE ASSUMPTION LIST, AND WHY
 * ============================================================================
 * `proposals.events` (the `proposal_events` reversal ledger, {@link
 * ProposalEvent}) and `POST /api/proposals/{id}/rollback` ({@link
 * KeystoneApi.rollbackProposal}) are both DESIGN-unpinned additions, so by the
 * rule above they belong in the numbered list. NEITHER IS ASSUMED ANY MORE —
 * both are now in `recon/api/review.py` (`get_proposal` attaches
 * `body["events"] = _read_events(...)`, and `rollback_endpoint` serves the
 * reversal), and the member names and column names below are read off
 * `_event_row` and `RollbackResult.as_dict` rather than chosen here. They are
 * still deliberately NOT in the numbered list:
 * the numbered list is parsed as data by
 * `service/tests/api/test_contract_assumptions.py`, which requires every id it
 * finds to be answered or excused IN THE SERVICE, and `src/lib/contract.test.ts`
 * pins the enumeration to A1-A10 exactly. Adding an eleventh entry is therefore
 * a coordinated service-and-client change, not a client-side one, and inventing
 * it here would turn one honest gap into two red suites.
 *
 * So they are recorded here instead, with the same discipline:
 *   - both stay OPTIONAL in the types below, and the UI renders nothing at all
 *     when they are absent (`rollbackProposal` is an optional method; `events`
 *     is an optional member with three distinct empty states in
 *     `ProposalDetail.tsx` — absent, empty, and present-but-unreadable). A
 *     dashboard deployed against an older service build must degrade, not crash.
 *   - both fail LOUD, never silent: an absent ledger says the service did not
 *     send one rather than showing an empty ledger as "no write happened", and
 *     a rollback the service cannot serve renders the service's own 404/405.
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
      'If the service ignores them it returns 200 with the UNFILTERED page — wrong rows on a reviewer surface, with no error. ANSWERED: `recon/api/review.py` serves both through the JOIN and puts the joined `conflict_type` / `conflict_sources` on every proposal row, so filterGuard.ts now VERIFIES the filter from the row and warns `ignored` when a row contradicts it. A row without those two members still cannot prove anything, and gets the `unverifiable` warning banner as before.',
  },
  {
    id: 'A9',
    // Kept as the assumption's stable label: it is the key path the dashboard
    // ASSUMED, it is what `service/tests/api/test_contract_assumptions.py`
    // cross-references by id, and the correction below is the point of the row.
    subject: 'proposals.evidence.observed_values',
    assumption:
      'WRONG AS ASSUMED, AND NOW CORRECTED. `evidence.observed_values` does not exist. The service puts observed values TOP-LEVEL on the conflict row (`review.py::_conflict_row`) and nested at `evidence.conflict.observed_values` (`reconciler.py::EvidencePacket.as_dict`). The conflict detail reads the row first and the packet second.',
    pinned:
      'DESIGN pins the `evidence jsonb` column, not its interior — which is exactly how a plausible key path went unanswered until the service was read.',
    failure: 'loud',
    consequence:
      'Read defensively from both places: absent from both, the conflict detail shows "—" in the Observed values column. While the key path was wrong it showed "—" on EVERY conflict, which is the loud-but-unnoticed failure this row now records.',
  },
  {
    id: 'A10',
    // Same: the label is the assumption, not the truth. The truth is below.
    subject: 'proposals.action.target_path',
    assumption:
      'STRUCTURALLY IMPOSSIBLE AS ASSUMED, AND NOW CORRECTED. `action` cannot carry a `target_path`: migration 0007 `ck_proposals_action_vocabulary` is a VALIDATED CHECK requiring `action - \'set\' = \'{}\'::jsonb` — exactly one top-level key, named `set`. The write set is read off `action.set` by `writePaths()`, which names every key the action assigns plus every member a nested assignment carries (`entities.current` nests one object, `survived`).',
    pinned:
      'DESIGN pins the `action jsonb` column, not its interior; the database pins the interior, and it pins it to `{"set": {…}}` and nothing else.',
    failure: 'loud',
    consequence:
      'While the key path was wrong, EVERY proposal rendered "evidence only — no field write" and the R24 apply control could never render for any row. Now: `{"set": {}}` is evidence-only, ≥1 assignment names every path it writes, and an action with no object-valued `set` renders an explicit "not in the committed shape" notice instead of being mistaken for evidence-only.',
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
  /**
   * A9. `observed_values jsonb`, TOP-LEVEL on the conflict row — this is the
   * "CRM says X, App DB says Y" the brief asks for, and it is the FIRST place
   * the conflict detail looks. Optional because a client may be talking to a
   * service that does not project it; the detail then falls back to
   * `evidence.conflict.observed_values` and finally to "—".
   */
  observed_values?: Record<string, unknown>
  status: ConflictStatus
  first_seen_run: string
  last_seen_run: string
}

/** One condition R24's gate evaluated — `recon.apply.GateCheck.as_dict`. */
export interface AutoApplyCheck {
  check: string
  passed: boolean
  detail: string
}

/**
 * R24's verdict on one proposal — `recon.apply.AutoApplyDecision.as_dict`.
 *
 * The service attaches this to `GET /api/proposals/{id}` and to nothing else:
 * it is a per-row read of the entity, the rollback path and the write set, so
 * it does not belong on a list page. Every field is optional to the dashboard —
 * a service that does not send it simply renders no gate panel.
 */
export interface AutoApplyVerdict {
  proposal_id: number
  allowed: boolean
  reason: string
  detail: string
  checks: AutoApplyCheck[]
}

/**
 * Which of the two writes behind `POST …/apply` the dashboard is asking for.
 *
 * `recon/api/review.py::apply_endpoint` is two code paths, not a flag inside
 * one, and the difference is WHO AUTHORISED THE WRITE:
 *
 * `'auto'`   → `?auto=true`. **R24.** `recon.apply.auto_apply` runs the gate
 *              first (sensitivity, approved case type, target on the allowlist,
 *              write set equal to the committed fix target, confidence ≥ 0.95,
 *              complete evidence, a recorded rollback path, appliable status)
 *              and answers 409 naming the condition that did not hold. The
 *              machine authorised it, or nothing happened.
 * `'manual'` → no `auto` parameter, which is the endpoint's pinned default.
 *              `recon.apply.apply_proposal`: a reviewer has approved and is
 *              authorising the write themselves. The gate does NOT run.
 *
 * They are never substituted for one another in the UI. A refused auto-apply is
 * rendered as a refusal, because falling back to the reviewer write would turn
 * R24's safety property into a successful write.
 */
export type ApplyMode = 'auto' | 'manual'

/**
 * One row of the `proposal_events` reversal ledger, exactly as
 * `review.py::_event_row` serves it.
 *
 * **`before` and `after` are not here, and that is the service's decision, not
 * an omission.** Both columns hold whole canonical records — the personal data —
 * so `_event_row` serves two sha256 digests and the field paths the write moved
 * instead. That is strictly stronger than a truncated value: the digests can be
 * compared against the apply response, the audit row and `entities.current::text`
 * itself, and none of them puts a person's record on a reviewer's screen.
 *
 * `event_id`, `txid` and `canonical_id` arrive as STRINGS, deliberately: they are
 * `bigint`/`bigint`/`uuid` columns and every JSON number in a browser is an IEEE
 * double, so a `bigint` sent as a number can come back changed.
 *
 * Every member but `event` is optional, so an older or partial build renders "—"
 * in a cell rather than throwing inside a table body.
 */
export interface ProposalEvent {
  /** `proposal_events.id`, as a string. */
  event_id?: string
  /**
   * `proposal_events.event`. Migration 0004's `CANONICAL_EVENTS` names the two
   * values that authorise a canonical mutation — `applied` and `rolled_back`.
   * Anything else renders as its raw token rather than being folded into one.
   */
  event: string
  /** Who wrote it — e.g. `system:auto-apply` vs `system:apply`. */
  actor?: string
  /** ISO-8601, server-rendered. The `ts` DEFAULT means the clock is not forgeable. */
  ts?: string
  /**
   * `pg_current_xact_id()` of the writing transaction. This is the property the
   * ledger exists for: the event, the canonical UPDATE and the status move share
   * one transaction id, which is what `KS011` checks and what makes "the write
   * was authorised" checkable from outside the database.
   */
  txid?: string
  /** The `entities` row this event authorises or reverses. */
  canonical_id?: string | null
  /**
   * sha256 of the canonical row this event OVERWROTE.
   *
   * `null` is meaningful: `_PROPOSAL_EVENTS` computes it as
   * `sha256(pe.before::text)`, and SQL propagates NULL, so a null digest means
   * no before-image was captured — i.e. that write has nothing to restore from.
   * That is the reversibility signal `reversibility()` reads.
   */
  before_digest?: string | null
  /** sha256 of the canonical row this event LEFT behind. */
  after_digest?: string | null
  /**
   * `keystone_differing_paths(before, after)` — which fields the write moved.
   *
   * A STRING, not a list: migration 0008 declares the function `RETURNS text`
   * and builds it with `string_agg(k, ', ' ORDER BY k)`. It also returns a whole
   * explanatory sentence in two degenerate cases ("(the whole value: one side is
   * not a JSON object)"), so it is rendered verbatim and never parsed or split.
   *
   * It names paths and never their contents, on purpose: the same function feeds
   * the `KS010`/`KS012` trigger messages, which are returned to clients and
   * written to the server log.
   */
  differing_paths?: string | null
}

/**
 * The `rollback` member — served on BOTH outcomes of `POST …/rollback`, which is
 * why one type covers both.
 *
 * On 200 it is `recon.apply.RollbackResult.as_dict`: the digest the apply
 * captured, the digest now in the row, and `byte_identical` — a claim asserted
 * before the transaction ends and again by `KS012` at COMMIT, so a 200 *means*
 * the bytes match.
 *
 * On the `rollback-not-on-top` 409 it is the refusal's evidence instead:
 * `on_top: false`, the two digests that disagree, and the paths they differ at.
 * A later apply is on top of this one, and reversing out of order would silently
 * discard an approved, applied, unreversed write.
 */
export interface RollbackReceipt {
  proposal_id?: number | string
  canonical_id?: string
  event_id?: number | string
  applied_before_digest?: string
  restored_digest?: string
  byte_identical?: boolean
  /** 409 only: `false` when the reversal is not on top of the stack. */
  on_top?: boolean
  /** 409 only. */
  applied_after_digest?: string
  /** 409 only. */
  current_digest?: string
  /**
   * 409 only: where the stored value and the canonical row disagree. `text`, not
   * a list — see `ProposalEvent.differing_paths`.
   */
  differing_paths?: string
}

/** PINNED column names — DESIGN §Data models `proposals`. */
export interface Proposal {
  id: string
  conflict_id: string
  fingerprint: string
  /**
   * `action jsonb`. DESIGN pins the column; the DATABASE pins the interior —
   * migration 0007's `ck_proposals_action_vocabulary` admits `{"set": {…}}` and
   * nothing else (A10). Read with `writePaths()` from src/lib/proposal.ts, and
   * rendered generically as a record besides.
   */
  action: Record<string, unknown>
  confidence: number
  /** `evidence jsonb` — rendered generically, key by key. See A9 for its interior. */
  evidence: Record<string, unknown>
  rationale: string | null
  status: ProposalStatus
  sensitive: boolean
  created_run: string
  decided_by: string | null
  decided_at: string | null
  /**
   * NOT columns of `proposals` — the JOINED conflict's `type` and `sources`,
   * named so they cannot be mistaken for columns (`review.py::_proposal_row`).
   * They exist so A8 becomes verifiable from the row: filterGuard checks the
   * `source`/`type` filters against them instead of merely warning. Optional,
   * because a service that serves the filters WITHOUT the JOIN sends neither —
   * and that is precisely the case the `unverifiable` warning still covers.
   */
  conflict_type?: ConflictType
  conflict_sources?: SourceId[]
  /**
   * R24's answer to "why is this one not applied automatically?", attached by
   * `GET /api/proposals/{id}` only. Optional and rendered only when present.
   */
  auto_apply?: AutoApplyVerdict
  /**
   * The `proposal_events` reversal ledger for this proposal, oldest first —
   * the record that proves the canonical write was authorised and can be
   * undone. Attached by `GET /api/proposals/{id}` only, like `auto_apply`: it
   * is a per-row read and does not belong on a list page.
   *
   * Optional, and its three absences are three different facts:
   * `undefined` (this service build does not send the ledger), `[]` (it sends
   * one and no canonical write has been authorised yet), and a non-array
   * (a shape the dashboard will not guess at). `ProposalDetail.tsx` renders
   * each of those distinctly rather than showing all three as "empty".
   */
  events?: ProposalEvent[]
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
  /**
   * `POST …/apply`. **`mode` is not decoration** — it selects which of the two
   * writes the service performs (see {@link ApplyMode}). It is optional only so
   * that a client written before R24's auto path existed still satisfies this
   * interface; `src/lib/httpClient.ts` treats an omitted mode as `'manual'`,
   * which is the endpoint's own default, so no caller can accidentally trigger
   * the gated path.
   */
  applyProposal(id: string, mode?: ApplyMode): Promise<Proposal>
  /**
   * `POST /api/proposals/{id}/rollback` — R24's "recorded rollback path", as an
   * endpoint. `review.py::rollback_endpoint` serves it as `apply_writer` and
   * answers with the updated proposal plus `events` and `rollback`.
   *
   * OPTIONAL, and the optionality is not hedging: a client that cannot reverse a
   * write is representable, and `useDecision` then refuses the action with a
   * named error instead of firing a request at a route that may not be there.
   *
   * The response body is NOT depended on (the same reasoning as A5): the UI
   * refetches the proposal and its ledger afterwards, and the refetched ledger
   * — now carrying both legs — is the actual proof. `null` is returned when the
   * body is not a proposal row, so a build answering with a bare
   * `RollbackResult` or a 204 costs nothing.
   */
  rollbackProposal?(id: string): Promise<Proposal | null>
  getScorecard(signal?: AbortSignal): Promise<Scorecard>
}
