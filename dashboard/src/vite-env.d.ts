/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Keystone service API, e.g. `http://localhost:8000`. */
  readonly VITE_API_BASE_URL?: string
  /** Demo API key sent as the `X-Api-Key` header. */
  readonly VITE_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
