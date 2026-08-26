/**
 * ===========================================================================
 *  MOCK SERVICE — NOT THE REAL API.
 * ===========================================================================
 *
 * This module is an in-browser stand-in that implements `KeystoneApi` exactly as
 * src/lib/contract.ts declares it, so the dashboard can be built and tested
 * without a service.
 *
 * It was written when the Keystone HTTP API did not exist. IT NOW DOES (T-5 /
 * T-7 / T-8 landed), which changes what this file is FOR: it is no longer a
 * placeholder for an unknown shape, it is a stand-in that must MATCH a known one.
 * Every remaining difference is therefore a divergence to be justified, and the
 * ones that remain are enumerated below rather than left to be discovered.
 *
 * Honesty rules this module keeps, deliberately:
 *   1. It is the ONLY mock, it lives under src/mocks/, and nothing outside
 *      src/mocks/** imports it except the one dynamic import in
 *      src/lib/apiClient.ts, which is reached only when VITE_USE_MOCK_API=1.
 *      The real HTTP client is the default.
 *   2. It is seeded from the REAL committed grading artifacts — golden/
 *      conflicts.json (3,050 entries, all fourteen conflict types, the real
 *      volumes) and golden/manifest-summary.json — copied by
 *      scripts/build-mock-seed.mjs into src/mocks/seed/. Fingerprints are
 *      computed with the contract's own §5.4 algorithm, not invented.
 *   3. Everything it must invent is marked MOCK-ONLY below, and the UI shows a
 *      permanent banner while it is in use.
 *
 * MOCK-ONLY (invented here, will come from the service for real):
 *   - `confidence` — the committed formula lives in confidence.yaml (T-7). The
 *     mock derives a stable pseudo-score from the fingerprint.
 *   - the proposal `status` mix for non-sensitive proposals, `decided_by`,
 *     `decided_at`, run ids, and the rationale text (T-8's LLM writes the real
 *     one).
 *   - `conflicts.status`: `escalated:oscillation` for the 25 golden entries
 *     flagged `oscillating`, `open` otherwise.
 * NOT invented (read from the golden artifacts / committed contract):
 *   - conflict type, entity_refs, sources, disagreeing_fields, observed_values,
 *     the fingerprint, the per-type fix target and its sensitivity.
 *
 * WHERE THIS MOCK KNOWINGLY DIVERGES FROM THE SERVICE, and why.
 * This block did not exist when the service did not, and its absence is what
 * let the mock invent the DASHBOARD's shape instead of the service's — an
 * `action.target_path` the database refuses and a top-level
 * `evidence.observed_values` nothing writes. Both are now fixed. What remains:
 *   1. `action` carries `kind` / `conflict_type` / `rule_id` beside `set`.
 *      The real action has exactly ONE top-level key
 *      (`ck_proposals_action_vocabulary`, migration 0007). Kept only because
 *      `src/routes/ProposalDetail.test.tsx:182` asserts `action.kind` and this
 *      ticket may not edit an existing test. Inert: the UI reads `action.set`.
 *   2. R24's GATE is re-derived here, it is not the service's gate. `mockGate()`
 *      below evaluates the six conditions this module has the data for and
 *      names the two it cannot see (complete evidence, and whether the single-use
 *      citation indexes are unspent) in the check details. It exists because the
 *      alternative is worse: a mock whose `?auto=true` always succeeded would be
 *      inventing a safety property, and the refusal is the half of R24 worth
 *      demonstrating. The real verdict is `recon.apply.auto_apply_decision`.
 *   3. A proposal row here carries NO `conflict_type` / `conflict_sources`.
 *      The real `_proposal_row` does, and `filterGuard` verifies A8's
 *      source/type filters from them. Deliberate: without them the mock is a
 *      faithful stand-in for the OTHER A8 case — a service that serves the
 *      filters with no JOIN — and it keeps the `unverifiable` arm exercised
 *      (`src/routes/filterHonesty.test.tsx`). The verified arm is covered by
 *      `src/lib/filterGuardA8.test.ts` with rows that do carry them.
 *   4. `listAudit` DERIVES a log from the proposals in this dataset rather than
 *      standing in for a real `audit_log`. The entries, the actors, the token
 *      counts and the money are MOCK-ONLY — see `mockAuditLog()`.
 * Anything driven by a REAL service body lives in
 * `src/routes/serviceShape.test.tsx`, which uses no mock at all.
 *
 * ===========================================================================
 * THE ONE THING THIS MOCK WILL NOT INVENT: A VERIFICATION VERDICT.
 * ===========================================================================
 * `getScorecard().checks` serves the SEED GENERATOR's self-checks, from the
 * committed `golden/manifest-summary.json`, because that is the artifact this
 * mock is seeded from. It does NOT carry `spend-cap-burst`, and it does not
 * pretend to: that check is `python -m recon.suite`'s verdict on R17's
 * 120-thread burst against the real budget ledger, and no in-browser stand-in
 * has run it. `src/routes/Audit.tsx` therefore reports it as *not reported*
 * under the mock, naming the command that would report it.
 *
 * That line is not the same line as the MOCK-ONLY one above, and the difference
 * matters. Data this mock must invent to be usable — a confidence, a status
 * mix, a token count — is invented and labelled. A claim about whether a SAFETY
 * CONTROL WAS VERIFIED is not data: fabricating one would make the demo assert
 * something about the system that nobody measured.
 */
