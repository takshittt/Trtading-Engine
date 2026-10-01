// Indian-style comma grouping (e.g. 1,00,000.00) used across the dashboard.
export function formatINR(value: number, decimals = 2): string {
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export function formatCurrency(value: number, decimals = 2): string {
  return `₹${formatINR(value, decimals)}`
}

// Signed P&L display, e.g. +₹1,234.50 / −₹1,234.50
export function formatPnl(value: number, decimals = 2): string {
  const sign = value >= 0 ? '+' : '−'
  return `${sign}₹${formatINR(Math.abs(value), decimals)}`
}

// True if `opened_at` falls on the current IST session date. Mirrors the
// backend `_opened_today`: opened_at is serialized as naive-UTC, so tag it UTC
// then shift by +5:30 to compare IST trading dates. Day-MTM is meaningless for
// a same-day entry, so callers use this to keep MTM at 0 for lots opened today.
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000
export function openedTodayIST(openedAt?: string): boolean {
  if (!openedAt) return false
  const utcMs = Date.parse(openedAt.endsWith('Z') ? openedAt : `${openedAt}Z`)
  if (Number.isNaN(utcMs)) return false
  const openedIstDate = new Date(utcMs + IST_OFFSET_MS).toISOString().slice(0, 10)
  const nowIstDate = new Date(Date.now() + IST_OFFSET_MS).toISOString().slice(0, 10)
  return openedIstDate === nowIstDate
}

// Short badge code for the service that placed a lot (source_service).
// Known services get a curated abbreviation; anything else falls back to its
// first two letters uppercased, so a new engine needs no code change to badge.
const SERVICE_BADGES: Record<string, string> = {
  grid: 'GR',
  reversal: 'RV',
  // Legacy service names still registered in the Gateway DB.
  snowball: 'GR',
  swingbottom: 'RV',
}
export function serviceBadge(name?: string | null): string | null {
  if (!name) return null
  const key = name.trim().toLowerCase()
  if (!key) return null
  return SERVICE_BADGES[key] ?? key.slice(0, 2).toUpperCase()
}
