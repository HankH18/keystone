/**
 * Small pure helpers about proposals and field paths. Kept out of component
 * files so they can be unit-tested directly and imported from anywhere.
 */
import { ApiConfigError, ApiError, ApiNetworkError, ApiParseError } from './api'
import {
  AUTO_APPLY_ELIGIBLE,
  SENSITIVE_FIELDS,
  type AutoApplyCheck,
  type AutoApplyVerdict,
  type ProposalEvent,
  type RollbackReceipt,
} from './contract'

/**
 * One path a proposal's action would EFFECTIVELY write.
 *
 * `leaf` is the source-qualified contract path the §6 allow-lists are written
 * in. `container` is the top-level key of `action.set` the write reaches that
 * leaf THROUGH, or `null` when the leaf is itself a top-level key. The two are
 * separate because `entities.current` is not flat — it holds one nested object,
 * `survived` — so a gate that read top-level keys would judge `survived`
 * instead of the nine contract paths inside it. Mirrors
 * `recon.apply.WritePath`, including its `container->leaf` display form.
 */
export interface ProposalWritePath {
  leaf: string
  container: string | null
  /** `leaf`, or `container->leaf` when it is reached through one. */
  display: string
}

/**
 * What `proposals.action` IS, as far as the dashboard can tell.
 *
 * `writes`        — a committed `{"set": {...}}` action that lands on ≥1 path.
 * `evidence-only` — a committed `{"set": {}}`: the §6 evidence-only proposal.
 * `unreadable`    — anything else. Said out loud, never silently folded into
 *                   `evidence-only`: "this action names no field" and "this
 *                   action is in a shape I do not understand" are different
 *                   facts, and conflating them is how A10 shipped broken.
 */
export type ActionShape = 'writes' | 'evidence-only' | 'unreadable'

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * `action.set`, or `null` when the action is not in the committed vocabulary.
 *
 * ===========================================================================
 * The action vocabulary is a DATABASE CONSTRAINT, not a convention.
 * ===========================================================================
 * `migrations/versions/0007_action_content_binding.py` adds
 * `ck_proposals_action_vocabulary`, created VALIDATED so it binds existing rows:
 *
 *     CHECK (CASE WHEN jsonb_typeof(action) = 'object'
 *                 THEN jsonb_exists(action, 'set')
 *                      AND jsonb_typeof(action -> 'set') = 'object'
 *                      AND action - 'set' = '{}'::jsonb
 *                 ELSE false END)
 *
 * `action - 'set' = '{}'::jsonb` — remove `set` and nothing remains. Exactly
 * ONE top-level key, named `set`. This dashboard used to read
 * `action.target_path`, a sibling the database REFUSES, so it read `null` for
 * every proposal the service could write: every row rendered "evidence only —
 * no field write", and the R24 apply control could never appear.
 */
function actionSet(action: unknown): Record<string, unknown> | null {
  if (!isPlainObject(action)) return null
  const set = action.set
  return isPlainObject(set) ? set : null
}

/**
 * Every path `OLD.current || action->'set'` would write, worst case.
 *
 * This mirrors `recon.apply.effective_write_paths` called with NO entity row —
 * the conservative arm the service itself uses when it has none: every key the
 * action names, plus every member a nested assignment carries. The dashboard
 * has no canonical row to diff against and must never narrow the set on a
 * guess, so it takes the wide answer and says how it got there.
 *
 * Sorted by `display`, so the same action always renders the same way.
 */
export function writePaths(action: unknown): ProposalWritePath[] {
  const set = actionSet(action)
  if (!set) return []
  const paths: ProposalWritePath[] = []
  const push = (leaf: string, container: string | null) =>
    paths.push({
      leaf,
      container,
      display: container === null ? leaf : `${container}->${leaf}`,
    })

  for (const key of Object.keys(set).sort()) {
    const assigned = set[key]
    if (!isPlainObject(assigned)) {
      // A non-object assignment writes the key its author NAMED, whatever the
      // value is worth — including `null`, a list and a scalar erasing a map.
      push(key, null)
      continue
    }
    const members = Object.keys(assigned).sort()
    if (members.length === 0) {
      // An assigned-but-empty object writes nothing inside itself, so name the
      // container rather than reporting an empty set a `writes` check waves through.
      push(key, null)
      continue
    }
    for (const member of members) push(member, key)
  }
  // Code-unit order, NOT `localeCompare`: the collation of a locale-aware sort
  // is environment-dependent, and the same action must render identically in
  // every browser and in the test runner.
  return paths.sort((left, right) =>
    left.display < right.display ? -1 : left.display > right.display ? 1 : 0,
  )
}

