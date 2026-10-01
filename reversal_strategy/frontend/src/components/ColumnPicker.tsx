import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export interface ColumnDef<T, C = unknown> {
  key: string
  /** Header text. Also what the picker lists. */
  label: string
  align?: 'left' | 'right' | 'center'
  /** Hover text on the header. */
  title?: string
  /** Cannot be hidden — the row would stop being identifiable without it. */
  locked?: boolean
  /** Hidden until the user asks for it. Keeps dense tables readable by default. */
  optional?: boolean
  cell: (row: T, ctx: C) => ReactNode
  /** Extra classes for this column's cells. */
  cellClass?: string | ((row: T, ctx: C) => string)
  /** If provided, this column is sortable. Returns the value to sort on. */
  sortValue?: (row: T) => string | number | null | undefined
}

const alignClass = (a?: string) =>
  a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left'

/**
 * Which columns a table shows, remembered per table across reloads.
 *
 * Stored as the hidden set rather than the visible one, so a column added in a
 * later release shows up for existing users instead of staying invisible because
 * it was missing from a saved list.
 */
export function useColumns<T, C>(storageKey: string, defs: ColumnDef<T, C>[]) {
  const key = `swing-cols-${storageKey}`

  const [hidden, setHidden] = useState<Set<string>>(() => {
    const fallback = new Set(defs.filter(d => d.optional).map(d => d.key))
    try {
      const raw = localStorage.getItem(key)
      if (!raw) return fallback
      const saved = JSON.parse(raw)
      return Array.isArray(saved) ? new Set<string>(saved) : fallback
    } catch {
      return fallback
    }
  })

  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify([...hidden])) } catch { /* session only */ }
  }, [key, hidden])

  const toggle = useCallback((k: string) => {
    setHidden(prev => {
      const next = new Set(prev)
      next.has(k) ? next.delete(k) : next.add(k)
      return next
    })
  }, [])

  const reset = useCallback(() => {
    setHidden(new Set(defs.filter(d => d.optional).map(d => d.key)))
  }, [defs])

  const visible = useMemo(
    () => defs.filter(d => d.locked || !hidden.has(d.key)),
    [defs, hidden])

  return { visible, hidden, toggle, reset }
}

/* ─── Sort state ──────────────────────────────────────────────── */

export type SortDir = 'asc' | 'desc'
export interface SortState { key: string; dir: SortDir }

/**
 * Sort state for a table — persisted per table key across reloads.
 * Click once → asc, again → desc, third time → clear.
 */
export function useSort(storageKey: string) {
  const key = `swing-sort-${storageKey}`
  const [sort, setSort] = useState<SortState | null>(() => {
    try {
      const raw = localStorage.getItem(key)
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  })

  useEffect(() => {
    try {
      if (sort) localStorage.setItem(key, JSON.stringify(sort))
      else localStorage.removeItem(key)
    } catch { /* session only */ }
  }, [key, sort])

  const toggle = useCallback((colKey: string) => {
    setSort(prev => {
      if (!prev || prev.key !== colKey) return { key: colKey, dir: 'asc' }
      if (prev.dir === 'asc') return { key: colKey, dir: 'desc' }
      return null // third click clears
    })
  }, [])

  const clear = useCallback(() => setSort(null), [])

  return { sort, toggle, clear }
}

/** Sort rows using the sortValue accessor from the matching column definition. */
export function sortRows<T, C>(
  rows: T[],
  cols: ColumnDef<T, C>[],
  sort: SortState | null,
): T[] {
  if (!sort) return rows
  const col = cols.find(c => c.key === sort.key)
  if (!col?.sortValue) return rows
  const accessor = col.sortValue
  const dir = sort.dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = accessor(a)
    const vb = accessor(b)
    // nulls / undefined always sink to the bottom
    if (va == null && vb == null) return 0
    if (va == null) return 1
    if (vb == null) return -1
    if (typeof va === 'string' && typeof vb === 'string') return va.localeCompare(vb) * dir
    return ((va as number) - (vb as number)) * dir
  })
}

