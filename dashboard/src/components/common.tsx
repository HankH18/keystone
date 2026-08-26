/**
 * Small shared presentational pieces: the error state, the evidence renderer,
 * the confidence figure, and the reference/field lists.
 */
import {
  COMPARED_FIELDS,
  SOURCE_LABEL,
  parseRef,
  type FilterWarning,
  type SourceId,
} from '../lib/contract'
import { describeError, fieldClassification } from '../lib/proposal'
import { Button } from './Button'

/**
 * A visible, announced, keyboard-reachable error. `role="alert"` so it is read
 * the moment it appears; a retry button so the reviewer is not stuck.
 */
export function ErrorState({
  error,
  onRetry,
  context,
}: {
  error: unknown
  onRetry?: () => void
  context: string
}) {
  const { title, detail } = describeError(error)
  return (
    <div className="alert" role="alert" data-testid="error-state">
      <h2>Could not load {context}</h2>
      <p>
        <strong>{title}</strong> — {detail}
      </p>
      {onRetry ? <Button onClick={onRetry}>Retry</Button> : null}
    </div>
  )
}

/**
 * Filters the service did not demonstrably apply (src/lib/filterGuard.ts).
 *
 * Loud on purpose. The alternative — a filtered heading over unfiltered rows —
 * is a reviewer approving a fix they never asked to see. `role="alert"` so it
 * is announced the moment the page repaints, not only seen.
 */
export function FilterWarnings({
  warnings,
}: {
  warnings?: readonly FilterWarning[]
}) {
  if (!warnings || warnings.length === 0) return null
  return (
    <div className="alert" role="alert" data-testid="filter-warning">
      <h2>These results may not be filtered</h2>
      <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
        {warnings.map((warning) => (
          <li
            key={`${warning.endpoint}:${warning.param}:${warning.kind}`}
            data-testid={`filter-warning-${warning.param}`}
            data-warning-kind={warning.kind}
          >
            {warning.detail}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function Loading({ what }: { what: string }) {
  return <p data-testid="loading">Loading {what}…</p>
}

// ---------------------------------------------------------------------------
// Figures
// ---------------------------------------------------------------------------

/**
 * Confidence as a tabular-numeral figure (R11). Two decimals always, so the
 * column is scannable and 0.90 does not read shorter than 0.895.
 */
export function Confidence({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <span className="num">—</span>
  }
  return (
    <span className="num" data-testid="confidence">
      {value.toFixed(2)}
    </span>
  )
}

export function Fingerprint({ value }: { value: string }) {
  return (
    <span className="ref" data-testid="fingerprint" title={value}>
      {value}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Sources / refs / fields
// ---------------------------------------------------------------------------

export function SourceList({ sources }: { sources: SourceId[] }) {
  if (sources.length === 0) return <span className="evidence-empty">none</span>
  return (
    <>
      {sources
        .map((source) => SOURCE_LABEL[source] ?? source)
        .join(' + ')}
    </>
  )
}

/** Record references, `{source}:{entity_type}:{natural_key}` (contract §5.4). */
export function RefList({ refs }: { refs: string[] }) {
  if (refs.length === 0) return <span className="evidence-empty">none</span>
  return (
    <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
      {refs.map((ref) => {
        const parsed = parseRef(ref)
        return (
          <li key={ref} className="ref">
            <strong>{SOURCE_LABEL[parsed.source as SourceId] ?? parsed.source}</strong>{' '}
            {parsed.entityType} <span>{parsed.key}</span>
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The disagreeing FIELDS, in the contract's own vocabulary: source-qualified
 * paths, grouped back into the COMPARED_FIELDS comparison rows (§2.4) so the
 * reviewer sees which two paths disagreed with each other rather than a flat
 * list, plus each path's §6 sensitivity classification.
 */
export function DisagreeingFields({
  paths,
  observed,
}: {
  paths: string[]
  observed?: Record<string, unknown>
}) {
  if (paths.length === 0) {
    return (
      <p className="evidence-empty" data-testid="no-disagreeing-fields">
        No disagreeing fields — only R-006 and R-014 populate this list
        (invariant-contract §2.4).
      </p>
    )
  }
  const present = new Set(paths)
  const rows = COMPARED_FIELDS.filter(
    (row) => present.has(row.left) || present.has(row.right),
  )
  const covered = new Set(rows.flatMap((row) => [row.left, row.right]))
  const leftovers = paths.filter((path) => !covered.has(path))

  return (
    <div className="table-scroll">
      <table className="data-table" data-testid="disagreeing-fields">
        <caption>
          Disagreeing fields, as source-qualified paths from COMPARED_FIELDS.
        </caption>
        <thead>
          <tr>
            <th scope="col">Comparison</th>
            <th scope="col">CRM-side path</th>
            <th scope="col">App-DB-side path</th>
            <th scope="col">Observed values</th>
            <th scope="col">Classification</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.logical}>
              <th scope="row">{row.logical}</th>
              <td className="ref">{row.left}</td>
              <td className="ref">{row.right}</td>
              <td className="ref">
                {observed
                  ? `${formatScalar(observed[row.left])} ≠ ${formatScalar(observed[row.right])}`
                  : '—'}
              </td>
              {/*
                This cell used to render `left / right` as one bare string, so a
                reviewer read "auto-apply eligible / sensitive" with nothing
                saying WHICH side was the sensitive one. Sensitivity is the
                safety property this whole table exists to surface -- a reader
                who takes the first half at face value draws exactly the wrong
                conclusion about whether the fix may be automated. Each side now
                names its own source.
              */}
              <td>
                <div>CRM — {fieldClassification(row.left)}</div>
                <div>App DB — {fieldClassification(row.right)}</div>
              </td>
            </tr>
          ))}
          {leftovers.map((path) => (
            <tr key={path}>
              <th scope="row">other</th>
              <td className="ref" colSpan={2}>
                {path}
              </td>
              <td className="ref">
                {observed ? formatScalar(observed[path]) : '—'}
              </td>
              <td>{fieldClassification(path)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

// ---------------------------------------------------------------------------
// Evidence packet
// ---------------------------------------------------------------------------

/**
 * The evidence packet is `evidence jsonb` — DESIGN pins the column, not its
 * interior. So it is rendered GENERICALLY, key by key, whatever the service
 * puts there. Nothing in this component depends on a particular key existing.
 */
export function EvidencePacket({
  evidence,
  testId = 'evidence-packet',
}: {
  evidence: unknown
  testId?: string
}) {
  if (evidence === null || evidence === undefined) {
    return <p className="evidence-empty">No evidence packet on this proposal.</p>
  }
  return (
    <div data-testid={testId}>
      <JsonNode value={evidence} />
    </div>
  )
}

function JsonNode({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="evidence-value">null</span>
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="evidence-empty">empty list</span>
    return (
      <ul className="evidence-tree">
        {value.map((item, index) => (
          <li key={index}>
            <JsonNode value={item} />
          </li>
        ))}
      </ul>
    )
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="evidence-empty">empty</span>
    return (
      <dl className="kv">
        {entries.map(([key, child]) => (
          <div key={key} style={{ display: 'contents' }}>
            <dt className="evidence-key">{key}</dt>
            <dd>
              <JsonNode value={child} />
            </dd>
          </div>
        ))}
      </dl>
    )
  }
  return <span className="evidence-value">{String(value)}</span>
}
