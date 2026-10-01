import type { ReactNode } from 'react'

/**
 * Centred dialog.
 *
 * The panel is capped to the viewport and only its body scrolls, so a dialog
 * taller than the window stays usable instead of running off the bottom with
 * no way to reach it. `footer` pins actions below that scroll area — use it for
 * Save/Cancel on any long form, or the buttons scroll out of reach.
 */
export function Modal({ title, onClose, children, wide, footer }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean; footer?: ReactNode
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
      <div className={`flex max-h-[calc(100vh-2rem)] w-full flex-col ${wide ? 'max-w-3xl' : 'max-w-lg'} rounded-2xl border border-slate-200 bg-white shadow-2xl`}
           onClick={(e) => e.stopPropagation()}>
        <div className="flex shrink-0 items-center justify-between border-b border-slate-100 px-5 py-3.5">
          <h3 className="text-sm font-semibold tracking-tight text-slate-800">{title}</h3>
          <button onClick={onClose} className="grid h-7 w-7 place-items-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-700 text-lg leading-none">×</button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
        {footer && (
          <div className="shrink-0 rounded-b-2xl border-t border-slate-100 bg-white px-5 py-3">{footer}</div>
        )}
      </div>
    </div>
  )
}

export function Badge({ children, className = '', title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center rounded border px-1 py-px text-[9px] font-semibold leading-tight ${className}`}>
      {children}
    </span>
  )
}

// `warn` is the handshake-succeeded-but-nothing-is-flowing state. Without it a
// dead price feed behind a live connection reads as fully healthy, which is the
// one case where a green dot actively misleads.
export function Dot({ on, warn }: { on: boolean; warn?: boolean }) {
  const cls = !on ? 'bg-rose-500' : warn ? 'bg-amber-500 pulse' : 'bg-emerald-500 pulse'
  return <span className={`inline-block h-2 w-2 rounded-full ${cls}`} />
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</span>
      {children}
    </label>
  )
}

/** Two-way MANUAL / AUTO (or any pair) segmented toggle. */
export function Toggle({ value, options, onChange }: {
  value: string; options: [string, string]; onChange: (v: string) => void
}) {
  return (
    <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-[11px] font-semibold">
      {options.map(o => (
        <button key={o} onClick={() => onChange(o)}
          className={`rounded-md px-3 py-1 transition-colors ${value === o ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-400 hover:text-slate-600'}`}>
          {o.toUpperCase()}
        </button>
      ))}
    </div>
  )
}

export const inputCls =
  'w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-800 outline-none focus:border-sky-400 focus:ring-2 focus:ring-sky-100'

export const btn = 'rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-40'
export const btnPrimary = `${btn} bg-sky-600 hover:bg-sky-700 text-white`
export const btnGreen = `${btn} bg-emerald-600 hover:bg-emerald-700 text-white`
export const btnGhost = `${btn} border border-slate-200 bg-white hover:bg-slate-50 text-slate-600`
export const btnDanger = `${btn} bg-rose-600 hover:bg-rose-700 text-white`
