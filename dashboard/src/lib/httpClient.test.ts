/**
 * THE REAL CLIENT, TESTED AT THE NETWORK LAYER.
 *
 * Every other suite in this package injects a client through `ApiProvider`, and
 * the Playwright run sets VITE_USE_MOCK_API=1. That means src/lib/httpClient.ts
 * and src/lib/api.ts — the code that will actually talk to the service when
 * T-5/T-7/T-8 land — were executed by NOTHING. A triple sabotage proved it:
 * approveProposal POSTing to /reject, listConflicts fetching the wrong URL, and
 * applyProposal hitting /approve, all at once, left both suites green.
 *
 * So this file swaps NOTHING. It imports the real `httpClient`, installs a
 * typed stub on `globalThis.fetch`, and asserts on the REQUEST that leaves the
 * module — exact URL, method, query string, headers — and on how the real
 * module turns each class of response into a typed error.
 *
 * If you change an endpoint, a param name, a header, or an error path, a test
 * here goes red. That is the whole point.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { httpClient } from './httpClient'
import {
  ApiConfigError,
  ApiError,
  ApiNetworkError,
  ApiParseError,
  KeystoneApiError,
} from './api'
import {
  MAX_PAGE_SIZE,
  type Conflict,
  type Page,
  type Proposal,
  type Scorecard,
} from './contract'

const KEY = 'committed-admin-demo-key'
const BASE = 'https://keystone.test'

// ---------------------------------------------------------------------------
// The fetch stub. It records what the client asked for and answers with a real
// `Response`, so nothing here is a re-implementation of fetch's semantics.
// ---------------------------------------------------------------------------

interface Captured {
  raw: string
  url: URL
  method: string
  headers: Record<string, string>
  body: string | null
  signal: AbortSignal | null | undefined
}

type Responder = (request: Captured) => Response | Promise<Response>

let calls: Captured[] = []

function headersOf(init?: HeadersInit): Record<string, string> {
  const out: Record<string, string> = {}
  if (!init) return out
  new Headers(init).forEach((value, key) => {
    out[key.toLowerCase()] = value
  })
  return out
}

function installFetch(responder: Responder = () => json({})): void {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = String(input)
      const captured: Captured = {
        raw,
        // Absolute in every test that matters; the fallback base only exists so
        // a relative URL (no VITE_API_BASE_URL) is still parseable.
        url: new URL(raw, 'http://relative.invalid'),
        method: init?.method ?? 'GET',
        headers: headersOf(init?.headers),
        body: typeof init?.body === 'string' ? init.body : null,
        signal: init?.signal,
      }
      calls.push(captured)
      return responder(captured)
    }),
  )
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function only(): Captured {
  expect(calls).toHaveLength(1)
  return calls[0]
}

function params(request: Captured): Record<string, string> {
  return Object.fromEntries(request.url.searchParams.entries())
}

// ---------------------------------------------------------------------------
// Fixtures shaped like the contract (src/lib/contract.ts), never like the mock.
// ---------------------------------------------------------------------------

function conflict(overrides: Partial<Conflict> = {}): Conflict {
  return {
    id: 'c-1',
    fingerprint: 'f'.repeat(64),
    type: 'C6',
    entity_refs: ['crm:contact:CRM-000001'],
    sources: ['crm', 'appdb'],
    disagreeing_fields: ['crm.contact.grade', 'appdb.student.grade'],
    status: 'open',
    first_seen_run: 'run-0001',
    last_seen_run: 'run-0003',
    ...overrides,
  }
}

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: 'p-1',
    conflict_id: 'c-1',
    fingerprint: 'f'.repeat(64),
    action: { kind: 'set_field', target_path: 'crm.contact.grade' },
    confidence: 0.81,
    evidence: { rule_id: 'R-006' },
    rationale: null,
    status: 'pending',
    sensitive: false,
    created_run: 'run-0003',
    decided_by: null,
    decided_at: null,
    ...overrides,
  }
}

function page<T>(items: T[], overrides: Partial<Page<T>> = {}): Page<T> {
  return { items, page: 1, page_size: 25, total: items.length, ...overrides }
}

const SCORECARD: Scorecard = {
  generated_at: '2026-08-22T09:00:00Z',
  run_id: 'run-0003',
  conflicts: { total: 1, by_type: { C6: 1 } },
  proposals: { total: 1, by_status: { pending: 1 } },
  checks: { sc_golden_key_unique: true },
}

/**
 * Every method the client exposes, with a body the real service could return.
 * Used by the header, error and configuration suites so a NEW method cannot be
 * added to `KeystoneApi` without being covered here.
 */
