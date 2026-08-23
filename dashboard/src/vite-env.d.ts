/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Keystone service API, e.g. `http://localhost:8000`. */
  readonly VITE_API_BASE_URL?: string
  /** Demo API key sent as the `X-Api-Key` header. */
  readonly VITE_API_KEY?: string
  /**
   * Set to `1` to swap the real HTTP client for the in-browser mock in
   * src/mocks/**. UNSET (the default) means the dashboard talks to the real
   * service. See src/lib/apiClient.ts.
   */
  readonly VITE_USE_MOCK_API?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