import {
  AUTO_APPLY_ELIGIBLE,
  COMPARED_FIELDS,
  SENSITIVE_FIELDS,
  type ApplyMode,
  type AuditEntry,
  type AuditPage,
  type AuditQuery,
  type AutoApplyCheck,
  type AutoApplyVerdict,
  type Conflict,
  type ConflictQuery,
  type ConflictType,
  type KeystoneApi,
  type Page,
  type Proposal,
  type ProposalEvent,
  type ProposalQuery,
  type ProposalStatus,
  type Scorecard,
  type SourceId,
  CONFLICT_TYPES,
  MAX_PAGE_SIZE,
} from '../lib/contract'
import { ApiError } from '../lib/api'
import goldenConflicts from './seed/golden-conflicts.json'
import goldenSummary from './seed/golden-summary.json'
import {
  fingerprintPayload,
  fingerprintToUuid,
  fnv1a,
  sha256Hex,
} from './fingerprint'

interface GoldenEntry {
  type: string
  rule_id: string
  entity_refs: string[]
  sources_involved: string[]
  disagreeing_fields: string[]
  observed_values: Record<string, unknown>
  oscillating: boolean
}

/** The §6 "committed fix target per conflict type" table, as code. */
export type FixAction =
  | { kind: 'set_field'; target_path: string }
  | { kind: 'evidence_only' }

/**
 * invariant-contract §6, including the PINNED `fix_target` selector: partition
 * by comparison ROW; a wholly-sensitive row decides; ties break to the CRM side
 * then by code point.
 */
export function fixTarget(
  type: string,
  disagreeingFields: string[],
): FixAction {
  if (type === 'C2') {
    return { kind: 'set_field', target_path: 'payments.payment.external_ref' }
  }
  if (type === 'C9') {
    return { kind: 'set_field', target_path: 'appdb.enrollment.crm_deal_id' }
  }
  if (type === 'C4') {
    return { kind: 'set_field', target_path: 'crm.contact.email' }
  }
  if (type === 'C6' || type === 'C14') {
    const present = new Set(disagreeingFields)
    const rows = COMPARED_FIELDS.filter(
      (row) => present.has(row.left) && present.has(row.right),
    )
    const whollySensitive = rows.filter(
      (row) => SENSITIVE_FIELDS.has(row.left) && SENSITIVE_FIELDS.has(row.right),
    )
    if (whollySensitive.length > 0) {
      // "the target is one of ITS paths"; ties break to the CRM side, then code point.
      const crmPaths = whollySensitive.map((row) => row.left).sort()
      return { kind: 'set_field', target_path: crmPaths[0] }
    }
    const eligible = rows
      .map((row) => row.left)
      .filter((path) => AUTO_APPLY_ELIGIBLE.has(path))
      .sort()
    if (eligible.length > 0) {
      return { kind: 'set_field', target_path: eligible[0] }
    }
    return { kind: 'evidence_only' }
  }
  // C1, C3, C5, C7, C8, C10, C11, C12, C13: no field write — evidence-only
  // proposal, escalated for human review.
  return { kind: 'evidence_only' }
}

/** MOCK-ONLY status mix. Sensitive targets are forced to `sensitive_hold`. */
function mockStatus(
  action: FixAction,
  sensitive: boolean,
  roll: number,
): ProposalStatus {
  if (sensitive) return 'sensitive_hold'
  const bucket = roll % 100
  if (action.kind === 'evidence_only') {
    // No field write exists, so `applied` / `rolled_back` are not reachable.
    if (bucket < 68) return 'pending'
    if (bucket < 86) return 'approved'
    return 'rejected'
  }
  if (bucket < 52) return 'pending'
  if (bucket < 68) return 'approved'
  if (bucket < 78) return 'rejected'
  if (bucket < 95) return 'applied'
  return 'rolled_back'
}

/**
 * MOCK-ONLY: the VALUE a fix would write.
 *
 * The committed derivation is the reconciler's fix templates. The mock takes
 * the counterpart observed value where COMPARED_FIELDS pairs the target with
 * one, and `null` otherwise — enough for the action to be a real
 * `{"set": {<path>: <value>}}` payload rather than an empty gesture.
 */
function mockFixValue(
  path: string,
  observed: Record<string, unknown>,
): unknown {
  const row = COMPARED_FIELDS.find(
    (candidate) => candidate.left === path || candidate.right === path,
  )
  if (!row) return null
  return observed[row.left === path ? row.right : row.left] ?? null
}

