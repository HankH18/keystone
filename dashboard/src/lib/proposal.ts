/**
 * Small pure helpers about proposals and field paths. Kept out of component
 * files so they can be unit-tested directly and imported from anywhere.
 */
import { ApiConfigError, ApiError, ApiNetworkError, ApiParseError } from './api'
import { AUTO_APPLY_ELIGIBLE, SENSITIVE_FIELDS } from './contract'

/**
 * `action jsonb` has no pinned interior (DESIGN pins the column only), so the
 * target path is read DEFENSIVELY: absent or non-string means "no field write",
 * never a crash and never an assumption.
 */
export function targetPath(action: unknown): string | null {
  if (!action || typeof action !== 'object') return null
  const value = (action as Record<string, unknown>).target_path
  return typeof value === 'string' ? value : null
}

/** invariant-contract §6: sensitivity and auto-apply eligibility are allowlists. */
export function fieldClassification(path: string): string {
  if (SENSITIVE_FIELDS.has(path)) return 'sensitive'
  if (AUTO_APPLY_ELIGIBLE.has(path)) return 'auto-apply eligible'
  return 'not eligible for auto-apply'
}

/** Turn any thrown value into something a reviewer can act on. */
export function describeError(error: unknown): { title: string; detail: string } {
  if (error instanceof ApiError) {
    return {
      title: `${error.problem.status} ${error.problem.title}`,
      detail:
        error.problem.detail ??
        'The service returned an error. See the status above.',
    }
  }
  if (error instanceof ApiConfigError) {
    return { title: 'Dashboard not configured', detail: error.message }
  }
  if (error instanceof ApiNetworkError) {
    return {
      title: 'Service unreachable',
      detail: `${error.message}. The request never left the browser, so nothing was changed.`,
    }
  }
  if (error instanceof ApiParseError) {
    return {
      title: 'Unreadable response',
      detail: `${error.message}. The dashboard will not guess at a body it cannot parse.`,
    }
  }
  if (error instanceof Error) {
    return { title: 'Request failed', detail: error.message }
  }
  return { title: 'Request failed', detail: String(error) }
}
