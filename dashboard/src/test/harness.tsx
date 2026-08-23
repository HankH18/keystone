/**
 * Test harness: render the real App, at a real URL, against an injected client.
 *
 * Nothing here stubs a component or a route. The tests drive the same
 * `<App>` the browser gets; only the API client is swapped, and it is swapped
 * for a client that implements the same `KeystoneApi` interface the real HTTP
 * client implements.
 */
import { render, type RenderResult } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import App from '../App'
import { Router } from '../lib/router'
import { ApiProvider } from '../lib/queries'
import {
  buildMockDataset,
  createMockClient,
  type MockDataset,
} from '../mocks/mockClient'
import type { KeystoneApi } from '../lib/contract'

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, gcTime: 0 },
      mutations: { retry: false },
    },
  })
}

export function renderWithClient(
  ui: ReactElement,
  client: KeystoneApi,
): RenderResult {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <ApiProvider client={client}>
        <Router>{ui}</Router>
      </ApiProvider>
    </QueryClientProvider>,
  )
}

export function renderApp(client: KeystoneApi, url = '/'): RenderResult {
  window.history.replaceState({}, '', url)
  return renderWithClient(<App />, client)
}

/** A golden entry, as `scripts/build-mock-seed.mjs` writes it. */
export interface SeedEntry {
  type: string
  rule_id: string
  entity_refs: string[]
  sources_involved: string[]
  disagreeing_fields: string[]
  observed_values: Record<string, unknown>
  oscillating: boolean
}

/**
 * A small dataset built by the SAME code path as the full golden seed, so a
 * test exercises the real derivation (fingerprint, fix target, sensitivity)
 * rather than a hand-written fixture that could drift from it.
 */
export function makeClient(entries: SeedEntry[]): KeystoneApi {
  return createMockClient(() => buildMockDataset(entries))
}

export function makeDataset(entries: SeedEntry[]): Promise<MockDataset> {
  return buildMockDataset(entries)
}

/** A C6 grade-only entry: non-sensitive, eligible target `crm.contact.grade`. */
export function gradeConflict(index: number): SeedEntry {
  return {
    type: 'C6',
    rule_id: 'R-006',
    entity_refs: [
      `appdb:student:student-${index}`,
      `crm:contact:CRM-${String(index).padStart(6, '0')}`,
    ],
    sources_involved: ['appdb', 'crm'],
    disagreeing_fields: ['appdb.student.grade', 'crm.contact.grade'],
    observed_values: {
      'appdb.student.grade': 'Grade 4',
      'crm.contact.grade': '5',
    },
    oscillating: false,
  }
}

/** A C14 name-only entry: wholly sensitive, so `sensitive_hold`. */
export function nameConflict(index: number): SeedEntry {
  return {
    type: 'C14',
    rule_id: 'R-014',
    entity_refs: [
      `appdb:student:sensitive-${index}`,
      `crm:contact:CRM-S${String(index).padStart(5, '0')}`,
    ],
    sources_involved: ['appdb', 'crm'],
    disagreeing_fields: [
      'appdb.student.first_name',
      'appdb.student.last_name',
      'crm.contact.first_name',
      'crm.contact.last_name',
    ],
    observed_values: {
      'appdb.student.first_name': 'galeav',
      'appdb.student.last_name': 'jarrow-gray',
      'crm.contact.first_name': 'joraui',
      'crm.contact.last_name': 'ellery-hart',
    },
    oscillating: false,
  }
}

/** A C1 entry: no committed fix template writes a field — evidence-only. */
export function evidenceOnlyConflict(index: number): SeedEntry {
  return {
    type: 'C1',
    rule_id: 'R-001',
    entity_refs: [
      `appdb:student:paid-no-deal-${index}`,
      `crm:contact:CRM-P${String(index).padStart(5, '0')}`,
    ],
    sources_involved: ['appdb', 'crm'],
    disagreeing_fields: [],
    observed_values: {
      d2_deal_count: 0,
      enrollment_ref: `appdb:enrollment:e-${index}`,
      paid_payment_refs: [`payments:payment:pi_${index}`],
    },
    oscillating: false,
  }
}

/** A C2 payments-only entry: eligible linkage target, single source. */
export function paymentConflict(index: number): SeedEntry {
  return {
    type: 'C2',
    rule_id: 'R-002',
    entity_refs: [`payments:payment:pi_${String(index).padStart(7, '0')}`],
    sources_involved: ['payments'],
    disagreeing_fields: [],
    observed_values: {
      external_ref: null,
      metadata_name_pair_present: false,
      payer_email_norm: `orphan-${index}@brightmail.example`,
    },
    oscillating: false,
  }
}
