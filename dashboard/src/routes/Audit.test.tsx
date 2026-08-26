/**
 * /audit — the log surface and the verification checks, at two layers.
 *
 * ===========================================================================
 * Layer 1 (`describe('the REAL http client')`): NOTHING IS SWAPPED.
 * ===========================================================================
 * It imports the real `httpClient`, stubs `globalThis.fetch`, and asserts on the
 * REQUEST that leaves the module — path, method, query string, the `X-Api-Key`
 * header — and on how the real module turns a 403 into a typed error. That is
 * the same discipline `src/lib/httpClient.test.ts` was written for, and for the
 * same reason: every route suite in this package injects a client, so without
 * one test at this layer `httpClient.listAudit` would be executed by nothing and
 * a wrong URL would leave the whole file green.
 *
 * ===========================================================================
 * Layer 2 (`describe('/audit')`): the real <App>, at a real URL, injected client.
 * ===========================================================================
 * These drive the same `<App>` the browser gets through `renderApp`; only the
 * API client is swapped, and it is swapped for one implementing the same
 * `KeystoneApi` interface. The bodies are shaped like the SERVICE's
 * (`recon/api/audit.py::_audit_row`, `recon/api/scorecard.py`), not like the
 * in-browser mock's — the lesson `src/routes/serviceShape.test.tsx` exists to
 * record. One case deliberately runs against the mock instead, to prove the
 * demo build renders and to pin the mock's honest "not reported" behaviour.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { httpClient } from '../lib/httpClient'
import { ApiError } from '../lib/api'
import { gradeConflict, makeClient, renderApp } from '../test/harness'
import { auditBody, auditConfidence, checkRows, usd } from './Audit'
import type {
  AuditEntry,
  AuditPage,
  AuditQuery,
  KeystoneApi,
  Scorecard,
} from '../lib/contract'

// ---------------------------------------------------------------------------
// Bodies shaped like the SERVICE (`recon/api/audit.py::_audit_row`)
// ---------------------------------------------------------------------------

const FINGERPRINT = 'a'.repeat(64)

/** The chokepoint's envelope: `recon.logging.audit_detail`. */
function envelope(body: Record<string, unknown>): Record<string, unknown> {
  return { mode: 'safe', body_sha256: 'b'.repeat(64), body }
}

function entry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: '9001',
    ts: '2026-08-22T11:00:00+00:00',
    actor: 'system:reconciler',
    action: 'proposal.created',
    subject: FINGERPRINT,
    detail: envelope({
      proposal_id: 4001,
      conflict_id: 9001,
      type: 'C6',
      status: 'pending',
      // A STRING, exactly as `recon.reconciler._proposal_audit_row` writes it.
      confidence: '0.91',
    }),
    tokens_in: null,
    tokens_out: null,
    cost_microusd: null,
    ...overrides,
  }
}

/** A reviewer decision: the actor is a REDACTION TOKEN, as the service serves it. */
const REVIEWER_TOKEN = '[pii:email:0e25494e6071:aaaaaaaa@aaaaaaaa.aaaaaa~]'

function auditPage(
  items: AuditEntry[],
  overrides: Partial<AuditPage> = {},
): AuditPage {
  return {
    items,
    page: 1,
    page_size: 25,
    total: items.length,
    totals: {
      tokens_in: items.reduce((n, i) => n + (i.tokens_in ?? 0), 0),
      tokens_out: items.reduce((n, i) => n + (i.tokens_out ?? 0), 0),
      cost_microusd: items.reduce((n, i) => n + (i.cost_microusd ?? 0), 0),
      priced_rows: items.filter((i) => i.cost_microusd !== null).length,
    },
    actors: ['system:budget', 'system:reconciler', REVIEWER_TOKEN],
    actions: ['llm_call', 'proposal.approved', 'proposal.created', 'reconcile.run'],
    ...overrides,
  }
}

/** `recon/api/scorecard.py` serves `docs/scorecard.json` as written. */
function scorecard(overrides: Partial<Scorecard> = {}): Scorecard {
  return {
    generated_at: '2026-08-22T09:00:00Z',
    run_id: 'run-0003',
    conflicts: { total: 1, by_type: { C6: 1 } },
    proposals: { total: 1, by_status: { pending: 1 } },
    checks: {
      'spend-cap-burst': true,
      'bench:spend-cap-exact': true,
      determinism: true,
      'golden-diff': true,
      coverage: true,
    },
    details: {
      'spend-cap-burst':
        'contenders=120 granted=6 refused=114 other=0 refusal_sqlstates=[KS006] cap=81600',
    },
    ...overrides,
  }
}