const METHODS: {
  name: string
  invoke: (signal?: AbortSignal) => Promise<unknown>
  ok: unknown
}[] = [
  {
    name: 'listConflicts',
    invoke: (signal) => httpClient.listConflicts({}, signal),
    ok: page([conflict()]),
  },
  {
    name: 'getConflict',
    invoke: (signal) => httpClient.getConflict('c-1', signal),
    ok: conflict(),
  },
  {
    name: 'listProposals',
    invoke: (signal) => httpClient.listProposals({}, signal),
    ok: page([proposal()]),
  },
  {
    name: 'getProposal',
    invoke: (signal) => httpClient.getProposal('p-1', signal),
    ok: proposal(),
  },
  {
    name: 'approveProposal',
    invoke: () => httpClient.approveProposal('p-1'),
    ok: proposal({ status: 'approved' }),
  },
  {
    name: 'rejectProposal',
    invoke: () => httpClient.rejectProposal('p-1'),
    ok: proposal({ status: 'rejected' }),
  },
  {
    name: 'applyProposal',
    invoke: () => httpClient.applyProposal('p-1'),
    ok: proposal({ status: 'applied' }),
  },
  {
    name: 'rollbackProposal',
    invoke: () => httpClient.rollbackProposal('p-1'),
    ok: proposal({ status: 'rolled_back' }),
  },
  {
    name: 'getScorecard',
    invoke: (signal) => httpClient.getScorecard(signal),
    ok: SCORECARD,
  },
]

