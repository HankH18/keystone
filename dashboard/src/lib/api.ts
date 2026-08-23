/**
 * Typed fetch wrapper for the Keystone client API.
 *
 * Per DESIGN §Dashboard ↔ API the dashboard talks to the HTTP API only —
 * never to Postgres — and authenticates with the committed admin demo key.
 *
 * EVERY failure mode leaves this module as a TYPED error, never as a raw
 * TypeError from `fetch` and never as a SyntaxError from `response.json()`:
 *   - non-2xx                  → `ApiError`        (carries the RFC7807 body)
 *   - transport failure        → `ApiNetworkError`  (DNS, refused, CORS, offline)
 *   - 2xx with a non-JSON body → `ApiParseError`
 *   - no `VITE_API_KEY`        → `ApiConfigError`, and the request is NOT sent
 * An `AbortError` is re-thrown untouched, because TanStack Query's own
 * cancellation depends on recognising it.
 */

/** RFC7807-style problem document returned by the service on error. */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail?: string
}

/** Base class of every error this module raises. */
export class KeystoneApiError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'KeystoneApiError'
  }
}

/** Error thrown for any non-2xx response. Carries the parsed problem body. */
export class ApiError extends KeystoneApiError {
  readonly status: number
  readonly problem: ProblemDetail

  constructor(problem: ProblemDetail) {
    super(`${problem.status} ${problem.title}`)
    this.name = 'ApiError'
    this.status = problem.status
    this.problem = problem
  }
}

/**
 * The request never reached the service (offline, DNS, connection refused,
 * TLS, CORS preflight). `fetch` reports all of these as a bare `TypeError`
 * with no useful message, so the URL is attached here.
 */
export class ApiNetworkError extends KeystoneApiError {
  readonly url: string

  constructor(url: string, cause: unknown) {
    super(`Could not reach the Keystone service at ${url}`, { cause })
    this.name = 'ApiNetworkError'
    this.url = url
  }
}

/** The service answered 2xx with a body that is not the JSON we can read. */
export class ApiParseError extends KeystoneApiError {
  readonly url: string
  readonly status: number

  constructor(url: string, status: number, cause: unknown) {
    super(
      `The Keystone service returned ${status} with a body that is not valid JSON (${url})`,
      { cause },
    )
    this.name = 'ApiParseError'
    this.url = url
    this.status = status
  }
}

/**
 * The dashboard is misconfigured. Raised BEFORE any request is made: an
 * unauthenticated call would come back 401/403 and read to a reviewer as "the
 * service is broken", which is the wrong diagnosis and the wrong fix.
 */
export class ApiConfigError extends KeystoneApiError {
  constructor(message: string) {
    super(message)
    this.name = 'ApiConfigError'
  }
}

export interface ApiRequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** Query-string params; `undefined` values are dropped. */
  query?: Record<string, string | number | boolean | undefined>
  /** JSON request body; serialized and sent with `content-type: application/json`. */
  body?: unknown
  signal?: AbortSignal
}

/**
 * Base URL of the service API, e.g. `http://localhost:8000`. No trailing slash.
 *
 * Read at CALL time rather than captured at module load: the value is static in
 * a built bundle either way, and a function is the only shape a test can vary
 * without re-importing the whole module graph.
 */
export function apiBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')
}

/** Demo API key sent as `X-Api-Key`. Admin scope; see DESIGN §HTTP API. */
export function apiKey(): string {
  return import.meta.env.VITE_API_KEY ?? ''
}

export function buildUrl(
  path: string,
  query?: ApiRequestOptions['query'],
): string {
  const normalized = path.startsWith('/') ? path : `/${path}`
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) search.set(key, String(value))
  }
  const qs = search.toString()
  return `${apiBaseUrl()}${normalized}${qs ? `?${qs}` : ''}`
}

async function toProblem(response: Response): Promise<ProblemDetail> {
  const fallback: ProblemDetail = {
    type: 'about:blank',
    title: response.statusText || 'Request failed',
    status: response.status,
  }
  try {
    const parsed: unknown = await response.json()
    if (parsed && typeof parsed === 'object') {
      return { ...fallback, ...(parsed as Partial<ProblemDetail>) }
    }
  } catch {
    // Non-JSON error body: fall through to the status-derived problem.
  }
  return fallback
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'AbortError'
  )
}

/**
 * Perform a request against the client API.
 *
 * @throws {ApiConfigError} when no API key is configured (nothing is sent).
 * @throws {ApiNetworkError} when the request never reached the service.
 * @throws {ApiError} on any non-2xx response.
 * @throws {ApiParseError} on a 2xx response whose body is not JSON.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { method = 'GET', query, body, signal } = options
  const url = buildUrl(path, query)
  const key = apiKey()

  if (!key) {
    throw new ApiConfigError(
      'No API key is configured, so the request was not sent. Set VITE_API_KEY ' +
        'to the committed admin demo key (DESIGN §Dashboard ↔ API). The dashboard ' +
        'never calls the service unauthenticated.',
    )
  }

  const headers: Record<string, string> = {
    accept: 'application/json',
    'X-Api-Key': key,
  }
  if (body !== undefined) headers['content-type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    // A cancelled query is not a failure; TanStack Query needs the AbortError.
    if (isAbort(error, signal)) throw error
    throw new ApiNetworkError(url, error)
  }

  if (!response.ok) throw new ApiError(await toProblem(response))
  if (response.status === 204) return undefined as T

  try {
    return (await response.json()) as T
  } catch (error) {
    throw new ApiParseError(url, response.status, error)
  }
}
