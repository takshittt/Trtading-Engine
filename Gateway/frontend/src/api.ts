// --- Backend location ---

// Unset (dev, or served behind the backend's reverse proxy): same origin, and
// the Vite proxy forwards /api. Set at build time when the UI is hosted apart
// from the API, e.g. VITE_API_BASE=https://gateway-api.example.com
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

/** ws(s):// URL for a backend WebSocket path such as /api/ws/ticker. */
export function wsUrl(path: string): string {
  if (API_BASE) return API_BASE.replace(/^http/, 'ws') + path
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${path}`
}

// --- Auth token storage (JWT) ---

const TOKEN_KEY = 'gw_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

// 401s from the *app auth* layer (bad/expired/missing JWT) carry these details.
// A 401 from the *broker* layer ("Not authenticated with Shoonya broker") must
// NOT log the user out — that just means the broker isn't connected yet.
const AUTH_401_PATTERNS = [
  'authorization header',
  'token expired',
  'invalid token',
  'user no longer exists',
]

function isAuthFailure(detail: string): boolean {
  const d = detail.toLowerCase()
  return AUTH_401_PATTERNS.some(p => d.includes(p))
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const headers = new Headers(options?.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const res = await fetch(`${API_BASE}/api${path}`, { ...options, headers })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = err.detail || res.statusText
    if (res.status === 401 && isAuthFailure(String(detail))) {
      // Token is bad/expired — drop it and bounce to sign-in.
      clearToken()
      if (!window.location.pathname.startsWith('/signin')) {
        window.location.href = '/signin'
      }
    }
    throw new Error(detail)
  }
  return res.json()
}

// --- App user auth API ---

export interface AuthUser {
  id: number
  email: string
  form_filled: boolean
}

interface AuthResponse {
  token: string
  user: AuthUser
}

export async function signup(email: string, password: string): Promise<AuthResponse> {
  return apiFetch('/auth/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

export async function signin(email: string, password: string): Promise<AuthResponse> {
  return apiFetch('/auth/signin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

export async function getMe(): Promise<AuthUser> {
  return apiFetch('/auth/me')
}

export interface ConfiguredBroker {
  id: number
  broker_name: string
  label: string
  is_active: boolean
  fields_present: string[]
}

// All broker accounts this user has saved (field NAMES only, never values).
export async function getCredentials(): Promise<ConfiguredBroker[]> {
  return apiFetch('/credentials')
}

// Saves a NEW account. The user's first account becomes active automatically;
// later ones are saved inactive until explicitly activated.
export async function saveCredentials(
  brokerName: string,
  credentials: Record<string, string>,
  label?: string
): Promise<ConfiguredBroker> {
  return apiFetch('/credentials', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ broker_name: brokerName, label, credentials }),
  })
}

// Replaces an existing account's label/credentials. Does not change which
// account is active.
export async function updateCredentials(
  accountId: number,
  brokerName: string,
  credentials: Record<string, string>,
  label?: string
): Promise<ConfiguredBroker> {
  return apiFetch(`/credentials/${accountId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ broker_name: brokerName, label, credentials }),
  })
}

// Marks this account as the one /api/connect and the trading engine use next.
export async function activateCredentials(accountId: number): Promise<ConfiguredBroker> {
  return apiFetch(`/credentials/${accountId}/activate`, { method: 'POST' })
}

export async function deleteCredentials(accountId: number): Promise<{ deleted: boolean }> {
  return apiFetch(`/credentials/${accountId}`, { method: 'DELETE' })
}

export interface RevealedCredentials {
  id: number
  broker_name: string
  label: string
  credentials: Record<string, string>
}

