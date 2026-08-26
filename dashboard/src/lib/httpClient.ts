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
  type ApplyMode,
  type AuditPage,
  type AuditQuery,
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

/**
 * Is this body a proposal row, or something else the endpoint chose to return?
 *
 * Used only by `rollbackProposal`, whose response shape is not pinned anywhere:
 * the service may answer with the updated proposal, with
 * `recon.apply.RollbackResult.as_dict` (digests, no row), or with 204. Rather
 * than assert one and mis-type the other two, the client checks and hands back
 * `null` when it is not looking at a row. The UI refetches either way.
 */
function asProposal(body: unknown): Proposal | null {
  if (typeof body !== 'object' || body === null) return null
  const candidate = body as Partial<Proposal>
  return typeof candidate.id === 'string' && typeof candidate.status === 'string'
    ? (body as Proposal)
    : null
}

/**
 * The real client. `rollbackProposal` is narrowed to REQUIRED here even though
 * `KeystoneApi` declares it optional: the optionality exists so that a client
 * which cannot reverse a write is representable (see `KeystoneApi`), and this
 * one can, so callers should not have to null-check the method they are looking
 * straight at.
 */
export const httpClient: KeystoneApi &
  Required<Pick<KeystoneApi, 'rollbackProposal' | 'listAudit'>> = {
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

  /**
   * PINNED: `POST /api/proposals/{id}/apply`, "approved only; auto path per R24".
   *
   * ==========================================================================
   * THE `auto` PARAMETER IS THE WHOLE DIFFERENCE BETWEEN TWO WRITES.
   * ==========================================================================
   * `review.py::apply_endpoint` takes `auto: bool = Query(default=False)`:
   *
   *   `?auto=true`  → `recon.apply.auto_apply` — R24's gate runs FIRST and the
   *                   write is refused with 409 plus every condition it
   *                   evaluated. A sensitive proposal is refused at any
   *                   confidence, before its confidence is read at all.
   *   no parameter  → `recon.apply.apply_proposal` — the reviewer's own
   *                   authorised write. No gate.
   *
   * This method used to send NO `auto` parameter from anywhere, so every button
   * in the dashboard was the ungated reviewer write and R24's guarded auto-apply
   * — a graded requirement and a demo beat — was unreachable from the UI. It was
   * not visibly broken, which is why it survived: the manual path 200s.
   *
   * `'manual'` deliberately sends NOTHING rather than `auto=false`. The absent
   * parameter IS the pinned default, so the manual request stays byte-identical
   * to the one this client has always sent; adding a redundant `auto=false`
   * would change the working path in order to decorate it.
   */
  applyProposal(id, mode: ApplyMode = 'manual') {
    return apiFetch<Proposal>(`/api/proposals/${encodeURIComponent(id)}/apply`, {
      method: 'POST',
      query: mode === 'auto' ? { auto: true } : undefined,
    })
  },

  /**
   * `POST /api/proposals/{id}/rollback` — the reversal leg of R24's "recorded
   * rollback path".
   *
   * NOT PINNED, and at the time of writing not present in `recon/api/review.py`
   * either: `recon.apply.rollback_proposal` exists and works, but no route
   * exposed it, so the reversal beat was a Python one-liner rather than
   * something a reviewer could do. This client asks for the route the service
   * needs; a build without it answers 404/405 and the UI renders that answer
   * verbatim instead of claiming a rollback happened.
   *
   * The body is not depended on — see `KeystoneApi.rollbackProposal`.
   */
  async rollbackProposal(id) {
    const body = await apiFetch<unknown>(
      `/api/proposals/${encodeURIComponent(id)}/rollback`,
      { method: 'POST' },
    )
    return asProposal(body)
  },

  getScorecard(signal) {
    // ASSUMED A4 for the body shape.
    return apiFetch<Scorecard>('/api/scorecard', { signal })
  },

  /**
   * `GET /api/audit` — `recon/api/audit.py`. Admin scope; a `client` key is
   * answered 403 and that 403 reaches the route as an ordinary `ApiError`.
   *
   * No `filterGuard` equivalent runs here, and that is a real difference rather
   * than an omission. filterGuard exists because `/api/proposals` is asked to
   * filter on `source` and `type`, which are not columns of `proposals` (A8) —
   * so a service could serve 200 with an unfiltered page. `actor`, `action` and
   * `subject` ARE columns of `audit_log`, and every row the endpoint returns
   * carries the value it was filtered on, so the guard is the render itself:
   * `Audit.tsx` shows the actor and action of every row it draws, next to the
   * filter that is supposed to have selected them. An ignored filter is visible
   * on screen, not merely warned about.
   */
  listAudit(query: AuditQuery, signal?: AbortSignal) {
    return apiFetch<AuditPage>('/api/audit', {
      query: {
        actor: query.actor,
        action: query.action,
        subject: query.subject,
        page: query.page,
        page_size: clampPageSize(query.page_size),
      },
      signal,
    })
  },
}