beforeEach(() => {
  vi.stubEnv('VITE_API_KEY', KEY)
  vi.stubEnv('VITE_API_BASE_URL', BASE)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// URL, method and id encoding — the sabotage surface
// ---------------------------------------------------------------------------

describe('the exact request each method makes', () => {
  it('every method covered here is a method the client actually exposes', () => {
    // Guards the tables below: a renamed or removed method must fail loudly
    // rather than silently stop being tested.
    const covered = new Set(METHODS.map((m) => m.name))
    const exposed = Object.keys(httpClient).sort()
    expect(exposed).toEqual([...covered].sort())
    // 8 -> 9: `rollbackProposal` was ADDED to the client, and this count is the
    // registration that forces it into `METHODS` and `CASES` above rather than
    // letting a ninth method ship untested. The number only ever moves UP with a
    // new method, and only together with its rows in both tables — moving it
    // without them is the failure this assertion exists to catch.
    expect(exposed).toHaveLength(9)
  })

  const CASES: {
    name: string
    invoke: () => Promise<unknown>
    pathname: string
    method: string
    ok: unknown
  }[] = [
    {
      name: 'listConflicts',
      invoke: () => httpClient.listConflicts({}),
      pathname: '/api/conflicts',
      method: 'GET',
      ok: page([conflict()]),
    },
    {
      name: 'getConflict',
      invoke: () => httpClient.getConflict('c-1'),
      pathname: '/api/conflicts/c-1',
      method: 'GET',
      ok: conflict(),
    },
    {
      name: 'listProposals',
      invoke: () => httpClient.listProposals({}),
      pathname: '/api/proposals',
      method: 'GET',
      ok: page([proposal()]),
    },
    {
      name: 'getProposal',
      invoke: () => httpClient.getProposal('p-1'),
      pathname: '/api/proposals/p-1',
      method: 'GET',
      ok: proposal(),
    },
    {
      name: 'approveProposal',
      invoke: () => httpClient.approveProposal('p-1'),
      pathname: '/api/proposals/p-1/approve',
      method: 'POST',
      ok: proposal(),
    },
    {
      name: 'rejectProposal',
      invoke: () => httpClient.rejectProposal('p-1'),
      pathname: '/api/proposals/p-1/reject',
      method: 'POST',
      ok: proposal(),
    },
    {
      name: 'applyProposal',
      invoke: () => httpClient.applyProposal('p-1'),
      pathname: '/api/proposals/p-1/apply',
      method: 'POST',
      ok: proposal(),
    },
    {
      name: 'rollbackProposal',
      invoke: () => httpClient.rollbackProposal('p-1'),
      pathname: '/api/proposals/p-1/rollback',
      method: 'POST',
      ok: proposal({ status: 'rolled_back' }),
    },
    {
      name: 'getScorecard',
      invoke: () => httpClient.getScorecard(),
      pathname: '/api/scorecard',
      method: 'GET',
      ok: SCORECARD,
    },
  ]

  for (const testCase of CASES) {
    it(`${testCase.name} → ${testCase.method} ${testCase.pathname}`, async () => {
      installFetch(() => json(testCase.ok))
      await testCase.invoke()
      const request = only()
      expect(request.url.origin).toBe(BASE)
      expect(request.url.pathname).toBe(testCase.pathname)
      expect(request.method).toBe(testCase.method)
    })
  }

  /**
   * The sabotage that stayed green: approve POSTing to /reject. Each decision
   * is asserted to hit its OWN endpoint and, explicitly, NEITHER of the other
   * two — a reviewer's approve becoming a reject is silent data damage.
   */
  it.each([
    ['approveProposal', 'approve', ['reject', 'apply', 'rollback']],
    ['rejectProposal', 'reject', ['approve', 'apply', 'rollback']],
    ['applyProposal', 'apply', ['approve', 'reject', 'rollback']],
    // `rollback` is the only one of the four that WRITES BACK, so a rollback
    // that landed on /apply would re-apply the fix it was asked to undo.
    ['rollbackProposal', 'rollback', ['approve', 'reject', 'apply']],
  ] as const)(
    '%s posts to /%s and to nothing else',
    async (method, segment, forbidden) => {
      installFetch(() => json(proposal()))
      await httpClient[
        method as
          | 'approveProposal'
          | 'rejectProposal'
          | 'applyProposal'
          | 'rollbackProposal'
      ]('p-42')
      const request = only()
      expect(request.method).toBe('POST')
      expect(request.url.pathname).toBe(`/api/proposals/p-42/${segment}`)
      for (const other of forbidden) {
        expect(request.url.pathname).not.toContain(`/${other}`)
      }
    },
  )

  /**
   * =========================================================================
   * R24: `?auto` IS the difference between two writes, so it is tested on the
   * wire and not merely at the interface.
   * =========================================================================
   * `review.py::apply_endpoint` takes `auto: bool = Query(default=False)`:
   * `?auto=true` runs `recon.apply.auto_apply` — the ten-condition gate — and
   * no parameter runs `recon.apply.apply_proposal`, the reviewer's own write,
   * with no gate at all.
   *
   * This client sent NO `auto` parameter from anywhere, so the dashboard's only
   * apply button was the ungated path 100% of the time and R24's guarded
   * auto-apply was unreachable from the UI. Nothing was red: the manual path
   * 200s, which is exactly why the gap survived. These two tests are the wire
   * evidence that both paths are now reachable and distinguishable.
   */
  it('applyProposal("auto") asks for R24 with ?auto=true', async () => {
    installFetch(() => json(proposal({ status: 'applied' })))
    await httpClient.applyProposal('p-42', 'auto')
    const request = only()
    expect(request.method).toBe('POST')
    expect(request.url.pathname).toBe('/api/proposals/p-42/apply')
    expect(params(request)).toEqual({ auto: 'true' })
  })

  it('applyProposal("manual") — and the default — send NO auto parameter', async () => {
    // The ABSENT parameter is the endpoint's pinned default, so the manual
    // request stays byte-identical to the one this client always sent. Sending a
    // redundant `auto=false` would change the working path to decorate it.
    installFetch(() => json(proposal({ status: 'applied' })))
    await httpClient.applyProposal('p-42', 'manual')
    expect(params(only())).toEqual({})

    calls = []
    await httpClient.applyProposal('p-42')
    expect(params(calls[0])).toEqual({})
  })

  it('carries the refused gate verdict off the 409 problem document', async () => {
    // The refusal IS the safety property demonstrating itself, so the decision
    // has to survive the trip through `toProblem()` intact.
    installFetch(() =>
      json(
        {
          type: 'https://keystone.example/problems/auto-apply-refused',
          title: 'auto-apply refused',
          status: 409,
          detail: "R24's gate refused proposal 6102: confidence 0.9000 < 0.95 (R24)",
          auto_apply: {
            proposal_id: 6102,
            allowed: false,
            reason: 'confidence_floor',
            detail: 'confidence 0.9000 < 0.95 (R24)',
            checks: [
              {
                check: 'confidence_floor',
                passed: false,
                detail: 'confidence 0.9000 < 0.95 (R24)',
              },
            ],
          },
        },
        409,
      ),
    )
    const error = await httpClient
      .applyProposal('p-1', 'auto')
      .catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const problem = (error as ApiError).problem
    expect(problem.detail).toContain('confidence 0.9000 < 0.95 (R24)')
    expect(problem.auto_apply?.reason).toBe('confidence_floor')
    expect(problem.auto_apply?.checks).toHaveLength(1)
  })

  it('rollbackProposal hands back null for a body that is not a proposal row', async () => {
    // `POST …/rollback` has no pinned response shape: the service may answer
    // with the row, with `RollbackResult.as_dict` (digests, no row), or with
    // 204. The client must not mis-type any of those as a proposal.
    installFetch(() =>
      json({
        proposal_id: 6268,
        canonical_id: '84990991-6cb1-56b9-9511-0fae07ec1fa4',
        event_id: 91,
        byte_identical: true,
      }),
    )
    await expect(httpClient.rollbackProposal('p-1')).resolves.toBeNull()

    calls = []
    await expect(
      httpClient.rollbackProposal('p-1'),
    ).resolves.toBeNull()
  })

  it('rollbackProposal returns the row when the service does answer with one', async () => {
    installFetch(() => json(proposal({ status: 'rolled_back' })))
    await expect(httpClient.rollbackProposal('p-1')).resolves.toMatchObject({
      id: 'p-1',
      status: 'rolled_back',
    })
  })

  it('percent-encodes an id so it cannot escape its path segment', async () => {
    installFetch(() => json(conflict()))
    await httpClient.getConflict('a/b?c=d#e')
    expect(only().url.pathname).toBe('/api/conflicts/a%2Fb%3Fc%3Dd%23e')

    calls = []
    await httpClient.approveProposal('x y/z')
    expect(calls[0].url.pathname).toBe('/api/proposals/x%20y%2Fz/approve')
  })

  it('uses VITE_API_BASE_URL, with any trailing slashes removed', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.test///')
    installFetch(() => json(SCORECARD))
    await httpClient.getScorecard()
    expect(only().raw).toBe('https://api.example.test/api/scorecard')
  })

  it('falls back to a same-origin relative path when no base URL is set', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    installFetch(() => json(SCORECARD))
    await httpClient.getScorecard()
    expect(only().raw).toBe('/api/scorecard')
  })
})

