/**
 * Client selection. THE REAL HTTP CLIENT IS THE DEFAULT.
 *
 * The in-browser mock (src/mocks/**) is loaded only when the build is given
 * VITE_USE_MOCK_API=1, and it is loaded through a dynamic import so it is not
 * even in the default bundle's entry chunk. Nothing outside src/mocks/**
 * imports the mock.
 *
 * `pnpm dev` and a plain `pnpm build` therefore talk to the real service and
 * show the error state until T-5/T-7/T-8 land it. `pnpm dev:mock` and the
 * Playwright a11y run set the flag.
 */
import type { KeystoneApi } from './contract'
import { httpClient } from './httpClient'

export const USE_MOCK_API: boolean =
  import.meta.env.VITE_USE_MOCK_API === '1' ||
  import.meta.env.VITE_USE_MOCK_API === 'true'

let resolved: Promise<KeystoneApi> | null = null

export function getApiClient(): Promise<KeystoneApi> {
  if (!resolved) {
    resolved = USE_MOCK_API
      ? import('../mocks/mockClient').then((m) => m.mockClient)
      : Promise.resolve(httpClient)
  }
  return resolved
}
