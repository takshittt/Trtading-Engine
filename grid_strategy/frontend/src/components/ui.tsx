import { useRef, useState, type ReactNode } from 'react'

export function Card({ title, right, children, className = '' }: {
  title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={`bg-white border border-gray-200 rounded-xl ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between px-4 py-2.5 border-b border-gray-200">
          <div className="text-sm font-semibold text-gray-900">{title}</div>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  )
}

export function Badge({ tone, children }: { tone: 'green' | 'red' | 'amber' | 'gray' | 'blue' | 'purple'; children: ReactNode }) {
  const tones: Record<string, string> = {
    green: 'bg-emerald-500/15 text-emerald-600 border-emerald-500',
    red: 'bg-red-500/15 text-red-600 border-red-400',
    amber: 'bg-amber-500/15 text-amber-600 border-amber-500',
    gray: 'bg-gray-200 text-gray-600 border-gray-300',
    blue: 'bg-sky-500/15 text-sky-600 border-sky-400',
    purple: 'bg-violet-500/15 text-violet-600 border-violet-500',
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border ${tones[tone]}`}>
      {children}
    </span>
  )
}

/** Green/red pill switch — the circuit-breaker control from the sketch. */
export function PillSwitch({ on, onChange, title, busy }: {
  on: boolean; onChange: (v: boolean) => void; title?: string; busy?: boolean
}) {
  return (
    <button
      title={title}
      disabled={busy}
      onClick={() => onChange(!on)}
      className={`relative w-11 h-6 rounded-full transition-colors duration-200 shrink-0
        ${on ? 'bg-emerald-500' : 'bg-red-500'} ${busy ? 'opacity-50' : 'cursor-pointer'}`}
    >
      <span className={`absolute top-0.5 w-5 h-5 bg-white rounded-full shadow transition-all duration-200
        ${on ? 'left-[22px]' : 'left-0.5'}`} />
    </button>
  )
}

/** Hover tooltip anchored with position:fixed so it escapes the watchlist's
 * overflow-x-auto clipping (which otherwise cuts it off). Opens ABOVE the
 * trigger when there's room, else below; clamped to stay on-screen. */
export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [pos, setPos] = useState<{ x: number; y: number; up: boolean } | null>(null)

  const show = () => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const up = r.top > 220                                  // enough room above → open upward
    const half = 160                                        // half of w-80 (320px)
    const x = Math.min(Math.max(r.left + r.width / 2, half + 8), window.innerWidth - half - 8)
    setPos({ x, y: up ? r.top - 8 : r.bottom + 8, up })
  }

  return (
    <span ref={ref} className="relative inline-flex items-center"
      onMouseEnter={show} onMouseLeave={() => setPos(null)}>
      {children}
      {pos && (
        <span
          style={{ left: pos.x, top: pos.y }}
          className={`pointer-events-none fixed z-[100] w-80 -translate-x-1/2 ${pos.up ? '-translate-y-full' : ''}
            bg-gray-100 border border-gray-300 text-gray-800 text-xs rounded-lg p-3 shadow-xl whitespace-pre-line`}>
          {text}
        </span>
      )}
    </span>
  )
}

export function Spinner() {
  return <span className="inline-block w-3.5 h-3.5 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
}

export const inr = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined
    ? '—'
    : `${v < 0 ? '-' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits })}`

export const pnlClass = (v: number | null | undefined) =>
  v === null || v === undefined ? 'text-gray-500' : v >= 0 ? 'text-emerald-600' : 'text-red-600'

export const fmtPx = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || v === 0 ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })

export const fmtTime = (iso: string) => {
  if (!iso) return '—'
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const hm = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
  return sameDay ? hm : `${d.getDate()}/${d.getMonth() + 1} ${hm.slice(0, 5)}`
}