// ---------------------------------------------------------------------------
// Headers / auth
// ---------------------------------------------------------------------------

describe('authentication headers', () => {
  it.each(METHODS)(
    '$name sends X-Api-Key from VITE_API_KEY, and asks for JSON',
    async ({ invoke, ok }) => {
      installFetch(() => json(ok))
      await invoke()
      const request = only()
      expect(request.headers['x-api-key']).toBe(KEY)
      expect(request.headers['accept']).toBe('application/json')
    },
  )

  it('sends the key VITE_API_KEY currently holds, not one captured at import', async () => {
    vi.stubEnv('VITE_API_KEY', 'rotated-key')
    installFetch(() => json(SCORECARD))
    await httpClient.getScorecard()
    expect(only().headers['x-api-key']).toBe('rotated-key')
  })

  it('sends no content-type on a body-less POST', async () => {
    installFetch(() => json(proposal()))
    await httpClient.approveProposal('p-1')
    const request = only()
    expect(request.body).toBeNull()
    expect(request.headers['content-type']).toBeUndefined()
  })

  it.each(METHODS)(
    '$name sends NOTHING when no API key is configured',
    async ({ invoke }) => {
      vi.stubEnv('VITE_API_KEY', '')
      installFetch(() => json({}))
      await expect(invoke()).rejects.toBeInstanceOf(ApiConfigError)
      // The request must never leave the browser: an unauthenticated call comes
      // back 401 and reads to a reviewer as "the service is broken".
      expect(calls).toHaveLength(0)
      expect(globalThis.fetch).not.toHaveBeenCalled()
    },
  )

  it('names the missing variable in the configuration error', async () => {
    vi.stubEnv('VITE_API_KEY', '')
    installFetch()
    await expect(httpClient.getScorecard()).rejects.toThrow(/VITE_API_KEY/)
  })
})