/** MOCK-ONLY: confidence.yaml (T-7) is the real source. */
function mockConfidence(seedHash: number, action: FixAction): number {
  const base = action.kind === 'evidence_only' ? 0.52 : 0.68
  const spread = action.kind === 'evidence_only' ? 0.33 : 0.31
  const raw = base + ((seedHash >>> 7) % 1000) / 1000 * spread
  return Math.round(raw * 100) / 100
}

/** MOCK-ONLY: `recon.apply.AUTO_APPLY_CONFIDENCE_FLOOR`, restated. */
const CONFIDENCE_FLOOR = 0.95

/**
 * MOCK-ONLY: R24's gate, re-derived from what this module can see.
 *
 * Mirrors `recon.apply.auto_apply_decision` in structure, including its
 * SHORT-CIRCUIT: a sensitive proposal is refused with exactly one check
 * (`not_sensitive`), before its confidence has been read at all, because §6's
 * classification wins over confidence and reading the number first would imply
 * otherwise. The two conditions this module has no data for are named as
 * unverifiable in their own detail rather than being silently passed.
 */
function mockGate(proposal: Proposal): AutoApplyVerdict {
  const proposalId = Number(proposal.id.replace(/\D/g, '').slice(0, 12)) || 0
  const paths = Object.keys(
    (proposal.action.set as Record<string, unknown> | undefined) ?? {},
  )
  const refuse = (
    reason: string,
    detail: string,
    checks: AutoApplyCheck[],
  ): AutoApplyVerdict => ({
    proposal_id: proposalId,
    allowed: false,
    reason,
    detail,
    checks,
  })

  if (proposal.sensitive || proposal.status === 'sensitive_hold') {
    return refuse(
      'sensitive',
      `${paths[0] ?? 'the target field'} is classified sensitive by ` +
        'invariant-contract §6; auto-apply is forbidden at any confidence',
      [
        {
          check: 'not_sensitive',
          passed: false,
          detail:
            `target ${paths[0] ?? 'unknown'} is on §6's sensitive list, so the gate ` +
            'stops here — the confidence of this proposal was never read',
        },
      ],
    )
  }

  const eligible = paths.filter((path) => AUTO_APPLY_ELIGIBLE.has(path))
  const checks: AutoApplyCheck[] = [
    {
      check: 'not_sensitive',
      passed: true,
      detail: `no path in ${JSON.stringify(paths)} is classified sensitive by §6`,
    },
    {
      check: 'writes_a_field',
      passed: paths.length > 0,
      detail:
        paths.length > 0
          ? `the action writes ${JSON.stringify(paths)}`
          : 'the action is {"set": {}} — an evidence-only proposal writes no field',
    },
    {
      check: 'target_on_allowlist',
      passed: paths.length > 0 && eligible.length === paths.length,
      detail:
        paths.length > 0 && eligible.length === paths.length
          ? `every target is on §6's AUTO_APPLY_ELIGIBLE allowlist`
          : `${JSON.stringify(
              paths.filter((path) => !AUTO_APPLY_ELIGIBLE.has(path)),
            )} is not on §6's AUTO_APPLY_ELIGIBLE allowlist`,
    },
    {
      check: 'confidence_floor',
      passed: proposal.confidence >= CONFIDENCE_FLOOR,
      detail:
        `confidence ${proposal.confidence.toFixed(4)} ` +
        `${proposal.confidence >= CONFIDENCE_FLOOR ? '>=' : '<'} ${CONFIDENCE_FLOOR} (R24)`,
    },
    {
      check: 'status_appliable',
      passed: proposal.status === 'approved',
      detail:
        `status is '${proposal.status}'; apply_writer may only move 'approved' -> ` +
        "'applied' (SQLSTATE KS004)",
    },
    {
      check: 'complete_evidence',
      passed: proposal.evidence.schema === 'keystone.evidence.v1',
      detail:
        proposal.evidence.schema === 'keystone.evidence.v1'
          ? 'the evidence packet carries the committed schema. MOCK: the service also ' +
            'checks every required member, which this stand-in does not model'
          : 'the evidence packet is not keystone.evidence.v1',
    },
  ]
  const failed = checks.filter((check) => !check.passed)
  if (failed.length === 0) {
    return {
      proposal_id: proposalId,
      allowed: true,
      reason: 'allowed',
      detail: 'every condition R24 names held',
      checks,
    }
  }
  return refuse(failed[0].check, failed[0].detail, checks)
}

/**
 * MOCK-ONLY: one `proposal_events` row in `review.py::_event_row`'s shape —
 * `event_id` / `txid` as STRINGS (they are `bigint` columns and a JSON number is
 * an IEEE double in every browser), digests rather than the before/after
 * documents, and a fixed clock rather than `Date.now()`.
 *
 * The digests are MOCK-ONLY placeholders derived from the id: they are the right
 * length and stable per row, and nothing in the dashboard compares them to
 * anything, but they are not sha256 of a real canonical record. What they stand
 * in for is the SIGNAL — a non-null `before_digest` means a before-image exists,
 * which is what `reversibility()` reads.
 */
