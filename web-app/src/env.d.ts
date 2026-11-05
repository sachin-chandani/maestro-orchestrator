/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" for MockClient, "0" for HttpClient; unset means MockClient (until #108), or HttpClient in `dev:api`. */
  readonly VITE_MAESTRO_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
