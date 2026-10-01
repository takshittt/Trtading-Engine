import type {
  Signal, Position, Summary, Config, StockInfo, LogLine, JournalStats, SearchResult, Preview,
  PaperStatus, RollTarget, RollPayload, PositionLegs, BrokerInfo,
} from './types'
import { API_BASE, authHeaders, clearToken } from './auth'

const J = { 'Content-Type': 'application/json' }

// A 401 means our Gateway token expired or was revoked mid-session. Drop it and
// reload so the login gate takes over, rather than letting every panel spin on
// failed requests.
function onUnauthorized(): void {
  clearToken()
  location.reload()
}

async function get<T>(url: string, timeoutMs?: number): Promise<T> {
  const r = await fetch(API_BASE + url, {
    headers: authHeaders(),
    ...(timeoutMs ? { signal: AbortSignal.timeout(timeoutMs) } : {}),
  })
  if (r.status === 401) { onUnauthorized(); throw new Error(`${url} -> 401`) }
  if (!r.ok) throw new Error(`${url} -> ${r.status}`)
  return r.json()
}
async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(API_BASE + url, {
    method: 'POST',
    headers: { ...J, ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (r.status === 401) { onUnauthorized(); throw new Error(`${url} -> 401`) }
  return r.json()
}
async function del<T>(url: string): Promise<T> {
  const r = await fetch(API_BASE + url, { method: 'DELETE', headers: authHeaders() })
  if (r.status === 401) { onUnauthorized(); throw new Error(`${url} -> 401`) }
  return r.json()
}

export interface BuyPayload {
  symbol: string; lots: number
  target_mode: string; target_method: string; target_value: number
  sl_mode: string; sl_method: string; sl_value: number
  atr?: number; resistance?: number; support?: number; signal_id?: number
}

export const api = {
  summary: () => get<Summary>('/api/summary'),
  config: () => get<Config>('/api/config'),
  saveConfig: (c: Partial<Config>) => post<{ ok: boolean; config: Config }>('/api/config', c),

  signals: () => get<Signal[]>('/api/signals?actionable=true'),
  todaySignals: () => get<Signal[]>('/api/signals/today'),
  // Polled every 5s while the tab is open — bound it so a stalled backend
  // surfaces as an error instead of silently never resolving.
  previousSignals: () => get<{ date: string; count: number; signals: Signal[] }[]>('/api/signals/previous', 15000),
  signalHistory: () => get<Signal[]>('/api/signals/history'),
  ignoreSignal: (id: number) => post(`/api/signals/${id}/ignore`),
  search: (q: string, exchange = '') => get<SearchResult[]>(`/api/signals/search?q=${encodeURIComponent(q)}${exchange ? `&exchange=${exchange}` : ''}`),
  manualAdd: (r: { symbol: string; signal_type: string; lot_size?: number; expiry?: string; timeframe?: string }) =>
    post<{ ok: boolean; signal: Signal }>('/api/signals/manual-add', r),
  preview: (symbol: string) => get<Preview>(`/api/signals/preview?symbol=${encodeURIComponent(symbol)}`),

  positions: () => get<Position[]>('/api/positions'),
  buy: (b: BuyPayload) => post<{ ok: boolean; error?: string; position?: Position }>('/api/positions/buy', b),
  average: (id: number, lots: number, signal_id?: number) =>
    post<{ ok: boolean; error?: string }>(`/api/positions/${id}/average`, { lots, signal_id }),
  editTargets: (id: number, e: { target?: number; stop_loss?: number }) => post(`/api/positions/${id}/edit`, e),
  partial: (id: number, lots: number) => post<{ ok: boolean; error?: string }>(`/api/positions/${id}/partial`, { lots }),
  exit: (id: number) => post<{ ok: boolean }>(`/api/positions/${id}/exit`),
  legs: (id: number) => get<PositionLegs>(`/api/positions/${id}/legs`),
  rollTargets: (id: number) => get<RollTarget[]>(`/api/positions/${id}/roll-targets`, 20000),
  roll: (id: number, p: RollPayload) =>
    post<{ ok: boolean; error?: string; basis: number; near_price: number; far_price: number }>(`/api/positions/${id}/roll`, p),

  stock: (symbol: string) => get<StockInfo>(`/api/stock/${symbol}`),
  blacklist: () => get<{ symbol: string; reason: string; created_at: string }[]>('/api/blacklist'),
  addBlacklist: (symbol: string) => post('/api/blacklist', { symbol }),
  removeBlacklist: (symbol: string) => del(`/api/blacklist/${symbol}`),

  journal: (book: 'all' | 'live' | 'paper' = 'all') =>
    get<{ trades: Position[]; stats: JournalStats; paper_stats: JournalStats }>(`/api/journal?book=${book}`),
  logs: () => get<LogLine[]>('/api/logs'),
  reconcile: () => post<{ ok: boolean; mismatches?: unknown[] }>('/api/reconcile'),
  refreshMargin: () => post<{ ok: boolean; as_of: string }>('/api/margin/refresh'),
  paperStatus: () => get<PaperStatus>('/api/paper/status'),
  paperStart: (lots: number) => post<{ ok: boolean } & PaperStatus>('/api/paper/start', { lots }),
  paperStop: (square_off: boolean) =>
    post<{ ok: boolean; squared_off: number } & PaperStatus>('/api/paper/stop', { square_off }),
  paperReset: () => post<{ ok: boolean; positions_removed: number; signals_reset: number }>('/api/paper/reset'),

  killSwitch: () => post<{ ok: boolean; closed: number }>('/api/kill-switch'),
  resume: () => post('/api/resume'),

  broker: () => get<BrokerInfo>('/api/broker'),
  brokerConnect: () =>
    post<{ ok: boolean; connected: boolean; already_connected: boolean; detail: string; uid: string; name: string }>(
      '/api/broker/connect'),
  brokerDisconnect: () =>
    post<{ ok: boolean; connected: boolean; detail: string; warning: string }>(
      '/api/broker/disconnect'),
}
