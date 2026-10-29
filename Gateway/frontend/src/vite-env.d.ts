/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  /** Grid dashboard URL linked from the landing page (default: this host, port 5175). */
  readonly VITE_GRID_URL?: string
  /** Reversal dashboard URL linked from the landing page (default: this host, port 5176). */
  readonly VITE_REVERSAL_URL?: string
}