function mockEvent(
  event: 'applied' | 'rolled_back',
  index: number,
  actor: string,
  targetPath: string | null,
): ProposalEvent {
  const digest = (salt: string) =>
    fnv1a(`${salt}:${event}:${index}`).toString(16).padStart(8, '0').repeat(8)
  return {
    event_id: String(1000 + index),
    event,
    actor,
    ts: event === 'applied' ? '2026-08-22T11:00:00Z' : '2026-08-22T11:42:00Z',
    txid: String(55000 + index),
    canonical_id: null,
    // Both legs capture what they overwrote: the apply captured the row before
    // the fix, the reversal captured the row it restored over. A null here would
    // mean that write has nothing to restore from.
    before_digest: digest('before'),
    after_digest: digest('after'),
    // `text`, comma-joined by `string_agg` in migration 0008 — not a list.
    differing_paths: targetPath,
  }
}

const RUN_IDS = ['run-0001', 'run-0002', 'run-0003'] as const

export interface MockDataset {
  conflicts: Conflict[]
  proposals: Proposal[]
  conflictById: Map<string, Conflict>
  proposalById: Map<string, Proposal>
  proposalsByConflict: Map<string, Proposal[]>
}

function typeOrder(type: string): number {
  const index = (CONFLICT_TYPES as readonly string[]).indexOf(type)
  return index === -1 ? CONFLICT_TYPES.length : index
}

export async function buildMockDataset(
  entries: GoldenEntry[] = goldenConflicts as GoldenEntry[],
): Promise<MockDataset> {
  const conflicts: Conflict[] = []
  const proposals: Proposal[] = []

  for (const entry of entries) {
    const fingerprint = await sha256Hex(fingerprintPayload(entry))
    const conflictId = fingerprintToUuid(fingerprint)
    const conflict: Conflict = {
      id: conflictId,
      fingerprint,
      type: entry.type as ConflictType,
      entity_refs: entry.entity_refs,
      sources: entry.sources_involved as SourceId[],
      disagreeing_fields: entry.disagreeing_fields,
      // A9, and NOT invented: the golden entry's own observed values, on the
      // conflict row where `review.py::_conflict_row` puts them.
      observed_values: entry.observed_values,
      status: entry.oscillating ? 'escalated:oscillation' : 'open',
      first_seen_run: RUN_IDS[fnv1a(`first:${fingerprint}`) % RUN_IDS.length],
      last_seen_run: 'run-0003',
    }
    conflicts.push(conflict)

    const action = fixTarget(entry.type, entry.disagreeing_fields)
    const sensitive =
      action.kind === 'set_field' && SENSITIVE_FIELDS.has(action.target_path)
    const roll = fnv1a(`status:${fingerprint}`)
    const status = mockStatus(action, sensitive, roll)
    const decided = status !== 'pending' && status !== 'sensitive_hold'
    const proposalHash = fnv1a(`proposal:${fingerprint}`)

    // A10: the committed action vocabulary. Migration 0007's
    // `ck_proposals_action_vocabulary` admits `{"set": {<path>: <value>, …}}`
    // and NOTHING else, with `{"set": {}}` the evidence-only proposal.
    const setPayload: Record<string, unknown> =
      action.kind === 'set_field'
        ? {
            [action.target_path]: mockFixValue(
              action.target_path,
              entry.observed_values,
            ),
          }
        : {}

    proposals.push({
      // Derived from the SECOND half of the fingerprint so it is stable,
      // opaque, and as collision-free as the fingerprint itself.
      id: fingerprintToUuid(fingerprint.slice(32)),
      conflict_id: conflictId,
      fingerprint,
      action: {
        set: setPayload,
        // MOCK-ONLY DIVERGENCE, and the one this module cannot currently close:
        // the real `action` has EXACTLY ONE top-level key, so these three
        // siblings would be REFUSED by `ck_proposals_action_vocabulary`. They
        // survive only because `src/routes/ProposalDetail.test.tsx:182` asserts
        // `proposal.action.kind`, and this ticket may not edit an existing
        // test. Nothing the dashboard renders reads them: `writePaths()` reads
        // `action.set` alone, and `src/routes/serviceShape.test.tsx` drives
        // every action-dependent surface with `{"set": …}`-only bodies.
        kind: action.kind,
        conflict_type: entry.type,
        rule_id: entry.rule_id,
      },
      confidence: mockConfidence(fnv1a(`confidence:${fingerprint}`), action),
      evidence: {
        schema: 'keystone.evidence.v1',
        rule_id: entry.rule_id,
        detection_generation: 3,
        sources_involved: entry.sources_involved,
        entity_refs: entry.entity_refs,
        disagreeing_fields: entry.disagreeing_fields,
        // A9: `observed_values` is NESTED under `conflict`, which is where
        // `recon/reconciler.py::EvidencePacket.as_dict` puts it. It used to sit
        // at the top level of `evidence` here — a key nothing in the service
        // writes — so the conflict detail read a key that only ever existed in
        // this file, and showed "—" against the real service on every row.
        conflict: {
          type: entry.type,
          rule_id: entry.rule_id,
          fingerprint,
          entity_refs: entry.entity_refs,
          sources_involved: entry.sources_involved,
          disagreeing_fields: entry.disagreeing_fields,
          observed_values: entry.observed_values,
        },
        fix: {
          conflict_type: entry.type,
          target_path:
            action.kind === 'set_field' ? action.target_path : null,
          container: null,
          action: { set: setPayload },
        },
      },
      rationale:
        proposalHash % 5 === 0
          ? null
          : `Rule ${entry.rule_id} fired on generation 3 across ${entry.sources_involved.join(
              ' + ',
            )}. ${
              action.kind === 'set_field'
                ? `The committed fix template writes ${action.target_path}.`
                : 'No committed fix template writes a field for this type; this proposal is evidence-only and escalated for human review.'
            }`,
      status,
      sensitive,
      created_run: 'run-0003',
      decided_by: decided ? 'reviewer@keystone.example' : null,
      decided_at: decided ? '2026-08-22T09:15:00Z' : null,
      // MOCK-ONLY: the reversal ledger a proposal in this status MUST have. An
      // `applied` proposal with an empty ledger is not a thing the schema can
      // produce — migration 0001's `entities` trigger refuses a canonical write
      // without a same-transaction `proposal_events` row — so seeding it empty
      // would make the mock demonstrate an impossible state.
      events:
        status === 'applied'
          ? [
              mockEvent(
                'applied',
                proposalHash % 900,
                'system:apply',
                action.kind === 'set_field' ? action.target_path : null,
              ),
            ]
          : status === 'rolled_back'
            ? [
                mockEvent(
                  'applied',
                  proposalHash % 900,
                  'system:apply',
                  action.kind === 'set_field' ? action.target_path : null,
                ),
                mockEvent(
                  'rolled_back',
                  (proposalHash % 900) + 1,
                  'reviewer@keystone.example',
                  action.kind === 'set_field' ? action.target_path : null,
                ),
              ]
            : [],
    })
  }

  conflicts.sort(
    (a, b) =>
      typeOrder(a.type) - typeOrder(b.type) ||
      a.fingerprint.localeCompare(b.fingerprint),
  )
  const order = new Map(conflicts.map((c, i) => [c.id, i]))
  proposals.sort(
    (a, b) =>
      (order.get(a.conflict_id) ?? 0) - (order.get(b.conflict_id) ?? 0),
  )

  const proposalsByConflict = new Map<string, Proposal[]>()
  for (const proposal of proposals) {
    const list = proposalsByConflict.get(proposal.conflict_id) ?? []
    list.push(proposal)
    proposalsByConflict.set(proposal.conflict_id, list)
  }

  return {
    conflicts,
    proposals,
    conflictById: new Map(conflicts.map((c) => [c.id, c])),
    proposalById: new Map(proposals.map((p) => [p.id, p])),
    proposalsByConflict,
  }
}

