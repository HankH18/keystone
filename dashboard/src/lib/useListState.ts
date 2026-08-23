/**
 * List state (filters + page) read from and written to the URL query string.
 *
 * Server-side by construction: this hook produces the REQUEST parameters. It
 * never holds the rows, so there is no code path on which a full 100k result
 * set could reach the client (R11's explicit non-goal).
 */
import { useCallback, useMemo } from 'react'
import { useQueryParams } from './router'
import { DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from './contract'

export interface ListState {
  source?: string
  type?: string
  status?: string
  page: number
  pageSize: number
}

export function useListState(): ListState & {
  setFilters: (next: { source?: string; type?: string; status?: string }) => void
  setPage: (page: number) => void
} {
  const { params, setParams } = useQueryParams()

  const state = useMemo<ListState>(() => {
    const rawPage = Number.parseInt(params.get('page') ?? '1', 10)
    const rawSize = Number.parseInt(
      params.get('page_size') ?? String(DEFAULT_PAGE_SIZE),
      10,
    )
    return {
      source: params.get('source') ?? undefined,
      type: params.get('type') ?? undefined,
      status: params.get('status') ?? undefined,
      page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
      pageSize:
        Number.isFinite(rawSize) && rawSize > 0
          ? Math.min(rawSize, MAX_PAGE_SIZE)
          : DEFAULT_PAGE_SIZE,
    }
  }, [params])

  const setFilters = useCallback(
    (next: { source?: string; type?: string; status?: string }) => {
      // Changing a filter always returns to page 1.
      setParams({
        source: next.source,
        type: next.type,
        status: next.status,
        page: undefined,
      })
    },
    [setParams],
  )

  const setPage = useCallback(
    (page: number) => setParams({ page: page <= 1 ? undefined : String(page) }),
    [setParams],
  )

  return { ...state, setFilters, setPage }
}
