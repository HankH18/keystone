/**
 * ===========================================================================
 *  MOCK SERVICE — NOT THE REAL API.
 * ===========================================================================
 *
 * The Keystone HTTP API does not exist yet (T-5 / T-7 / T-8 build it). This
 * module is an in-browser stand-in that implements `KeystoneApi` exactly as
 * src/lib/contract.ts declares it, so the dashboard can be built and tested
 * against the pinned contract before the service lands.
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
 */
import {
  AUTO_APPLY_ELIGIBLE,
  COMPARED_FIELDS,
  SENSITIVE_FIELDS,
  type Conflict,
  type ConflictQuery,
  type ConflictType,
  type KeystoneApi,
  type Page,
  type Proposal,
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

/** MOCK-ONLY: confidence.yaml (T-7) is the real source. */
function mockConfidence(seedHash: number, action: FixAction): number {
  const base = action.kind === 'evidence_only' ? 0.52 : 0.68
  const spread = action.kind === 'evidence_only' ? 0.33 : 0.31
  const raw = base + ((seedHash >>> 7) % 1000) / 1000 * spread
  return Math.round(raw * 100) / 100
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

    proposals.push({
      // Derived from the SECOND half of the fingerprint so it is stable,
      // opaque, and as collision-free as the fingerprint itself.
      id: fingerprintToUuid(fingerprint.slice(32)),
      conflict_id: conflictId,
      fingerprint,
      action: {
        kind: action.kind,
        ...(action.kind === 'set_field'
          ? { target_path: action.target_path }
          : {}),
        conflict_type: entry.type,
        rule_id: entry.rule_id,
      },
      confidence: mockConfidence(fnv1a(`confidence:${fingerprint}`), action),
      evidence: {
        rule_id: entry.rule_id,
        detection_generation: 3,
        sources_involved: entry.sources_involved,
        entity_refs: entry.entity_refs,
        disagreeing_fields: entry.disagreeing_fields,
        observed_values: entry.observed_values,
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

/**
 * Creates a mock client over a dataset. Exported so tests can build a small,
 * fast dataset; the module-level `mockClient` uses the full golden seed.
 */
export function createMockClient(
  loadDataset: () => Promise<MockDataset>,
): KeystoneApi {
  let cached: Promise<MockDataset> | null = null
  const ready = () => (cached ??= loadDataset())

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
      return found
    },

    async approveProposal(id: string): Promise<Proposal> {
      return decide(await ready(), id, 'approved')
    },

    async rejectProposal(id: string): Promise<Proposal> {
      return decide(await ready(), id, 'rejected')
    },

    async applyProposal(id: string): Promise<Proposal> {
      const data = await ready()
      const current = data.proposalById.get(id)
      if (!current) throw notFound('proposal', id)
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
      return decide(data, id, 'applied')
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
        checks: goldenSummary.self_check as Record<string, boolean>,
      }
    },
  }
}

function decide(
  data: MockDataset,
  id: string,
  status: ProposalStatus,
): Proposal {
  const current = data.proposalById.get(id)
  if (!current) throw notFound('proposal', id)
  const updated: Proposal = {
    ...current,
    status,
    decided_by: 'reviewer@keystone.example',
    decided_at: '2026-08-22T10:00:00Z',
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