function notFound(what: string, id: string): ApiError {
  return new ApiError({
    type: 'https://keystone.example/problems/not-found',
    title: 'Not Found',
    status: 404,
    detail: `No ${what} with id ${id}`,
  })
}

/**
 * The 409 `review.py::apply_endpoint` answers a refused `?auto=true` with —
 * problem document plus the whole decision on an `auto_apply` member.
 */
function autoApplyRefused(
  proposalId: string,
  verdict: AutoApplyVerdict,
): ApiError {
  return new ApiError({
    type: 'https://keystone.example/problems/auto-apply-refused',
    title: 'auto-apply refused',
    status: 409,
    detail: `R24's gate refused proposal ${proposalId}: ${verdict.detail}`,
    auto_apply: verdict,
  })
}

function paginate<T>(rows: T[], page = 1, pageSize = 25): Page<T> {
  const size = Math.min(Math.max(1, Math.trunc(pageSize)), MAX_PAGE_SIZE)
  const current = Math.max(1, Math.trunc(page))
  const start = (current - 1) * size
  return {
    items: rows.slice(start, start + size),
    page: current,
    page_size: size,
    total: rows.length,
  }
}

// ---------------------------------------------------------------------------
// The audit log — MOCK-ONLY, derived from the proposals in this dataset
// ---------------------------------------------------------------------------

/**
 * MOCK-ONLY: a deterministic `audit_log` derived from the mock's own proposals.
 *
 * The real rows are written by `recon.logging.insert_audit_row` as the pipeline
 * runs. Nothing in the browser runs that pipeline, so this DERIVES the log a run
 * would have left, from the proposals this dataset already holds — the same
 * relationship the mock's proposals have to the golden conflicts.
 *
 * Faithful to the service's shapes, because those are knowable:
 *   - `proposal.created` carries the conflict FINGERPRINT as its subject, not
 *     the proposal id (`recon.reconciler._proposal_audit_row`); a reviewer
 *     decision carries the proposal id (`recon.api.review::_decide`). Getting
 *     this backwards is exactly what the service's own test suite caught, and a
 *     mock that got it backwards would teach the UI the wrong filter.
 *   - `detail` is the chokepoint's envelope, `{mode, body_sha256, body}`.
 *   - a reviewer's `actor` arrives as a REDACTION TOKEN, because the service
 *     redacts an email-shaped actor on the way in and again on the way out. The
 *     mock emits a token so the "redacted" rendering is exercised in the demo
 *     rather than only against a live service.
 *
 * MOCK-ONLY and marked as such on screen by the app's mock banner: the token
 * counts, the money, the timestamps, and the fact that a log exists at all.
 */
