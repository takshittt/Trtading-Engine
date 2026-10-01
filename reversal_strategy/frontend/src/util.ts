export const inr = (n: number) =>
  '₹' + (n ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })

export const inr2 = (n: number) =>
  '₹' + (n ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const pct = (n: number) => `${n >= 0 ? '+' : ''}${(n ?? 0).toFixed(2)}%`

export const pnlColor = (n: number) => (n > 0 ? 'text-emerald-600' : n < 0 ? 'text-rose-600' : 'text-slate-600')

export const timeShort = (iso: string) => {
  try { return new Date(iso).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
  catch { return iso }
}

export const dateShort = (iso: string) => {
  try { return new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) }
  catch { return iso }
}

/** Wall-clock with seconds — execution timing is useless rounded to the minute. */
export const stampShort = (iso: string | null) => {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return iso }
}

/** Signal→fill latency. Sub-second is the interesting range, so keep ms until 1s. */
export const latency = (ms: number | null | undefined) => {
  if (!ms && ms !== 0) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 60_000)}m`
}

/** Compact holding period. */
export const duration = (secs: number | null | undefined) => {
  if (secs == null) return '—'
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.round(secs / 60)}m`
  if (secs < 86_400) return `${(secs / 3600).toFixed(1)}h`
  return `${(secs / 86_400).toFixed(1)}d`
}

export const tfColor: Record<string, string> = {
  '1H': 'bg-sky-50 text-sky-700 border-sky-200',
  '4H': 'bg-violet-50 text-violet-700 border-violet-200',
  '1D': 'bg-amber-50 text-amber-700 border-amber-200',
}

const _MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

/**
 * True when a contract's expiry has passed.
 *
 * Expired contracts stop ticking, so anything still quoting one is showing a
 * dead price. Accepts Shoonya's "30-JUN-2026" and ISO. An unreadable or absent
 * expiry is never called expired — a false positive would hide a live row.
 */
export const isExpired = (expiry: string | null | undefined): boolean => {
  if (!expiry) return false
  let d: Date | null = null
  const m = expiry.trim().toUpperCase().match(/^(\d{1,2})-([A-Z]{3})-(\d{4})$/)
  if (m) {
    const mon = _MONTHS.indexOf(m[2])
    if (mon >= 0) d = new Date(+m[3], mon, +m[1])
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(expiry)) {
    const [y, mo, dd] = expiry.split('-').map(Number)
    d = new Date(y, mo - 1, dd)
  }
  if (!d || isNaN(d.getTime())) return false
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return d < today
}
