/**
 * The REAL client. This is the DEFAULT code path — `src/lib/apiClient.ts`
 * resolves to this unless VITE_USE_MOCK_API=1 is set explicitly.
 *
 * It talks to the Keystone HTTP client API over `apiFetch` (X-Api-Key, RFC7807
 * errors). Everything here is the PINNED contract from DESIGN §HTTP API except
 * where a comment names an ASSUMED item from src/lib/contract.ts.
 */
import { apiFetch } from './api'
import { guardConflictPage, guardProposalPage } from './filterGuard'
import {
  MAX_PAGE_SIZE,
  type Conflict,
  type KeystoneApi,
  type Page,
  type Proposal,
  type Scorecard,
} from './contract'

function clampPageSize(size: number | undefined): number | undefined {
  if (size === undefined) return undefined
  return Math.min(Math.max(1, Math.trunc(size)), MAX_PAGE_SIZE)
}

export const httpClient: KeystoneApi = {
  async listConflicts(query, signal) {
    // ASSUMED A1: page / page_size params and the {items,...} envelope.
    const page = await apiFetch<Page<Conflict>>('/api/conflicts', {
      query: {
        source: query.source,
        type: query.type,
        status: query.status,
        page: query.page,
        page_size: clampPageSize(query.page_size),
      },
      signal,
    })
    // A 200 is not proof the filter was applied. See filterGuard.ts.
    return guardConflictPage(query, page)
  },

  getConflict(id, signal) {
    // ASSUMED A2: per-id GET.
    return apiFetch<Conflict>(`/api/conflicts/${encodeURIComponent(id)}`, {
      signal,
    })
  },

  async listProposals(query, signal) {
    // ASSUMED A8 for `source` / `type`: the proposals table has neither column,
    // so these two filters need a JOIN to conflicts. A service that ignores
    // them answers 200 with the UNFILTERED page — guardProposalPage() makes
    // that visible instead of letting it read as a filtered result.
    const page = await apiFetch<Page<Proposal>>('/api/proposals', {
      query: {
        source: query.source,
        type: query.type,
        status: query.status,
        // ASSUMED A3: conflict_id filter.
        conflict_id: query.conflict_id,
        page: query.page,
        page_size: clampPageSize(query.page_size),
      },
      signal,
    })
    return guardProposalPage(query, page)
  },

  getProposal(id, signal) {
    // ASSUMED A2.
    return apiFetch<Proposal>(`/api/proposals/${encodeURIComponent(id)}`, {
      signal,
    })
  },

  approveProposal(id) {
    // PINNED: POST /api/proposals/{id}/approve. ASSUMED A5 for the body — the
    // UI refetches regardless, so a different body shape costs nothing.
    return apiFetch<Proposal>(
      `/api/proposals/${encodeURIComponent(id)}/approve`,
      { method: 'POST' },
    )
  },

  rejectProposal(id) {
    return apiFetch<Proposal>(
      `/api/proposals/${encodeURIComponent(id)}/reject`,
      { method: 'POST' },
    )
  },

  applyProposal(id) {
    return apiFetch<Proposal>(`/api/proposals/${encodeURIComponent(id)}/apply`, {
      method: 'POST',
    })
  },

  getScorecard(signal) {
    // ASSUMED A4 for the body shape.
    return apiFetch<Scorecard>('/api/scorecard', { signal })
  },
}
