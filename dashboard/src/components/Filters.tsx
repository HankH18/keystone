/**
 * Filter and pagination controls.
 *
 * The filter state lives in the URL query string, so a filtered view is
 * shareable, survives reload, and is reachable by a Playwright test without
 * simulating clicks. Every request that changes filters resets to page 1 —
 * page 7 of a list you just narrowed to 3 rows is a blank screen.
 *
 * Every control has a real `<label for>`; none relies on placeholder text.
 */
import {
  CONFLICT_TYPES,
  CONFLICT_TYPE_LABEL,
  PROPOSAL_STATUSES,
  SOURCE_IDS,
  SOURCE_LABEL,
  type ConflictType,
} from '../lib/contract'
import { CONFLICT_STATUS_META, PROPOSAL_STATUS_META } from '../lib/statusMeta'
import { Button } from './Button'

export interface FilterValue {
  source?: string
  type?: string
  status?: string
}

export interface FiltersProps {
  value: FilterValue
  onChange: (next: FilterValue) => void
  /** Which status vocabulary the status <select> offers. */
  statusKind: 'proposal' | 'conflict'
  idPrefix: string
}

export function Filters({ value, onChange, statusKind, idPrefix }: FiltersProps) {
  const statusOptions =
    statusKind === 'proposal'
      ? PROPOSAL_STATUSES.map((status) => ({
          value: status,
          label: PROPOSAL_STATUS_META[status].label,
        }))
      : Object.entries(CONFLICT_STATUS_META).map(([status, meta]) => ({
          value: status,
          label: meta.label,
        }))

  return (
    <div className="filters">
      <div className="field">
        <label htmlFor={`${idPrefix}-source`}>Source</label>
        <select
          id={`${idPrefix}-source`}
          value={value.source ?? ''}
          onChange={(event) =>
            onChange({ ...value, source: event.target.value || undefined })
          }
        >
          <option value="">All sources</option>
          {SOURCE_IDS.map((source) => (
            <option key={source} value={source}>
              {SOURCE_LABEL[source]}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor={`${idPrefix}-type`}>Conflict type</label>
        <select
          id={`${idPrefix}-type`}
          value={value.type ?? ''}
          onChange={(event) =>
            onChange({ ...value, type: event.target.value || undefined })
          }
        >
          <option value="">All types</option>
          {CONFLICT_TYPES.map((type: ConflictType) => (
            <option key={type} value={type}>
              {type} — {CONFLICT_TYPE_LABEL[type]}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor={`${idPrefix}-status`}>Status</label>
        <select
          id={`${idPrefix}-status`}
          value={value.status ?? ''}
          onChange={(event) =>
            onChange({ ...value, status: event.target.value || undefined })
          }
        >
          <option value="">All statuses</option>
          {statusOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <Button
        onClick={() => onChange({})}
        inert={!value.source && !value.type && !value.status}
      >
        Clear filters
      </Button>
    </div>
  )
}

export interface PaginationProps {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
  /** What is being paged, for the accessible names. */
  label: string
}

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  label,
}: PaginationProps) {
  const lastPage = Math.max(1, Math.ceil(total / pageSize))
  const first = total === 0 ? 0 : (page - 1) * pageSize + 1
  const last = Math.min(page * pageSize, total)

  return (
    <nav className="pagination" aria-label={`${label} pagination`}>
      <Button onClick={() => onPageChange(page - 1)} inert={page <= 1}>
        Previous page
      </Button>
      <Button onClick={() => onPageChange(page + 1)} inert={page >= lastPage}>
        Next page
      </Button>
      <p className="pagination-status num" data-testid="pagination-status">
        Showing {first}–{last} of {total} {label}. Page {page} of {lastPage}.
      </p>
    </nav>
  )
}