/** Does this proposal's action land on any field at all? */
export function writesAField(action: unknown): boolean {
  return writePaths(action).length > 0
}

/** `writes` / `evidence-only` / `unreadable` — see {@link ActionShape}. */
export function actionShape(action: unknown): ActionShape {
  if (actionSet(action) === null) return 'unreadable'
  return writesAField(action) ? 'writes' : 'evidence-only'
}

/** The write set as one reviewer-readable phrase. Never only the first path. */
export function describeWritePaths(paths: readonly ProposalWritePath[]): string {
  if (paths.length === 0) return ''
  if (paths.length === 1) return `write ${paths[0].display}`
  return `write ${paths.length} fields: ${paths.map((path) => path.display).join(', ')}`
}

/** invariant-contract §6: sensitivity and auto-apply eligibility are allowlists. */
export function fieldClassification(path: string): string {
  if (SENSITIVE_FIELDS.has(path)) return 'sensitive'
  if (AUTO_APPLY_ELIGIBLE.has(path)) return 'auto-apply eligible'
  return 'not eligible for auto-apply'
}

/**
 * R24's verdict off a REFUSED auto-apply, or `null` if this is another failure.
 *
 * `review.py::apply_endpoint` answers a refused `?auto=true` with a 409 problem
 * document carrying `auto_apply` — the whole decision, every condition it
 * evaluated. That refusal is the safety property demonstrating itself, so the UI
 * shows the service's own reason rather than "request failed"; this function is
 * where the shape is checked once, defensively, instead of at the render site.
 *
 * Read from a 409 ONLY. Any other status carrying an `auto_apply` member is not
 * a gate refusal — an apply that 500s after the gate passed would otherwise be
 * rendered as "the gate said no", which is the wrong diagnosis and the wrong fix.
 */
export function refusedGate(error: unknown): AutoApplyVerdict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const verdict = error.problem.auto_apply
  if (typeof verdict !== 'object' || verdict === null) return null
  if (typeof verdict.reason !== 'string') return null
  return verdict
}

/** The conditions that did NOT hold. Never assumes `checks` is an array. */
export function failedChecks(verdict: AutoApplyVerdict): AutoApplyCheck[] {
  return Array.isArray(verdict.checks)
    ? verdict.checks.filter((check) => check && check.passed === false)
    : []
}

/**
 * Whether one ledger row recorded a before-image — i.e. whether that write can
 * still be reversed — in WORDS, because R12's bar is that a colourblind reviewer
 * loses nothing and a tick mark is not a sentence.
 *
 * Read off `before_digest`, and the derivation is the service's, not a guess:
 * `_PROPOSAL_EVENTS` computes it as `sha256(pe.before::text)` and SQL propagates
 * NULL, so a null digest means the row captured no before-image and that write
 * has nothing to restore from. A digest that is simply ABSENT is a third answer —
 * an older build that does not serve one — and is reported as unstated rather
 * than as "not reversible": absent evidence is not evidence of absence.
 */
export function reversibility(event: ProposalEvent): string {
  if (typeof event.before_digest === 'string' && event.before_digest !== '') {
    return 'before-image captured — reversible'
  }
  if (event.before_digest === null) return 'no before-image — NOT reversible'
  return 'not stated by this service build'
}

/**
 * The `rollback` member off a REFUSED reversal, or `null` for another failure.
 *
 * `review.py::_stale_reversal` answers 409 `rollback-not-on-top` when the
 * canonical row no longer holds what this proposal's apply left: a later apply
 * is on top, and reversing out of order would discard an approved, applied,
 * unreversed write. Like the auto-apply refusal, that is the product working, so
 * the service's own evidence is shown rather than "409 Conflict".
 *
 * Keyed on `on_top === false` specifically. The same member name carries the 200
 * receipt, and rendering a successful receipt as a refusal would be worse than
 * rendering neither.
 */
export function refusedRollback(error: unknown): RollbackReceipt | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const receipt = error.problem.rollback
  if (typeof receipt !== 'object' || receipt === null) return null
  return receipt.on_top === false ? receipt : null
}

/**
 * The `rollback` receipt off a SUCCESSFUL reversal body, or `null`.
 *
 * Takes `unknown` because the endpoint's 200 body is the proposal row with two
 * extra members, and the client hands the row back typed as a `Proposal` — the
 * extras ride along at runtime but are not on that type. Reading them here, once,
 * defensively, is better than widening `Proposal` with a member that only ever
 * appears on one response.
 */
export function rollbackReceipt(body: unknown): RollbackReceipt | null {
  if (typeof body !== 'object' || body === null) return null
  const receipt = (body as { rollback?: unknown }).rollback
  if (typeof receipt !== 'object' || receipt === null) return null
  return receipt as RollbackReceipt
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
