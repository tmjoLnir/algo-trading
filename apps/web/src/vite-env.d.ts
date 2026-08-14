/// <reference types="vite/client" />

/**
 * Declaring our own env vars (rather than relying on Vite's string index
 * signature) means a typo in `import.meta.env.VITE_API_BSAE_URL` is a compile
 * error instead of a silent `undefined` that falls back to localhost in
 * production.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_WS_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