/** Gear button that opens a tick-list of the table's columns. */
export function ColumnPicker<T, C>({ defs, hidden, onToggle, onReset, label = 'Columns' }: {
  defs: ColumnDef<T, C>[]
  hidden: Set<string>
  onToggle: (key: string) => void
  onReset: () => void
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  const shown = defs.filter(d => d.locked || !hidden.has(d.key)).length

  return (
    <div ref={box} className="relative">
      <button onClick={() => setOpen(o => !o)}
        title={`${label} — choose what to show (${shown} of ${defs.length})`}
        className={`grid h-7 w-7 place-items-center rounded-lg border text-xs transition-colors ${
          open ? 'border-sky-300 bg-sky-50 text-sky-700' : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'}`}>
        <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="currentColor" aria-hidden="true">
          <rect x="3" y="4" width="4" height="16" rx="1" />
          <rect x="10" y="4" width="4" height="16" rx="1" />
          <rect x="17" y="4" width="4" height="16" rx="1" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 z-40 mt-1 max-h-80 w-56 overflow-auto rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl">
          <div className="flex items-center justify-between px-2 pb-1 pt-0.5">
            <span className="text-[10px] font-bold uppercase tracking-wide text-slate-400">{label}</span>
            <button onClick={onReset}
              className="text-[10px] font-semibold text-sky-600 hover:underline">Reset</button>
          </div>
          {defs.map(d => {
            const on = d.locked || !hidden.has(d.key)
            return (
              <label key={d.key}
                title={d.locked ? 'Always shown' : undefined}
                className={`flex items-center gap-2 rounded-lg px-2 py-1 text-xs ${
                  d.locked ? 'cursor-default text-slate-400' : 'cursor-pointer text-slate-700 hover:bg-slate-50'}`}>
                <input type="checkbox" checked={on} disabled={d.locked}
                  onChange={() => onToggle(d.key)} />
                <span className="truncate">{d.label || d.key}</span>
                {d.locked && <span className="ml-auto text-[9px] uppercase text-slate-300">fixed</span>}
              </label>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** Header row built from the visible column definitions. Supports clickable sort headers. */
export function ColumnHead<T, C>({ cols, className = '', sort, onSort }: {
  cols: ColumnDef<T, C>[]
  className?: string
  sort?: SortState | null
  onSort?: (key: string) => void
}) {
  return (
    <thead className={`sticky top-0 z-10 bg-slate-50 text-[9px] uppercase tracking-tight text-slate-400 ${className}`}>
      <tr className="border-b border-slate-100">
        {cols.map(c => {
          const isSortable = !!c.sortValue && !!onSort
          const isActive = sort?.key === c.key
          const arrow = isActive ? (sort!.dir === 'asc' ? ' ▲' : ' ▼') : ''
          return (
            <th key={c.key}
              title={c.title || (isSortable ? `Sort by ${c.label}` : undefined)}
              onClick={isSortable ? () => onSort(c.key) : undefined}
              className={`whitespace-nowrap px-1.5 py-1 ${alignClass(c.align)}${
                isSortable ? ' cursor-pointer select-none hover:text-slate-600 transition-colors' : ''}${
                isActive ? ' text-sky-600' : ''}`}>
              {c.label}{arrow}
            </th>
          )
        })}
      </tr>
    </thead>
  )
}

/** Cells for one row, in the visible column order. */
export function ColumnCells<T, C>({ cols, row, ctx, pad = 'px-1.5 py-1' }: {
  cols: ColumnDef<T, C>[]
  row: T
  ctx: C
  pad?: string
}) {
  return (
    <>
      {cols.map(c => {
        const extra = typeof c.cellClass === 'function' ? c.cellClass(row, ctx) : (c.cellClass ?? '')
        return (
          <td key={c.key} className={`${pad} ${alignClass(c.align)} ${extra}`}>
            {c.cell(row, ctx)}
          </td>
        )
      })}
    </>
  )
}
