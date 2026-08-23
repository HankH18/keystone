/**
 * The data layer: a client provider plus TanStack Query hooks.
 *
 * The provider exists so tests can inject a client explicitly. In the app it is
 * not given one and falls back to `getApiClient()` — the real HTTP client
 * unless VITE_USE_MOCK_API=1.
 */
import { createContext, useContext, useMemo, type ReactNode } from 'react'
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query'
import { getApiClient } from './apiClient'
import { guardConflictPage, guardProposalPage } from './filterGuard'
import type {
  Conflict,
  ConflictQuery,
  KeystoneApi,
  Page,
  Proposal,
  ProposalQuery,
  Scorecard,
} from './contract'

const ApiContext = createContext<Promise<KeystoneApi> | null>(null)

export function ApiProvider({
  client,
  children,
}: {
  client?: KeystoneApi | Promise<KeystoneApi>
  children: ReactNode
}) {
  const value = useMemo(
    () => (client ? Promise.resolve(client) : null),
    [client],
  )
  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>
}

export function useApi(): Promise<KeystoneApi> {
  return useContext(ApiContext) ?? getApiClient()
}

export function useConflicts(
  query: ConflictQuery,
): UseQueryResult<Page<Conflict>> {
  const api = useApi()
  return useQuery({
    queryKey: ['conflicts', query],
    // The filter check runs HERE as well as inside the HTTP client, so it
    // covers whatever client is in play — including the mock and anything a
    // test injects. It is pure and idempotent, so running it twice on the real
    // client's answer yields exactly the same verdict.
    queryFn: async ({ signal }) =>
      guardConflictPage(query, await (await api).listConflicts(query, signal)),
  })
}

export function useConflict(id: string): UseQueryResult<Conflict> {
  const api = useApi()
  return useQuery({
    queryKey: ['conflict', id],
    queryFn: async ({ signal }) => (await api).getConflict(id, signal),
  })
}

export function useProposals(
  query: ProposalQuery,
  options?: { enabled?: boolean },
): UseQueryResult<Page<Proposal>> {
  const api = useApi()
  return useQuery({
    queryKey: ['proposals', query],
    queryFn: async ({ signal }) =>
      guardProposalPage(query, await (await api).listProposals(query, signal)),
    enabled: options?.enabled ?? true,
  })
}

export function useProposal(id: string): UseQueryResult<Proposal> {
  const api = useApi()
  return useQuery({
    queryKey: ['proposal', id],
    queryFn: async ({ signal }) => (await api).getProposal(id, signal),
  })
}

export function useScorecard(): UseQueryResult<Scorecard> {
  const api = useApi()
  return useQuery({
    queryKey: ['scorecard'],
    queryFn: async ({ signal }) => (await api).getScorecard(signal),
  })
}

export type DecisionKind = 'approve' | 'reject' | 'apply'

/**
 * Approve / reject / apply.
 *
 * Optimistic: the proposal's cached row flips immediately so the reviewer sees
 * the effect of their keystroke, and every proposal/scorecard query is
 * invalidated on settle so the screen ends up showing what the SERVICE says,
 * not what we hoped. On failure the optimistic write is rolled back and the
 * caller renders the error.
 */
export function useDecision() {
  const api = useApi()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async ({ id, kind }: { id: string; kind: DecisionKind }) => {
      const client = await api
      if (kind === 'approve') return client.approveProposal(id)
      if (kind === 'reject') return client.rejectProposal(id)
      return client.applyProposal(id)
    },
    onMutate: async ({ id, kind }) => {
      await queryClient.cancelQueries({ queryKey: ['proposal', id] })
      const previous = queryClient.getQueryData<Proposal>(['proposal', id])
      if (previous) {
        const optimisticStatus =
          kind === 'approve'
            ? 'approved'
            : kind === 'reject'
              ? 'rejected'
              : 'applied'
        queryClient.setQueryData<Proposal>(['proposal', id], {
          ...previous,
          status: optimisticStatus,
        })
      }
      return { previous }
    },
    onError: (_error, { id }, context) => {
      if (context?.previous) {
        queryClient.setQueryData(['proposal', id], context.previous)
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['proposal'] })
      void queryClient.invalidateQueries({ queryKey: ['proposals'] })
      void queryClient.invalidateQueries({ queryKey: ['scorecard'] })
    },
  })
}