// ---------------------------------------------------------------------------
// Query-string encoding
// ---------------------------------------------------------------------------

describe('filter and pagination parameters', () => {
  it('serializes every conflicts filter under its contract name', async () => {
    installFetch(() =>
      json(page([conflict({ type: 'C14', sources: ['crm'], status: 'open' })])),
    )
    await httpClient.listConflicts({
      source: 'crm',
      type: 'C14',
      status: 'open',
      page: 3,
      page_size: 10,
    })
    expect(params(only())).toEqual({
      source: 'crm',
      type: 'C14',
      status: 'open',
      page: '3',
      page_size: '10',
    })
  })

  it('serializes every proposals filter, including conflict_id (A3)', async () => {
    installFetch(() => json(page([proposal({ conflict_id: 'c-9' })])))
    await httpClient.listProposals({
      source: 'payments',
      type: 'C2',
      status: 'sensitive_hold',
      conflict_id: 'c-9',
      page: 2,
      page_size: 5,
    })
    expect(params(only())).toEqual({
      source: 'payments',
      type: 'C2',
      status: 'sensitive_hold',
      conflict_id: 'c-9',
      page: '2',
      page_size: '5',
    })
  })

  it('omits absent filters entirely rather than sending empty values', async () => {
    installFetch(() => json(page([conflict()])))
    await httpClient.listConflicts({})
    const request = only()
    expect(request.raw).toBe(`${BASE}/api/conflicts`)
    expect(request.raw).not.toContain('?')
    expect(request.raw).not.toContain('undefined')
  })

  it('percent-encodes a filter value that contains URL syntax', async () => {
    installFetch(() => json(page([])))
    await httpClient.listConflicts({ status: 'escalated:oscillation' })
    const request = only()
    expect(request.raw).toContain('status=escalated%3Aoscillation')
    expect(request.url.searchParams.get('status')).toBe('escalated:oscillation')
  })

  it('clamps page_size to MAX_PAGE_SIZE — R11 forbids pulling the population', async () => {
    installFetch(() => json(page([])))
    await httpClient.listConflicts({ page_size: 100_000 })
    expect(params(only()).page_size).toBe(String(MAX_PAGE_SIZE))

    calls = []
    await httpClient.listProposals({ page_size: 100_000 })
    expect(calls[0].url.searchParams.get('page_size')).toBe(String(MAX_PAGE_SIZE))
  })

  it('clamps a zero or negative page_size up to 1, and truncates a fraction', async () => {
    installFetch(() => json(page([])))
    await httpClient.listConflicts({ page_size: 0 })
    expect(params(only()).page_size).toBe('1')

    calls = []
    await httpClient.listConflicts({ page_size: -20 })
    expect(calls[0].url.searchParams.get('page_size')).toBe('1')

    calls = []
    await httpClient.listConflicts({ page_size: 7.9 })
    expect(calls[0].url.searchParams.get('page_size')).toBe('7')
  })

  it('sends no page_size at all when the caller did not ask for one', async () => {
    installFetch(() => json(page([])))
    await httpClient.listConflicts({ page: 2 })
    expect(params(only())).toEqual({ page: '2' })
  })

  it('forwards the abort signal it was given', async () => {
    installFetch(() => json(page([conflict()])))
    const controller = new AbortController()
    await httpClient.listConflicts({}, controller.signal)
    expect(only().signal).toBe(controller.signal)
  })
})

// ---------------------------------------------------------------------------
// Response bodies
// ---------------------------------------------------------------------------