/**
 * A client that answers ONLY what `/audit` reads, and records what it was asked.
 *
 * Built on the mock so the rest of `<App>` (the nav, the shell) still works,
 * with `listAudit` and `getScorecard` replaced by service-shaped answers.
 */
function auditClient(options: {
  page?: (query: AuditQuery) => AuditPage
  card?: Scorecard | (() => never)
  seen?: AuditQuery[]
}): KeystoneApi {
  const base = makeClient([gradeConflict(0)])
  return {
    ...base,
    listAudit: async (query) => {
      options.seen?.push(query)
      return (options.page ?? (() => auditPage([entry()])))(query)
    },
    getScorecard: async () => {
      const card = options.card ?? scorecard()
      return typeof card === 'function' ? card() : card
    },
  }
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

// ===========================================================================
// Layer 1 — the real HTTP client, at the network layer
// ===========================================================================

describe('the REAL http client asks the service for the log', () => {
  interface Captured {
    url: URL
    method: string
    headers: Record<string, string>
  }
  let calls: Captured[] = []

  function installFetch(responder: () => Response): void {
    calls = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const headers: Record<string, string> = {}
        new Headers(init?.headers).forEach((value, key) => {
          headers[key.toLowerCase()] = value
        })
        calls.push({
          url: new URL(String(input), 'http://relative.invalid'),
          method: init?.method ?? 'GET',
          headers,
        })
        return responder()
      }),
    )
  }

  function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }

  beforeEach(() => {
    vi.stubEnv('VITE_API_KEY', 'committed-admin-demo-key')
    vi.stubEnv('VITE_API_BASE_URL', 'https://keystone.test')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('GETs /api/audit with the filters and the admin key', async () => {
    installFetch(() => json(auditPage([entry()])))

    const page = await httpClient.listAudit(
      {
        actor: 'system:reconciler',
        action: 'proposal.created',
        subject: FINGERPRINT,
        page: 2,
        page_size: 10,
      },
      undefined,
    )

    expect(calls).toHaveLength(1)
    const [request] = calls
    expect(request.method).toBe('GET')
    expect(request.url.origin + request.url.pathname).toBe(
      'https://keystone.test/api/audit',
    )
    expect(Object.fromEntries(request.url.searchParams.entries())).toEqual({
      actor: 'system:reconciler',
      action: 'proposal.created',
      subject: FINGERPRINT,
      page: '2',
      page_size: '10',
    })
    expect(request.headers['x-api-key']).toBe('committed-admin-demo-key')
    expect(page.totals.priced_rows).toBe(0)
  })

  it('sends no filter parameters when none are set', async () => {
    installFetch(() => json(auditPage([])))
    await httpClient.listAudit({})
    expect([...calls[0].url.searchParams.keys()]).toEqual([])
  })

  it('clamps page_size to the contract cap before it leaves the browser', async () => {
    installFetch(() => json(auditPage([])))
    await httpClient.listAudit({ page_size: 5000 })
    expect(calls[0].url.searchParams.get('page_size')).toBe('100')
  })

  it('surfaces the service 403 for a client-scoped key as a typed ApiError', async () => {
    installFetch(() =>
      json(
        {
          type: 'https://keystone.invalid/problems/forbidden',
          title: 'forbidden',
          status: 403,
          detail: "scope 'client' may not use this endpoint; 'admin' is required (R20).",
        },
        403,
      ),
    )
    await expect(httpClient.listAudit({})).rejects.toBeInstanceOf(ApiError)
  })
})

// ===========================================================================
// Layer 2 — the route, through the real <App>
// ===========================================================================

