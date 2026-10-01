import type {
  AlertSettings, ChartPayload, EconEventRow, InstrumentSnap, LogRow, LotRow, PnlResponse,
  SearchResult, Snapshot, SortMode,
} from './types'

export const API_BASE = import.meta.env.VITE_API_BASE ?? `${window.location.protocol}//${window.location.hostname}:8010`
// Token accessors live in ./auth; read localStorage directly here to avoid a
// circular import at module-eval time (auth.ts imports API_BASE from here).
function getTokenLS(): string | null {
  return localStorage.getItem('sb_token')
}

/** Dashboard WebSocket URL with the user token as ?token= (WS can't send headers). */
export function wsUrl(): string {
  const base = API_BASE.replace(/^http/, 'ws') + '/ws'
  const token = getTokenLS()
  return token ? `${base}?token=${encodeURIComponent(token)}` : base
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getTokenLS()
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (r.status === 401) {
    // Token missing/expired/invalid → drop it and show grid's login screen.
    localStorage.removeItem('sb_token')
    window.location.reload()
    throw new Error('Not authenticated')
  }
  if (!r.ok) {
    let detail = r.statusText
    try {
      const j = await r.json()
      detail = j.detail ?? detail
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return r.json() as Promise<T>
}

export const api = {
  state: () => req<Snapshot>('/api/state'),
  brokerConnect: () =>
    req<{ connected: boolean; already_connected: boolean; detail: string }>(
      '/api/broker/connect', { method: 'POST' }),
  brokerDisconnect: () =>
    req<{ connected: boolean; detail: string }>('/api/broker/disconnect', { method: 'POST' }),
  engineStart: () => req('/api/engine/start', { method: 'POST' }),
  engineStop: () => req('/api/engine/stop', { method: 'POST' }),
  resetGlobalCb: () => req<{ tripped: boolean; was_tripped: boolean }>(
    '/api/engine/global-cb/reset', { method: 'POST' }),

  search: (q: string, exch = 'ALL') =>
    req<{ results: SearchResult[] }>(`/api/search?q=${encodeURIComponent(q)}&exch=${exch}`),

  instruments: () => req<InstrumentSnap[]>('/api/instruments'),
  addInstrument: (body: {
    sym: string; exch: string; tsym: string; token: string; lot_size: number; expiry: string
    mode?: 'auto' | 'ladder'; instr_type?: 'FUT' | 'OPT'; opttype?: string; strike?: number
  }) =>
    req('/api/instruments', { method: 'POST', body: JSON.stringify(body) }),
  updateInstrument: (id: number, body: object) =>
    req(`/api/instruments/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteInstrument: (id: number) => req(`/api/instruments/${id}`, { method: 'DELETE' }),
  setCircuitBreaker: (id: number, on: boolean) =>
    req(`/api/instruments/${id}/cb?on=${on}`, { method: 'POST' }),
  flatten: (id: number) => req<{ closed: number }>(`/api/instruments/${id}/flatten`, { method: 'POST' }),
  rolloverNow: (id: number, target?: { target_tsym: string; target_token: string }) =>
    req(`/api/instruments/${id}/rollover`, { method: 'POST', body: JSON.stringify(target ?? {}) }),
  rolloverPlan: (id: number, targetToken = '') =>
    req<import('./types').RolloverPlan>(
      `/api/instruments/${id}/rollover/plan${targetToken ? `?target_token=${encodeURIComponent(targetToken)}` : ''}`),
  rollLot: (lotId: number, target?: { target_tsym: string; target_token: string }) =>
    req<{ ok: boolean; basis: number | null; reason: string }>(
      `/api/lots/${lotId}/roll`, { method: 'POST', body: JSON.stringify(target ?? {}) }),

  ladderPreview: (id: number, body: {
    basis: 'fixed' | 'support'; anchor_price: number; interval_points: number
    num_levels: number; sr_lookback_days: number
  }) =>
    req<{ ltp: number; basis: string; levels: { price: number; note: string; target: number }[] }>(
      `/api/instruments/${id}/ladder/preview`, { method: 'POST', body: JSON.stringify(body) }),
  ladderConfirm: (id: number, body: {
    basis: 'fixed' | 'support'; anchor_price: number; interval_points: number
    num_levels: number; sr_lookback_days: number
    levels: { price: number; lots_override?: number | null; target_override?: number | null
      sl_override?: number | null }[]
    first_target?: number | null
    first_sl?: number | null
    sl_enabled?: boolean | null
    target_chain_pct?: number | null
  }) =>
    req<{ ok: boolean; levels_saved: number }>(
      `/api/instruments/${id}/ladder/confirm`, { method: 'POST', body: JSON.stringify(body) }),
  ladderArm: (id: number, on: boolean) =>
    req(`/api/instruments/${id}/ladder/arm?on=${on}`, { method: 'POST' }),
  editLadderLevel: (levelId: number, body: { price?: number; lots_override?: number; target_override?: number; sl_override?: number }) =>
    req(`/api/ladder/levels/${levelId}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteLadderLevel: (levelId: number) => req(`/api/ladder/levels/${levelId}`, { method: 'DELETE' }),
  clearLadderLevels: (id: number) =>
    req<{ ok: boolean; cleared: number; filled: number; kept: number }>(
      `/api/instruments/${id}/ladder/levels`, { method: 'DELETE' }),
  addLadderLevel: (body: { instrument_id: number; price: number; lots_override?: number | null; target_override?: number | null; sl_override?: number | null }) =>
    req('/api/ladder/levels', { method: 'POST', body: JSON.stringify(body) }),

  // `init` carries an AbortSignal: switching tab/sort/filter cancels the
  // in-flight request instead of letting the server finish work nobody reads.
  lots: (status: 'open' | 'pending' | 'closed', sort: SortMode, sym = '', source = '', limit = 200, offset = 0,
         init?: RequestInit) =>
    req<{ total: number; lots: LotRow[] }>(
      `/api/lots?status=${status}&sort=${sort}&sym=${encodeURIComponent(sym)}&source=${source}&limit=${limit}&offset=${offset}`,
      init),
  exitLot: (id: number) => req(`/api/lots/${id}/exit`, { method: 'POST' }),
  skipLot: (id: number) =>
    req<{ ok: boolean; next_level_price: number | null }>(`/api/lots/${id}/skip`, { method: 'POST' }),
  cancelPendingLot: (id: number) =>
    req<{ ok: boolean; paused?: boolean }>(`/api/lots/${id}/cancel`, { method: 'POST' }),
  reestablishLevel: (levelId: number) =>
    req<{ ok: boolean; placed: boolean; await_recovery: boolean }>(
      `/api/ladder/levels/${levelId}/reestablish`, { method: 'POST' }),
  editLot: (id: number, body: { target_price?: number; sl_price?: number }) =>
    req(`/api/lots/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  tempExit: (id: number, body: { type: 'market' | 'limit' | 'cancel'; price?: number }) =>
    req(`/api/lots/${id}/temp-exit`, { method: 'POST', body: JSON.stringify(body) }),
  reEnter: (id: number, body: { type: 'market' | 'limit' | 'cancel'; price?: number }) =>
    req(`/api/lots/${id}/re-enter`, { method: 'POST', body: JSON.stringify(body) }),

  chart: (instId: number, tf: number, bars = 240) =>
    req<ChartPayload>(`/api/chart/${instId}?tf=${tf}&bars=${bars}`),

  logs: (limit = 5, offset = 0, filters: { sym?: string; level?: string; category?: string } = {}) => {
    const p = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (filters.sym) p.set('sym', filters.sym)
    if (filters.level) p.set('level', filters.level)
    if (filters.category) p.set('category', filters.category)
    return req<{ total: number; logs: LogRow[] }>(`/api/logs?${p.toString()}`)
  },

  pnl: () => req<PnlResponse>('/api/pnl'),

  getSettings: () => req<AlertSettings>('/api/settings'),
  saveSettings: (body: {
    whatsapp_enabled?: boolean; whatsapp_number?: string
    callmebot_apikey?: string; alert_on_feed_down?: boolean
  }) => req<AlertSettings>('/api/settings', { method: 'PUT', body: JSON.stringify(body) }),

  events: (days = 14) => req<EconEventRow[]>(`/api/events?days=${days}`),
  addEvent: (body: { dt: string; title: string; impact: string; region: string; note: string }) =>
    req('/api/events', { method: 'POST', body: JSON.stringify(body) }),
  deleteEvent: (id: number) => req(`/api/events/${id}`, { method: 'DELETE' }),
}