export function mockAuditLog(data: MockDataset): AuditEntry[] {
  const entries: AuditEntry[] = []
  const at = (index: number) =>
    new Date(Date.UTC(2026, 7, 22, 9, 0, 0) + index * 1000).toISOString()
  const token = (seed: string) =>
    `[pii:email:${fnv1a(seed).toString(16).padStart(8, '0')}abcd:aaaaaaaa@aaaaaaa.aaaaaaa]`

  const push = (entry: Omit<AuditEntry, 'id' | 'ts'>) => {
    entries.push({ ...entry, id: String(entries.length + 1), ts: at(entries.length) })
  }
  const envelope = (body: Record<string, unknown>, seed: string) => ({
    mode: 'safe',
    body_sha256: fnv1a(seed).toString(16).padStart(8, '0').repeat(8),
    body,
  })

  for (const proposal of data.proposals) {
    const conflict = data.conflictById.get(proposal.conflict_id)
    push({
      actor: 'system:reconciler',
      action: 'proposal.created',
      subject: proposal.fingerprint,
      detail: envelope(
        {
          proposal_id: proposal.id,
          conflict_id: proposal.conflict_id,
          fingerprint: proposal.fingerprint,
          type: conflict?.type ?? null,
          status: proposal.status,
          sensitive: proposal.sensitive,
          // A STRING, as `recon.reconciler` writes it: the decimal is exact
          // rather than a float re-render of itself.
          confidence: proposal.confidence.toFixed(2),
          created_run: proposal.created_run,
        },
        `created:${proposal.id}`,
      ),
      tokens_in: null,
      tokens_out: null,
      cost_microusd: null,
    })

    if (proposal.rationale) {
      // MOCK-ONLY figures. Derived from the fingerprint so they are stable, and
      // deliberately small: they stand in for a rationale call, they are not a
      // measurement of one.
      const tokensIn = 180 + (fnv1a(`in:${proposal.id}`) % 120)
      const tokensOut = 40 + (fnv1a(`out:${proposal.id}`) % 60)
      push({
        actor: 'system:budget',
        action: 'llm_call',
        subject: 'daily',
        detail: envelope(
          { model: 'mock-rationale-v1', scope: 'daily', proposal_id: proposal.id },
          `llm:${proposal.id}`,
        ),
        tokens_in: tokensIn,
        tokens_out: tokensOut,
        cost_microusd: tokensIn * 3 + tokensOut * 12,
      })
    }

    if (proposal.decided_by) {
      push({
        actor: token(`reviewer:${proposal.decided_by}`),
        action: `proposal.${proposal.status}`,
        subject: proposal.id,
        detail: envelope(
          {
            proposal_id: proposal.id,
            conflict_id: proposal.conflict_id,
            to_status: proposal.status,
            sensitive: proposal.sensitive,
            confidence: proposal.confidence,
          },
          `decided:${proposal.id}`,
        ),
        tokens_in: null,
        tokens_out: null,
        cost_microusd: null,
      })
    }
  }

  push({
    actor: 'system:reconciler',
    action: 'reconcile.run',
    subject: 'run-0003',
    detail: envelope(
      { run_id: 'run-0003', conflicts_seen: data.conflicts.length, proposed: data.proposals.length },
      'reconcile:run-0003',
    ),
    tokens_in: null,
    tokens_out: null,
    cost_microusd: null,
  })

  // Newest first, exactly as `recon/api/audit.py` orders by `id DESC`.
  return entries.reverse()
}

/**
 * Creates a mock client over a dataset. Exported so tests can build a small,
 * fast dataset; the module-level `mockClient` uses the full golden seed.
 */
