/**
 * /proposals — the proposal list.
 *
 * Confidence is a tabular-numeral figure so the column is scannable; status is
 * icon + label + border treatment, never colour alone.
 */
import { useEffect, useMemo } from 'react'
import { createColumnHelper } from '@tanstack/react-table'
import { DataTable, type Columns, type TableFeatureSet } from '../components/DataTable'
import { Filters, Pagination } from '../components/Filters'
import {
  Confidence,
  ErrorState,
  FilterWarnings,
  Loading,
} from '../components/common'
import { useAnnounce } from '../lib/announcer'
import { actionShape, describeWritePaths, writePaths } from '../lib/proposal'
import { StatusBadge } from '../components/StatusBadge'
import { PageHeading } from '../components/PageHeading'
import { Link } from '../lib/router'
import { useListState } from '../lib/useListState'
import { useProposals } from '../lib/queries'
import {
  isConflictType,
  PROPOSAL_STATUSES,
  type ConflictType,
  type Proposal,
  type ProposalStatus,
  type SourceId,
} from '../lib/contract'

const helper = createColumnHelper<TableFeatureSet, Proposal>()

const columns: Columns<Proposal> = helper.columns([
  helper.display({
    id: 'proposal',
    header: 'Proposal',
    cell: ({ row }) => (
      <Link to={`/proposals/${encodeURIComponent(row.original.id)}`}>
        {row.original.id.slice(0, 8)}…
      </Link>
    ),
  }),
  helper.accessor('action', {
    id: 'target',
    header: 'Proposed fix',
    // A10: read off `action.set` (the only interior migration 0007 permits),
    // never off a `target_path` sibling the database refuses. Every path the
    // action writes is named, not just the first.
    cell: ({ getValue }) => {
      const action = getValue()
      const shape = actionShape(action)
      if (shape === 'unreadable') {
        return (
          <span className="evidence-empty" data-testid="fix-unreadable">
            unreadable action — not the committed {'{"set": …}'} shape
          </span>
        )
      }
      const paths = writePaths(action)
      return paths.length === 0 ? (
        <span data-testid="fix-evidence-only">
          evidence only — no field write
        </span>
      ) : (
        <span className="ref" data-testid="fix-target">
          {describeWritePaths(paths)}
        </span>
      )
    },
  }),
  helper.accessor('confidence', {
    header: 'Confidence',
    cell: ({ getValue }) => <Confidence value={getValue() as number} />,
  }),
  helper.accessor('sensitive', {
    header: 'Sensitive field',
    cell: ({ getValue }) =>
      (getValue() as boolean) ? (
        <span>Yes — auto-apply forbidden</span>
      ) : (
        <span>No</span>
      ),
  }),
  helper.accessor('status', {
    header: 'Status',
    cell: ({ getValue }) => (
      <StatusBadge status={getValue() as string} kind="proposal" />
    ),
  }),
  helper.accessor('conflict_id', {
    header: 'Conflict',
    cell: ({ getValue }) => (
      <Link to={`/conflicts/${encodeURIComponent(getValue() as string)}`}>
        {(getValue() as string).slice(0, 8)}…
      </Link>
    ),
  }),
  helper.accessor('decided_by', {
    header: 'Decided by',
    cell: ({ getValue }) =>
      (getValue() as string | null) ?? <span className="evidence-empty">—</span>,
  }),
])

function isProposalStatus(value: string): value is ProposalStatus {
  return (PROPOSAL_STATUSES as readonly string[]).includes(value)
}

export function ProposalsRoute() {
  const state = useListState()
  const announce = useAnnounce()

  const query = useMemo(
    () => ({
      source: state.source as SourceId | undefined,
      type: isConflictType(state.type ?? '') ? (state.type as ConflictType) : undefined,
      status: isProposalStatus(state.status ?? '')
        ? (state.status as ProposalStatus)
        : undefined,
      page: state.page,
      page_size: state.pageSize,
    }),
    [state.source, state.type, state.status, state.page, state.pageSize],
  )

  const proposals = useProposals(query)
  const data = proposals.data

  useEffect(() => {
    if (!data) return
    announce(
      `Proposals updated. Showing ${data.items.length} of ${data.total} matching proposals, page ${data.page}.`,
    )
  }, [data, announce])

  return (
    <>
      <PageHeading>Proposals</PageHeading>
      <p className="page-intro">
        One proposal per conflict. Proposals land <em>pending</em> or{' '}
        <em>held for human review</em>; nothing is written until a reviewer acts.
        Open a row for the evidence packet and the approve / reject actions.
      </p>

      <Filters
        idPrefix="proposals"
        statusKind="proposal"
        value={{ source: state.source, type: state.type, status: state.status }}
        onChange={state.setFilters}
      />

      {proposals.isPending ? <Loading what="proposals" /> : null}
      {proposals.isError ? (
        <ErrorState
          error={proposals.error}
          context="proposals"
          onRetry={() => void proposals.refetch()}
        />
      ) : null}

      {data ? (
        <>
          <FilterWarnings warnings={data.warnings} />
          <DataTable
            caption={`Proposals — ${data.total} match the current filters`}
            columns={columns}
            data={data.items}
            rowId={(row) => row.id}
            emptyMessage="No proposals match these filters."
          />
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            onPageChange={state.setPage}
            label="proposals"
          />
        </>
      ) : null}
    </>
  )
}
