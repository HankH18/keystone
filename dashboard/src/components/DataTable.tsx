/**
 * The table.
 *
 * TanStack Table **v9** (the version this repo actually resolves — v9's
 * `useTable` + `tableFeatures` + `table.FlexRender`, NOT v8's `useReactTable` +
 * `getCoreRowModel`). Only the core feature set is registered: sorting,
 * filtering and pagination are all SERVER-side (R11 forbids loading 100k rows
 * into the client), so registering client-side row models here would be dead
 * weight that also lies about where the work happens.
 *
 * Semantics: a real `<table>` with a `<caption>`, `<th scope="col">` headers and
 * a `<th scope="row">` first cell per row, inside a focusable scroll container
 * so a keyboard user can scroll a wide table without a mouse.
 */
import { tableFeatures, useTable } from '@tanstack/react-table'
import type { ColumnDef, RowData } from '@tanstack/react-table'

// The registered feature set is a module-level constant by design (v9 requires
// a stable `features` object); it is not a component, so Fast Refresh's
// one-export-kind-per-file rule does not apply.
// eslint-disable-next-line react-refresh/only-export-components
export const tableFeatureSet = tableFeatures({})
export type TableFeatureSet = typeof tableFeatureSet

// `any` is TanStack's own column-value parameter here: `ColumnHelper.columns`
// returns `Array<ColumnDef<TFeatures, TData, any>>`, and narrowing it to
// `unknown` makes every heterogeneous column list unassignable.
export type Columns<TData extends RowData> = Array<
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ColumnDef<TableFeatureSet, TData, any>
>

export interface DataTableProps<TData extends RowData> {
  columns: Columns<TData>
  data: TData[]
  /** Required: a table with no caption is a table a screen reader cannot place. */
  caption: string
  /** Stable row id, used as the React key. */
  rowId: (row: TData) => string
  emptyMessage: string
}

export function DataTable<TData extends RowData>({
  columns,
  data,
  caption,
  rowId,
  emptyMessage,
}: DataTableProps<TData>) {
  const table = useTable({ features: tableFeatureSet, columns, data })

  return (
    <div
      className="table-scroll"
      tabIndex={0}
      role="region"
      aria-label={caption}
    >
      <table className="data-table">
        <caption>{caption}</caption>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th key={header.id} scope="col">
                  {header.isPlaceholder ? null : (
                    <table.FlexRender header={header} />
                  )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr>
              <td colSpan={columns.length}>{emptyMessage}</td>
            </tr>
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr key={rowId(row.original)}>
                {row.getAllCells().map((cell, index) =>
                  index === 0 ? (
                    <th key={cell.id} scope="row">
                      <table.FlexRender cell={cell} />
                    </th>
                  ) : (
                    <td key={cell.id}>
                      <table.FlexRender cell={cell} />
                    </td>
                  ),
                )}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