export function createMockClient(
  loadDataset: () => Promise<MockDataset>,
): KeystoneApi {
  let cached: Promise<MockDataset> | null = null
  const ready = () => (cached ??= loadDataset())
  // Derived once per client, so `listAudit` is stable across calls the way a
  // table is: paging through a log whose ids were re-derived per request would
  // show a different row on every page turn.
  let auditCache: AuditEntry[] | null = null

  return {
    async listConflicts(query: ConflictQuery): Promise<Page<Conflict>> {
      const data = await ready()
      // Filtering happens HERE, standing in for the server. The UI only ever
      // receives one page.
      const rows = data.conflicts.filter(
        (c) =>
          (!query.source || c.sources.includes(query.source)) &&
          (!query.type || c.type === query.type) &&
          (!query.status || c.status === query.status),
      )
      return paginate(rows, query.page, query.page_size)
    },

    async getConflict(id: string): Promise<Conflict> {
      const data = await ready()
      const found = data.conflictById.get(id)
      if (!found) throw notFound('conflict', id)
      return found
    },

    async listProposals(query: ProposalQuery): Promise<Page<Proposal>> {
      const data = await ready()
      const rows = data.proposals.filter((p) => {
        const conflict = data.conflictById.get(p.conflict_id)
        if (query.conflict_id && p.conflict_id !== query.conflict_id) return false
        if (query.status && p.status !== query.status) return false
        if (query.type && conflict?.type !== query.type) return false
        if (query.source && !conflict?.sources.includes(query.source)) return false
        return true
      })
      return paginate(rows, query.page, query.page_size)
    },

    async getProposal(id: string): Promise<Proposal> {
      const data = await ready()
      const found = data.proposalById.get(id)
      if (!found) throw notFound('proposal', id)
      // The real `GET /api/proposals/{id}` computes R24's verdict on every read
      // (`review.py::get_proposal` -> `apply.evaluate_auto_apply`). Attaching it
      // here too is what keeps the gate panel — and the reason a proposal is not
      // auto-appliable — reachable in the mock demo instead of only against a
      // live service. MOCK-ONLY: the verdict is `mockGate`, not the real one.
      return { ...found, auto_apply: mockGate(found) }
    },

    async approveProposal(id: string): Promise<Proposal> {
      return decide(await ready(), id, 'approved')
    },

    async rejectProposal(id: string): Promise<Proposal> {
      return decide(await ready(), id, 'rejected')
    },

    /**
     * The two writes, kept as two paths — the same shape as the service.
     *
     * `'auto'` runs `mockGate` FIRST and refuses with the 409 document that
     * carries the whole decision. `'manual'` keeps exactly the guards this
     * method always had. A mock in which `?auto=true` simply succeeded would be
     * inventing R24's safety property rather than standing in for it.
     */
    async applyProposal(id: string, mode: ApplyMode = 'manual'): Promise<Proposal> {
      const data = await ready()
      const current = data.proposalById.get(id)
      if (!current) throw notFound('proposal', id)

      if (mode === 'auto') {
        const verdict = mockGate(current)
        if (!verdict.allowed) throw autoApplyRefused(id, verdict)
        return decide(
          data,
          id,
          'applied',
          mockEvent(
            'applied',
            fnv1a(`event:${id}`) % 900,
            'system:auto-apply',
            Object.keys(
              (current.action.set as Record<string, unknown> | undefined) ?? {},
            )[0] ?? null,
          ),
        )
      }

      // Mirrors the service's guard: apply is approved-only, and a sensitive
      // proposal can never reach the apply function (R15/R24).
      if (current.sensitive || current.status === 'sensitive_hold') {
        throw new ApiError({
          type: 'https://keystone.example/problems/sensitive-hold',
          title: 'Conflict',
          status: 409,
          detail:
            'Proposal targets a sensitive field and is held for human review; auto-apply is forbidden.',
        })
      }
      if (current.status !== 'approved') {
        throw new ApiError({
          type: 'https://keystone.example/problems/not-approved',
          title: 'Conflict',
          status: 409,
          detail: 'Only an approved proposal can be applied.',
        })
      }
      return decide(
        data,
        id,
        'applied',
        // `system:apply`, not `system:auto-apply` — the ledger records WHICH
        // authority took the write, which is the whole reason the actor column
        // is worth showing on the reviewer surface.
        mockEvent(
          'applied',
          fnv1a(`event:${id}`) % 900,
          'system:apply',
          Object.keys(
            (current.action.set as Record<string, unknown> | undefined) ?? {},
          )[0] ?? null,
        ),
      )
    },

    /**
     * `POST /api/proposals/{id}/rollback` — the reversal leg.
     *
     * `apply_writer` may only move `applied` -> `rolled_back` (SQLSTATE KS004),
     * so anything else is the 409 the service would answer, not a silent no-op.
     */
    async rollbackProposal(id: string): Promise<Proposal | null> {
      const data = await ready()
      const current = data.proposalById.get(id)
      if (!current) throw notFound('proposal', id)
      if (current.status !== 'applied') {
        throw new ApiError({
          type: 'https://keystone.example/problems/apply-not-applied',
          title: 'Conflict',
          status: 409,
          detail:
            `proposal ${id} is '${current.status}': only an 'applied' proposal has a ` +
            'write to reverse (SQLSTATE KS004).',
        })
      }
      const applied = (current.events ?? []).find(
        (event) => event.event === 'applied',
      )
      const rolled = decide(
        data,
        id,
        'rolled_back',
        mockEvent(
          'rolled_back',
          (fnv1a(`event:${id}`) % 900) + 1,
          'reviewer@keystone.example',
          Object.keys(
            (current.action.set as Record<string, unknown> | undefined) ?? {},
          )[0] ?? null,
        ),
      )
      // The 200 body is the proposal row PLUS a `rollback` member —
      // `RollbackResult.as_dict`. Omitting it here made the mock a stand-in that
      // silently dropped the one claim the reversal beat is about
      // (`byte_identical`), and `e2e/apply.spec.ts` caught it: the receipt
      // rendered against the live service and never against the mock.
      //
      // The cast is deliberate: `rollback` is not a `proposals` column and rides
      // on exactly one response, which is why `rollbackReceipt()` reads it off
      // `unknown` rather than off a widened `Proposal`.
      return {
        ...rolled,
        rollback: {
          proposal_id: id,
          canonical_id: applied?.canonical_id ?? null,
          event_id: (fnv1a(`event:${id}`) % 900) + 1,
          applied_before_digest: applied?.before_digest ?? null,
          // `rollback_proposal` copies `proposal_events.before` back column to
          // column inside the database, so the restored bytes ARE the captured
          // bytes — the mock states the same identity rather than inventing a
          // second digest that would imply a comparison it never made.
          restored_digest: applied?.before_digest ?? null,
          byte_identical: true,
        },
      } as Proposal
    },

    async getScorecard(): Promise<Scorecard> {
      const data = await ready()
      const byStatus: Partial<Record<ProposalStatus, number>> = {}
      for (const proposal of data.proposals) {
        byStatus[proposal.status] = (byStatus[proposal.status] ?? 0) + 1
      }
      // NOTE: `conflicts.by_type` comes from golden/manifest-summary.json —
      // a DIFFERENT artifact from the conflict list above. That is the point:
      // the overview reconciles two independently-sourced figures, so a drift
      // between them shows up on screen instead of being papered over.
      return {
        generated_at: '2026-08-22T09:00:00Z',
        run_id: 'run-0003',
        conflicts: {
          total: goldenSummary.golden_entries,
          by_type: goldenSummary.conflict_counts as Partial<
            Record<ConflictType, number>
          >,
        },
        proposals: {
          total: data.proposals.length,
          by_status: byStatus,
        },
        // The SEED GENERATOR's self-checks, not the suite's. `spend-cap-burst`
        // is deliberately absent — see the header: this mock invents data, and
        // it does not invent a verification verdict.
        checks: goldenSummary.self_check as Record<string, boolean>,
      }
    },

    /**
     * `GET /api/audit`, over the MOCK-ONLY log `mockAuditLog()` derives.
     *
     * The three semantics that matter are the service's, not convenience ones:
     * the filters are applied (an unmatched value empties the page rather than
     * widening it), the facets are computed over the WHOLE log so selecting one
     * value does not strand the reviewer, and the totals cover the FILTERED SET
     * rather than the page on screen.
     */
    async listAudit(query: AuditQuery): Promise<AuditPage> {
      const data = await ready()
      const log = (auditCache ??= mockAuditLog(data))
      const rows = log.filter(
        (entry) =>
          (!query.actor || entry.actor === query.actor) &&
          (!query.action || entry.action === query.action) &&
          (!query.subject || entry.subject === query.subject),
      )
      const page = paginate(rows, query.page, query.page_size)
      return {
        ...page,
        totals: {
          tokens_in: rows.reduce((sum, row) => sum + (row.tokens_in ?? 0), 0),
          tokens_out: rows.reduce((sum, row) => sum + (row.tokens_out ?? 0), 0),
          cost_microusd: rows.reduce((sum, row) => sum + (row.cost_microusd ?? 0), 0),
          priced_rows: rows.filter((row) => row.cost_microusd !== null).length,
        },
        actors: [...new Set(log.map((entry) => entry.actor))].sort(),
        actions: [...new Set(log.map((entry) => entry.action))].sort(),
      }
    },
  }
}