describe('successful responses', () => {
  it('returns the pagination envelope as the service sent it (A1)', async () => {
    const body = page([conflict({ id: 'c-7' })], {
      page: 4,
      page_size: 5,
      total: 3050,
    })
    installFetch(() => json(body))
    const result = await httpClient.listConflicts({ page: 4, page_size: 5 })
    expect(result.items.map((row) => row.id)).toEqual(['c-7'])
    expect(result.page).toBe(4)
    expect(result.page_size).toBe(5)
    expect(result.total).toBe(3050)
  })

  it('returns a single row for the per-id GETs (A2)', async () => {
    installFetch(() => json(conflict({ id: 'c-2' })))
    await expect(httpClient.getConflict('c-2')).resolves.toMatchObject({ id: 'c-2' })
    vi.unstubAllGlobals()
    installFetch(() => json(proposal({ id: 'p-2' })))
    await expect(httpClient.getProposal('p-2')).resolves.toMatchObject({ id: 'p-2' })
  })

  it('treats 204 No Content as a successful decision with no body', async () => {
    installFetch(() => new Response(null, { status: 204 }))
    await expect(httpClient.applyProposal('p-1')).resolves.toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// Error paths — the point is that NOTHING escapes as an untyped crash
// ---------------------------------------------------------------------------

describe('error responses become typed errors', () => {
  it('turns an RFC7807 4xx body into an ApiError carrying the problem', async () => {
    installFetch(() =>
      json(
        {
          type: 'https://keystone.example/problems/not-found',
          title: 'Not Found',
          status: 404,
          detail: 'No proposal with id p-404',
        },
        404,
      ),
    )
    const error = await httpClient.getProposal('p-404').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.problem).toEqual({
      type: 'https://keystone.example/problems/not-found',
      title: 'Not Found',
      status: 404,
      detail: 'No proposal with id p-404',
    })
    expect(apiError.message).toBe('404 Not Found')
  })

  it('surfaces a 409 from apply (approved-only / sensitive hold) as an ApiError', async () => {
    installFetch(() =>
      json(
        {
          type: 'https://keystone.example/problems/sensitive-hold',
          title: 'Conflict',
          status: 409,
          detail: 'Proposal targets a sensitive field.',
        },
        409,
      ),
    )
    const error = await httpClient.applyProposal('p-1').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).problem.detail).toContain('sensitive field')
  })

  it('turns a 422 validation body into an ApiError', async () => {
    installFetch(() =>
      json({ type: 'about:blank', title: 'Unprocessable Entity', status: 422 }, 422),
    )
    await expect(httpClient.listProposals({ page: -1 })).rejects.toBeInstanceOf(
      ApiError,
    )
  })

  it('still produces an ApiError when a 5xx body is not JSON at all', async () => {
    installFetch(
      () =>
        new Response('<html><body>502 Bad Gateway</body></html>', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: { 'content-type': 'text/html' },
        }),
    )
    const error = await httpClient.getScorecard().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(502)
    expect((error as ApiError).problem.title).toBe('Bad Gateway')
  })

  it.each(METHODS)('$name surfaces a 500 as a typed ApiError', async ({ invoke }) => {
    installFetch(
      () =>
        new Response('', { status: 500, statusText: 'Internal Server Error' }),
    )
    const error = await invoke().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toBeInstanceOf(KeystoneApiError)
    expect((error as ApiError).status).toBe(500)
  })

  it.each(METHODS)(
    '$name turns a transport failure into ApiNetworkError, not a bare TypeError',
    async ({ invoke }) => {
      installFetch(() => {
        throw new TypeError('Failed to fetch')
      })
      const error = await invoke().catch((e: unknown) => e)
      expect(error).toBeInstanceOf(ApiNetworkError)
      expect(error).not.toBeInstanceOf(ApiError)
      expect((error as ApiNetworkError).message).toContain(BASE)
      expect((error as ApiNetworkError).cause).toBeInstanceOf(TypeError)
    },
  )

  it.each(METHODS)(
    '$name turns a malformed 200 body into ApiParseError, not a SyntaxError',
    async ({ invoke }) => {
      installFetch(
        () =>
          new Response('{"items": [', {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
      )
      const error = await invoke().catch((e: unknown) => e)
      expect(error).toBeInstanceOf(ApiParseError)
      expect((error as ApiParseError).status).toBe(200)
      expect(error).not.toBeInstanceOf(SyntaxError)
    },
  )

  it('turns an HTML 200 (a login redirect, a proxy page) into ApiParseError', async () => {
    installFetch(
      () =>
        new Response('<!doctype html><title>Sign in</title>', {
          status: 200,
          headers: { 'content-type': 'text/html' },
        }),
    )
    await expect(httpClient.listConflicts({})).rejects.toBeInstanceOf(ApiParseError)
  })

  it('re-throws an AbortError untouched — a cancelled query is not a failure', async () => {
    const controller = new AbortController()
    installFetch(() => {
      controller.abort()
      throw Object.assign(new Error('The operation was aborted.'), {
        name: 'AbortError',
      })
    })
    const error = await httpClient
      .listConflicts({}, controller.signal)
      .catch((e: unknown) => e)
    expect((error as Error).name).toBe('AbortError')
    expect(error).not.toBeInstanceOf(ApiNetworkError)
    expect(error).not.toBeInstanceOf(KeystoneApiError)
  })
})

// ---------------------------------------------------------------------------
// Filter honesty (CONTRACT_ASSUMPTIONS A3 / A8)
// ---------------------------------------------------------------------------

describe('the client refuses to present an unfiltered page as a filtered one', () => {
  it('warns when /api/conflicts returns rows of the wrong type', async () => {
    installFetch(() => json(page([conflict({ type: 'C1' }), conflict({ type: 'C6' })])))
    const result = await httpClient.listConflicts({ type: 'C6' })
    const warning = result.warnings?.find((w) => w.param === 'type')
    expect(warning).toBeDefined()
    expect(warning?.kind).toBe('ignored')
    expect(warning?.detail).toContain('/api/conflicts')
    expect(warning?.detail).toContain('type=C6')
  })

  it('warns when /api/conflicts returns rows from a source it was not asked for', async () => {
    installFetch(() => json(page([conflict({ sources: ['payments'] })])))
    const result = await httpClient.listConflicts({ source: 'crm' })
    expect(result.warnings?.map((w) => w.param)).toEqual(['source'])
  })

  it('warns when /api/conflicts ignores the status filter', async () => {
    installFetch(() => json(page([conflict({ status: 'open' })])))
    const result = await httpClient.listConflicts({
      status: 'escalated:oscillation',
    })
    expect(result.warnings?.map((w) => w.param)).toEqual(['status'])
  })

  it('attaches no warnings at all when the response honours every filter', async () => {
    installFetch(() =>
      json(page([conflict({ type: 'C6', sources: ['crm'], status: 'open' })])),
    )
    const result = await httpClient.listConflicts({
      type: 'C6',
      source: 'crm',
      status: 'open',
    })
    expect(result.warnings).toBeUndefined()
  })

  it('warns that a proposals source/type filter is UNVERIFIABLE (A8)', async () => {
    // The rows look fine — they cannot look otherwise, because a proposal row
    // carries neither field. That is exactly the silent failure A8 names.
    installFetch(() => json(page([proposal(), proposal({ id: 'p-2' })])))
    const result = await httpClient.listProposals({ source: 'crm', type: 'C6' })
    const kinds = result.warnings?.map((w) => `${w.param}:${w.kind}`) ?? []
    expect(kinds).toContain('type:unverifiable')
    expect(kinds).toContain('source:unverifiable')
    expect(result.warnings?.every((w) => w.assumption === 'A8')).toBe(true)
    expect(result.warnings?.[0].detail).toMatch(/JOIN to conflicts/i)
  })

  it('warns about an unverifiable proposals filter even on an EMPTY page', async () => {
    // An empty filtered result is the most trusted screen there is: "there are
    // none of those". It must not be trusted when the filter may be a no-op.
    installFetch(() => json(page<Proposal>([])))
    const result = await httpClient.listProposals({ type: 'C14' })
    expect(result.warnings?.map((w) => w.param)).toEqual(['type'])
  })

  it('warns when /api/proposals returns a status it was not asked for', async () => {
    installFetch(() => json(page([proposal({ status: 'applied' })])))
    const result = await httpClient.listProposals({ status: 'pending' })
    expect(result.warnings?.map((w) => `${w.param}:${w.kind}`)).toEqual([
      'status:ignored',
    ])
  })

  it("warns when /api/proposals ignores conflict_id — the conflict detail's whole query (A3)", async () => {
    installFetch(() => json(page([proposal({ conflict_id: 'someone-else' })])))
    const result = await httpClient.listProposals({ conflict_id: 'c-1' })
    expect(result.warnings?.map((w) => `${w.param}:${w.kind}`)).toEqual([
      'conflict_id:ignored',
    ])
  })

  it('never lets the SERVICE put warnings on a page — they are the client’s verdict', async () => {
    const forged = {
      ...page([proposal()]),
      warnings: [
        {
          kind: 'ignored',
          endpoint: '/api/proposals',
          param: 'status',
          value: 'pending',
          assumption: 'A8',
          detail: 'forged by the service',
        },
      ],
    }
    installFetch(() => json(forged))
    const result = await httpClient.listProposals({ status: 'pending' })
    expect(result.warnings).toBeUndefined()
  })
})