describe('/audit renders the log a reviewer reconciles against', () => {
  it('shows the actions, the actors and the confidence the log recorded', async () => {
    renderApp(
      auditClient({
        page: () =>
          auditPage([
            entry(),
            entry({
              id: '9002',
              action: 'llm_call',
              actor: 'system:budget',
              subject: 'daily',
              detail: envelope({ model: 'mock-rationale-v1', scope: 'daily' }),
              tokens_in: 240,
              tokens_out: 61,
              cost_microusd: 1797,
            }),
          ]),
      }),
      '/audit',
    )

    const table = await screen.findByRole('table', { name: /audit log/i })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)

    expect(within(table).getByText('proposal.created')).toBeInTheDocument()
    expect(within(table).getByText('llm_call')).toBeInTheDocument()
    expect(within(table).getByText('system:budget')).toBeInTheDocument()
    // The confidence is a STRING in the service's body and must still render as
    // a two-decimal figure in its own column, not as `NaN` and not only as a
    // line inside the expandable detail.
    // `Confidence` carries the test id only when it HAS a value, so exactly one
    // of the two rows is expected here: the `llm_call` row recorded none.
    const confidences = within(table).getAllByTestId('confidence')
    expect(confidences).toHaveLength(1)
    expect(confidences[0]).toHaveTextContent('0.91')
    expect(within(table).getByText('240 / 61')).toBeInTheDocument()
  })

  it('reports the recorded cost over the FILTERED SET, not over the page', async () => {
    renderApp(
      auditClient({
        page: () =>
          auditPage([entry()], {
            total: 3050,
            totals: {
              tokens_in: 512000,
              tokens_out: 128000,
              cost_microusd: 2_500_000,
              priced_rows: 1900,
            },
          }),
      }),
      '/audit',
    )

    const totals = await screen.findByTestId('audit-totals')
    expect(totals).toHaveTextContent('3050')
    expect(totals).toHaveTextContent('1900')
    expect(totals).toHaveTextContent('512000')
    // Integer microUSD, with the dollar figure beside it — never a rounded
    // dollar amount standing alone.
    expect(screen.getByTestId('audit-cost')).toHaveTextContent(
      '2500000 µUSD ($2.500000)',
    )
  })

  it('labels a redacted actor as withheld rather than rendering it as a value', async () => {
    renderApp(
      auditClient({
        page: () =>
          auditPage([
            entry({
              id: '9003',
              action: 'proposal.approved',
              actor: REVIEWER_TOKEN,
              subject: '4001',
            }),
          ]),
      }),
      '/audit',
    )

    await screen.findByRole('table', { name: /audit log/i })
    const redacted = screen.getAllByTestId('redacted')
    expect(redacted.length).toBeGreaterThan(0)
    expect(redacted[0]).toHaveTextContent('redacted')
    // The token itself is still on screen: it is stable per value, so two rows
    // showing the same token are the same actor and that must stay checkable.
    expect(redacted[0]).toHaveTextContent(REVIEWER_TOKEN)
  })

  it('sends the filter the reviewer chose to the SERVER and returns to page 1', async () => {
    const seen: AuditQuery[] = []
    const user = userEvent.setup()
    renderApp(auditClient({ seen }), '/audit?page=4')

    await screen.findByRole('table', { name: /audit log/i })
    expect(seen.at(-1)).toMatchObject({ page: 4 })

    await user.selectOptions(
      screen.getByLabelText('Action'),
      'proposal.approved',
    )

    await waitFor(() =>
      expect(seen.at(-1)).toMatchObject({
        action: 'proposal.approved',
        page: 1,
      }),
    )
    // The filter lives in the URL, so a filtered view is shareable and survives
    // a reload — and the page number was dropped, not carried.
    expect(window.location.search).toContain('action=proposal.approved')
    expect(window.location.search).not.toContain('page=4')
  })

  it('sends a pasted subject — the id the rest of the dashboard shows', async () => {
    const seen: AuditQuery[] = []
    const user = userEvent.setup()
    renderApp(auditClient({ seen }), '/audit')

    await screen.findByRole('table', { name: /audit log/i })
    const field = screen.getByLabelText(/^Subject/)
    await user.click(field)
    await user.paste(FINGERPRINT)
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(seen.at(-1)).toMatchObject({ subject: FINGERPRINT }),
    )
  })

  /**
   * The subject box must be CONTROLLED by the same state the two selects beside
   * it are controlled by.
   *
   * An uncontrolled `defaultValue` box looks identical until a filter is
   * cleared from OUTSIDE the box. React does not write back to an uncontrolled
   * input on re-render, and `AuditRoute` is not remounted by a query-string
   * change, so the old text survives "Clear filters" — and because leaving the
   * box commits whatever is in it, the reviewer's next tab through the filter
   * row silently RE-APPLIES the filter they just cleared, against a URL and a
   * pair of selects that say it is gone.
   */
  it('clears the subject box with the filters, and a later blur does not re-apply it', async () => {
    const seen: AuditQuery[] = []
    const user = userEvent.setup()
    renderApp(auditClient({ seen }), `/audit?subject=${FINGERPRINT}`)

    await screen.findByRole('table', { name: /audit log/i })
    const field = screen.getByLabelText(/^Subject/)
    expect(field).toHaveValue(FINGERPRINT)
    await waitFor(() => expect(seen.at(-1)).toMatchObject({ subject: FINGERPRINT }))

    await user.click(screen.getByRole('button', { name: /clear filters/i }))

    // The box the reviewer can SEE is empty — not just the URL behind it.
    await waitFor(() => expect(field).toHaveValue(''))
    expect(window.location.search).not.toContain('subject=')

    // ... and focusing the box and leaving it does not put the filter back.
    const afterClear = seen.length
    await user.click(field)
    await user.tab()

    expect(field).toHaveValue('')
    expect(window.location.search).not.toContain('subject=')
    await waitFor(() =>
      expect(screen.getByLabelText(/^Subject/)).toHaveValue(''),
    )
    expect(seen.slice(afterClear).map((query) => query.subject)).not.toContain(
      FINGERPRINT,
    )
  })

  it('says an empty page is an empty RESULT, not an unfiltered one', async () => {
    renderApp(
      auditClient({ page: () => auditPage([], { total: 0 }) }),
      '/audit?action=no.such.action',
    )
    expect(
      await screen.findByText(/No audit entries match these filters/),
    ).toBeInTheDocument()
    expect(screen.getByText(/applied by the service/)).toBeInTheDocument()
  })

  it('renders the service 403 verbatim instead of an empty log', async () => {
    const base = makeClient([gradeConflict(0)])
    const forbidden = new ApiError({
      type: 'https://keystone.invalid/problems/forbidden',
      title: 'forbidden',
      status: 403,
      detail: "scope 'client' may not use this endpoint; 'admin' is required (R20).",
    })
    renderApp(
      {
        ...base,
        listAudit: async () => {
          throw forbidden
        },
      },
      '/audit',
    )

    const alert = await screen.findByTestId('error-state')
    expect(alert).toHaveTextContent('forbidden')
    expect(alert).toHaveTextContent('admin')
    expect(screen.queryByRole('table', { name: /audit log/i })).toBeNull()
  })

  it('refuses BEFORE any request when the client cannot read the log at all', async () => {
    // `listAudit` is optional on `KeystoneApi`. A client without it must not
    // read to a reviewer as a service saying the log is empty.
    const withoutAudit: KeystoneApi = { ...makeClient([gradeConflict(0)]) }
    delete withoutAudit.listAudit
    renderApp(withoutAudit, '/audit')

    const alert = await screen.findByTestId('error-state')
    expect(alert).toHaveTextContent(/listAudit/)
    expect(alert).toHaveTextContent(/not an empty log/i)
  })
})