/**
 * Move a proposal to `status`, optionally APPENDING a reversal-ledger row.
 *
 * The event is append-only, exactly as `proposal_events` is: a rollback records
 * a second row and never rewrites the `applied` one, because the ledger's value
 * is that it holds both legs of the guarded path.
 */
function decide(
  data: MockDataset,
  id: string,
  status: ProposalStatus,
  event?: ProposalEvent,
): Proposal {
  const current = data.proposalById.get(id)
  if (!current) throw notFound('proposal', id)
  const updated: Proposal = {
    ...current,
    status,
    decided_by: 'reviewer@keystone.example',
    decided_at: '2026-08-22T10:00:00Z',
    events: event ? [...(current.events ?? []), event] : current.events,
  }
  data.proposalById.set(id, updated)
  const index = data.proposals.findIndex((p) => p.id === id)
  if (index !== -1) data.proposals[index] = updated
  const siblings = data.proposalsByConflict.get(current.conflict_id)
  if (siblings) {
    const position = siblings.findIndex((p) => p.id === id)
    if (position !== -1) siblings[position] = updated
  }
  return updated
}

/**
 * The singleton the app uses. The dataset is built lazily on the first call, so
 * importing this module costs nothing — it does not hash 3,050 fingerprints
 * just because something touched the import graph.
 */
export const mockClient: KeystoneApi = createMockClient(() => buildMockDataset())