// Returns one account's DECRYPTED credential values — gated by re-entering the app password.
export async function revealCredentials(accountId: number, password: string): Promise<RevealedCredentials> {
  return apiFetch(`/credentials/${accountId}/reveal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
}

export interface CredentialTestResult {
  success: boolean
  message: string
}

// Tests credentials currently open in the form (unsaved or saved) — nothing is persisted.
export async function testCredentials(
  brokerName: string,
  credentials: Record<string, string>
): Promise<CredentialTestResult> {
  return apiFetch('/credentials/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ broker_name: brokerName, credentials }),
  })
}

// Tests a saved account's stored credentials without revealing them to the client.
export async function testStoredCredentials(accountId: number, password: string): Promise<CredentialTestResult> {
  return apiFetch(`/credentials/${accountId}/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
}

export async function getExchangeTargets(): Promise<import('./types').ExchangeTarget[]> {
  return apiFetch('/exchange-targets')
}

export async function updateExchangeTarget(
  exch: string,
  patch: { enabled?: boolean; target_value?: number }
): Promise<import('./types').ExchangeTarget> {
  return apiFetch(`/exchange-targets/${encodeURIComponent(exch)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function getSymbolTargets(): Promise<import('./types').SymbolTarget[]> {
  return apiFetch('/symbol-targets')
}

export async function updateSymbolTarget(
  exch: string,
  tsym: string,
  patch: { enabled?: boolean; target_value?: number }
): Promise<import('./types').SymbolTarget> {
  return apiFetch(`/symbol-targets/${encodeURIComponent(exch)}/${encodeURIComponent(tsym)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function updateLotTarget(
  lotId: number,
  patch: { enabled?: boolean; target_value?: number }
): Promise<import('./types').OrderLot> {
  return apiFetch(`/lots/${lotId}/target`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function updateLotStrategy(
  lotId: number,
  strategyName: string
): Promise<import('./types').OrderLot> {
  return apiFetch(`/lots/${lotId}/strategy`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ strategy_name: strategyName }),
  })
}

export async function getStrategyNames(): Promise<string[]> {
  return apiFetch('/lots/strategy-names')
}

export async function setLotTempExit(
  lotId: number,
  enabled: boolean
): Promise<import('./types').OrderLot> {
  return apiFetch(`/lots/${lotId}/temp-exit`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
}

export async function listPersistentOrders(): Promise<import('./types').PersistentOrder[]> {
  return apiFetch('/persistent-orders')
}

export async function createPersistentOrder(payload: {
  exchange: string
  tradingsymbol: string
  buy_or_sell: 'B' | 'S'
  product_type: string
  price_type: string
  price: number
  trigger_price: number
  quantity: number
  target_enabled: boolean
  target_value: number
  description?: string
}): Promise<import('./types').PersistentOrder> {
  return apiFetch('/persistent-orders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function cancelPersistentOrder(
  id: number
): Promise<import('./types').PersistentOrder> {
  return apiFetch(`/persistent-orders/${id}`, { method: 'DELETE' })
}

export async function getLotHistory(
  limit = 200,
  offset = 0
): Promise<import('./types').OrderLot[]> {
  return apiFetch(`/lots/history?limit=${limit}&offset=${offset}`)
}

export async function clearCancelledHistory(): Promise<{ status: string; deleted: number }> {
  return apiFetch('/lots/history/cancelled', { method: 'DELETE' })
}

export interface SubscribedSymbol {
  exch: string
  token: string
  tsym: string
  lp: string
  bp1: string
  sp1: string
}

export async function getSubscribedSymbols(): Promise<{ symbols: SubscribedSymbol[] }> {
  return apiFetch('/status/subscribed')
}

export async function unsubscribeAllSymbols(): Promise<{ unsubscribed: number }> {
  return apiFetch('/status/unsubscribe_all', { method: 'POST' })
}

export async function cancelBrokerOrder(
  orderId: string
): Promise<{ status: string; order_id: string }> {
  return apiFetch(`/orders/${encodeURIComponent(orderId)}`, { method: 'DELETE' })
}

export async function getRollTargets(
  lotId: number
): Promise<import('./types').ScripSearchResult[]> {
  return apiFetch(`/lots/${lotId}/roll-targets`)
}

export async function rolloverLot(
  lotId: number,
  payload: import('./types').RolloverPayload
): Promise<import('./types').RolloverResult> {
  return apiFetch(`/lots/${lotId}/rollover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function refreshScripmaster(): Promise<{
  total: number
  symbols_loaded: number
  per_exchange: Record<string, number>
  updated_at: string
}> {
  return apiFetch('/scripmaster/refresh', { method: 'POST' })
}

export async function getQuote(
  exch: string,
  token: string
): Promise<import('./types').QuoteResponse> {
  return apiFetch(`/quote?exchange=${encodeURIComponent(exch)}&token=${encodeURIComponent(token)}`)
}

export async function getFailedRollovers(): Promise<import('./types').RolloverFailure[]> {
  return apiFetch('/rollover-intents/failures')
}

export async function ackRollover(id: number): Promise<{ status: string; id: number }> {
  return apiFetch(`/rollover-intents/${id}/ack`, { method: 'POST' })
}

// --- Market hours settings ---

type MHSettings = import('./types').MarketHoursSettings

export async function getMarketHours(): Promise<MHSettings> {
  return apiFetch('/market-hours')
}

export async function updateExchangeHours(
  exch: string,
  open: string,
  close: string
): Promise<MHSettings> {
  return apiFetch(`/market-hours/${encodeURIComponent(exch)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ open, close }),
  })
}

export async function resetExchangeHours(exch: string): Promise<MHSettings> {
  return apiFetch(`/market-hours/${encodeURIComponent(exch)}`, { method: 'DELETE' })
}

export async function setMarketHoursEnforced(enforced: boolean): Promise<MHSettings> {
  return apiFetch('/market-hours/enforced', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enforced }),
  })
}

export async function addMarketHoliday(date: string, label: string): Promise<MHSettings> {
  return apiFetch('/market-hours/holidays', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date, label }),
  })
}

export async function removeMarketHoliday(date: string): Promise<MHSettings> {
  return apiFetch(`/market-hours/holidays/${encodeURIComponent(date)}`, { method: 'DELETE' })
}