// ===========================================================================
// The checks panel — the half of the scorecard that was never rendered
// ===========================================================================

describe('/audit surfaces the verification checks', () => {
  it('leads with the spend-cap burst and shows what it measured', async () => {
    renderApp(auditClient({}), '/audit')

    const summary = await screen.findByTestId('spend-cap-summary')
    expect(summary).toHaveTextContent('spend-cap-burst')
    expect(within(summary).getByTestId('verdict-pass')).toHaveTextContent('Pass')
    expect(screen.getByTestId('spend-cap-detail')).toHaveTextContent(
      'contenders=120 granted=6 refused=114',
    )
    expect(screen.getByTestId('checks-passing')).toHaveTextContent('5 of 5')

    // It is FIRST in the table, not buried in name order among the others.
    const table = screen.getByTestId('checks-table')
    const headers = within(table)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent)
    expect(headers[0]).toBe('spend-cap-burst')
    expect(headers).toContain('determinism')
  })

  it('shows a FAILED check as failed, in words as well as in colour', async () => {
    renderApp(
      auditClient({
        card: scorecard({
          checks: { 'spend-cap-burst': false, determinism: true },
          details: { 'spend-cap-burst': 'over-admitted=True ledger_violations=2' },
        }),
      }),
      '/audit',
    )

    const summary = await screen.findByTestId('spend-cap-summary')
    expect(within(summary).getByTestId('verdict-fail')).toHaveTextContent('Fail')
    expect(screen.getByTestId('checks-passing')).toHaveTextContent('1 of 2')
  })

  it('reports a check the scorecard does not carry as NOT REPORTED, never as a pass', async () => {
    renderApp(
      auditClient({
        card: scorecard({ checks: { sc_golden_key_unique: true }, details: undefined }),
      }),
      '/audit',
    )

    const summary = await screen.findByTestId('spend-cap-summary')
    expect(within(summary).getByTestId('verdict-unreported')).toHaveTextContent(
      'Not reported',
    )
    expect(within(summary).queryByTestId('verdict-pass')).toBeNull()
    expect(screen.getByTestId('spend-cap-detail')).toHaveTextContent(
      'recon.suite',
    )
  })

  it('renders the log even when the scorecard cannot be read', async () => {
    renderApp(
      auditClient({
        card: () => {
          throw new ApiError({
            type: 'https://keystone.invalid/problems/scorecard-unavailable',
            title: 'no scorecard has been generated',
            status: 503,
          })
        },
      }),
      '/audit',
    )

    // Two independent fetches; a scorecard that is missing must not take the
    // log down with it.
    expect(await screen.findByTestId('error-state')).toHaveTextContent(
      'no scorecard has been generated',
    )
    expect(await screen.findByRole('table', { name: /audit log/i })).toBeInTheDocument()
  })
})

