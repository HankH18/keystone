/**
 * Typed fetch wrapper for the Keystone client API (T-0 stub).
 *
 * Per DESIGN §Dashboard ↔ API the dashboard talks to the HTTP API only —
 * never to Postgres — and authenticates with the committed admin demo key.
 * Endpoint helpers (`/api/conflicts`, `/api/proposals`, …) are deliberately
 * NOT defined here: they land with T-10 once T-7 pins the response shapes.
 */

/** RFC7807-style problem document returned by the service on error. */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail?: string
}

/** Error thrown for any non-2xx response. Carries the parsed problem body. */
export class ApiError extends Error {
  readonly status: number
  readonly problem: ProblemDetail

  constructor(problem: ProblemDetail) {
    super(`${problem.status} ${problem.title}`)
    this.name = 'ApiError'
    this.status = problem.status
    this.problem = problem
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

/** Base URL of the service API, e.g. `http://localhost:8000`. No trailing slash. */
export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? ''
).replace(/\/+$/, '')

/** Demo API key sent as `X-Api-Key`. Admin scope; see DESIGN §HTTP API. */
export const API_KEY: string = import.meta.env.VITE_API_KEY ?? ''

function buildUrl(
  path: string,
  query?: ApiRequestOptions['query'],
): string {
  const normalized = path.startsWith('/') ? path : `/${path}`
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) search.set(key, String(value))
  }
  const qs = search.toString()
  return `${API_BASE_URL}${normalized}${qs ? `?${qs}` : ''}`
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

/**
 * Perform a request against the client API.
 *
 * @throws {ApiError} on any non-2xx response.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { method = 'GET', query, body, signal } = options

  const headers: Record<string, string> = {
    accept: 'application/json',
    'X-Api-Key': API_KEY,
  }
  if (body !== undefined) headers['content-type'] = 'application/json'

  const response = await fetch(buildUrl(path, query), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (!response.ok) throw new ApiError(await toProblem(response))
  if (response.status === 204) return undefined as T

  return (await response.json()) as T
}
