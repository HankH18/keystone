/**
 * /conflicts — the conflict list.
 *
 * One page of server-filtered, server-paginated rows. Every row shows the
 * type, its rule, the disagreeing SOURCES, the count of disagreeing FIELDS and
 * the status; the detail route shows the rest.
 */
import { useEffect, useMemo } from 'react'
import { createColumnHelper } from '@tanstack/react-table'
import { DataTable, type Columns, type TableFeatureSet } from '../components/DataTable'
import { Filters, Pagination } from '../components/Filters'
import { ErrorState, FilterWarnings, Loading } from '../components/common'
import { useAnnounce } from '../lib/announcer'
import { StatusBadge } from '../components/StatusBadge'
import { PageHeading } from '../components/PageHeading'
import { Link } from '../lib/router'
import { useListState } from '../lib/useListState'
import { useConflicts } from '../lib/queries'
import {
  CONFLICT_TYPE_LABEL,
  RULE_ID_BY_TYPE,
  SOURCE_LABEL,
  isConflictType,
  type Conflict,
  type ConflictType,
  type SourceId,
} from '../lib/contract'

const helper = createColumnHelper<TableFeatureSet, Conflict>()

const columns: Columns<Conflict> = helper.columns([
  helper.display({
    id: 'conflict',
    header: 'Conflict',
    cell: ({ row }) => (
      <Link to={`/conflicts/${encodeURIComponent(row.original.id)}`}>
        {row.original.type} — {CONFLICT_TYPE_LABEL[row.original.type] ?? 'Unknown type'}
      </Link>
    ),
  }),
  helper.accessor('type', {
    id: 'rule',
    header: 'Rule',
    cell: ({ getValue }) => (
      <span className="ref">{RULE_ID_BY_TYPE[getValue() as ConflictType] ?? '—'}</span>
    ),
  }),
  helper.accessor('sources', {
    header: 'Disagreeing sources',
    cell: ({ getValue }) =>
      (getValue() as SourceId[])
        .map((source) => SOURCE_LABEL[source] ?? source)
        .join(' + '),
  }),
  helper.accessor('disagreeing_fields', {
    header: 'Disagreeing fields',
    cell: ({ getValue }) => {
      const paths = getValue() as string[]
      if (paths.length === 0) return <span className="evidence-empty">none</span>
      return (
        <span className="ref">
          {paths.length} — {paths.join(', ')}
        </span>
      )
    },
  }),
  helper.accessor('entity_refs', {
    header: 'Records',
    cell: ({ getValue }) => (
      <span className="num">{(getValue() as string[]).length}</span>
    ),
  }),
  helper.accessor('status', {
    header: 'Status',
    cell: ({ getValue }) => (
      <StatusBadge status={getValue() as string} kind="conflict" />
    ),
  }),
  helper.accessor('fingerprint', {
    header: 'Fingerprint',
    cell: ({ getValue }) => (
      <span className="ref">{(getValue() as string).slice(0, 16)}…</span>
    ),
  }),
  helper.accessor('last_seen_run', {
    header: 'Last seen',
    cell: ({ getValue }) => <span className="ref">{getValue() as string}</span>,
  }),
])

export function ConflictsRoute() {
  const state = useListState()
  const announce = useAnnounce()

  const query = useMemo(
    () => ({
      source: state.source as SourceId | undefined,
      type: isConflictType(state.type ?? '') ? (state.type as ConflictType) : undefined,
      status: state.status,
      page: state.page,
      page_size: state.pageSize,
    }),
    [state.source, state.type, state.status, state.page, state.pageSize],
  )

  const conflicts = useConflicts(query)
  const data = conflicts.data

  useEffect(() => {
    if (!data) return
    announce(
      `Conflicts updated. Showing ${data.items.length} of ${data.total} matching conflicts, page ${data.page}.`,
    )
  }, [data, announce])

  return (
    <>
      <PageHeading>Conflicts</PageHeading>
      <p className="page-intro">
        Every conflict detected on the latest run, filtered and paginated by the
        service. Open a row for the disagreeing sources and fields, the evidence
        packet, and the proposal.
      </p>

      <Filters
        idPrefix="conflicts"
        statusKind="conflict"
        value={{ source: state.source, type: state.type, status: state.status }}
        onChange={state.setFilters}
      />

      {conflicts.isPending ? <Loading what="conflicts" /> : null}
      {conflicts.isError ? (
        <ErrorState
          error={conflicts.error}
          context="conflicts"
          onRetry={() => void conflicts.refetch()}
        />
      ) : null}

      {data ? (
        <>
          <FilterWarnings warnings={data.warnings} />
          <DataTable
            caption={`Conflicts — ${data.total} match the current filters`}
            columns={columns}
            data={data.items}
            rowId={(row) => row.id}
            emptyMessage="No conflicts match these filters."
          />
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            onPageChange={state.setPage}
            label="conflicts"
          />
        </>
      ) : null}
    </>
  )
}