// ===========================================================================
// The demo build — the in-browser mock, and its honest gap
// ===========================================================================

describe('/audit under the in-browser mock', () => {
  it('renders a derived log, and does NOT claim the spend cap was verified', async () => {
    // The mock's own client, unmodified: this is what `pnpm dev:mock` and the
    // Playwright a11y run see.
    renderApp(makeClient([gradeConflict(0), gradeConflict(1)]), '/audit')

    const table = await screen.findByRole('table', { name: /audit log/i })
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1)
    expect(within(table).getAllByText('proposal.created').length).toBeGreaterThan(0)

    // The mock is seeded from the generator's manifest, which carries no suite
    // verdicts — and it does not invent one.
    const summary = await screen.findByTestId('spend-cap-summary')
    expect(within(summary).getByTestId('verdict-unreported')).toBeInTheDocument()
  })
})

// ===========================================================================
// The pure readers, on the shapes the service really writes
// ===========================================================================

describe('reading a row', () => {
  it('unwraps the chokepoint envelope and leaves an unrouted body alone', () => {
    expect(auditBody(envelope({ a: 1 }))).toEqual({ a: 1 })
    // `recon.logging.AUDIT_WRITERS` declares two writers that bind detail
    // without the envelope; those bodies must not render as empty.
    expect(auditBody({ scope: 'daily', reserve_microusd: 13600 })).toEqual({
      scope: 'daily',
      reserve_microusd: 13600,
    })
    expect(auditBody(null)).toBeNull()
  })

  it('reads a confidence written as a string OR as a number', () => {
    expect(auditConfidence(entry())).toBe(0.91)
    expect(
      auditConfidence(entry({ detail: envelope({ confidence: 0.5 }) })),
    ).toBe(0.5)
    expect(auditConfidence(entry({ detail: envelope({}) }))).toBeNull()
    expect(auditConfidence(entry({ detail: null }))).toBeNull()
    // A tokenised confidence is not a number and must not become one.
    expect(
      auditConfidence(entry({ detail: envelope({ confidence: '[pii:opaque:ab:aa]' }) })),
    ).toBeNull()
  })

  it('orders the checks headline-first and renders every one the card carries', () => {
    const rows = checkRows(
      scorecard({
        checks: { zzz: true, 'golden-diff': false, 'spend-cap-burst': true, aaa: true },
      }),
    )
    expect(rows.map((row) => row.name)).toEqual([
      'spend-cap-burst',
      'golden-diff',
      'aaa',
      'zzz',
    ])
    expect(rows.map((row) => row.verdict)).toEqual([
      'pass',
      'fail',
      'pass',
      'pass',
    ])
  })

  it('renders integer microUSD as dollars without inventing precision', () => {
    expect(usd(1797)).toBe('$0.001797')
    expect(usd(0)).toBe('$0.000000')
  })
})
