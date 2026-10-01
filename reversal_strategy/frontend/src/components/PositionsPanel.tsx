import { Fragment, useMemo, useState } from 'react'
import type { Position, Summary } from '../types'
import { api } from '../api'
import { ColumnCells, ColumnHead, ColumnPicker, useColumns, useSort, sortRows } from './ColumnPicker'
import { POSITION_COLUMNS, type PositionCtx } from './positionColumns'
import { inputCls, btnGhost } from './ui'
import { RolloverModal } from './RolloverModal'
import { AveragingModal } from './AveragingModal'

function EditRow({ p, onDone, cols }: { p: Position; onDone: () => void; cols: number }) {
  const [target, setTarget] = useState(p.target)
  const [sl, setSl] = useState(p.stop_loss)
  return (
    <tr className="bg-slate-50">
      <td colSpan={cols} className="px-4 py-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-[11px] text-slate-500">Target
            <input type="number" value={target} onChange={e => setTarget(+e.target.value)} className={`${inputCls} mt-1 w-32`} />
          </label>
          <label className="text-[11px] text-slate-500">Stop Loss
            <input type="number" value={sl} onChange={e => setSl(+e.target.value)} className={`${inputCls} mt-1 w-32`} />
          </label>
          <button className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-700"
            onClick={async () => { await api.editTargets(p.id, { target, stop_loss: sl }); onDone() }}>Save (→ manual)</button>
          <button className={btnGhost} onClick={onDone}>Cancel</button>
        </div>
      </td>
    </tr>
  )
}

function ExitMenu({ p, onDone }: { p: Position; onDone: () => void }) {
  const [open, setOpen] = useState(false)
  const [partial, setPartial] = useState(false)
  const [lots, setLots] = useState(1)
  return (
    <div className="relative inline-block">
      <button onClick={() => setOpen(o => !o)}
        className="rounded-md bg-rose-600 px-1.5 py-0.5 text-[10px] font-bold text-white hover:bg-rose-700">Exit ▾</button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-52 rounded-xl border border-slate-200 bg-white p-1.5 shadow-lg">
          <button className="block w-full rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50"
            onClick={async () => { setOpen(false); await api.exit(p.id); onDone() }}>Complete Exit ({p.lots} lot)</button>
          {p.lots > 1 && !partial && (
            <button className="block w-full rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-slate-700 hover:bg-slate-50"
              onClick={() => setPartial(true)}>Partial Exit…</button>
          )}
          {p.lots > 1 && partial && (
            <div className="flex items-center gap-1.5 px-2 py-1.5">
              <input type="number" min={1} max={p.lots - 1} value={lots}
                onChange={e => setLots(Math.max(1, Math.min(p.lots - 1, +e.target.value)))}
                className="w-16 rounded-md border border-slate-200 px-2 py-1 text-xs" />
              <span className="text-[11px] text-slate-400">of {p.lots} lots</span>
              <button className="ml-auto rounded-md bg-rose-600 px-2 py-1 text-[11px] font-bold text-white hover:bg-rose-700"
                onClick={async () => { setOpen(false); await api.partial(p.id, lots); onDone() }}>Exit</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function PositionsPanel({ positions, summary, onStock, refresh }: {
  positions: Position[]; summary: Summary | null
  onStock: (s: string) => void; refresh: () => void
}) {
  const [editing, setEditing] = useState<number | null>(null)
  const [rolling, setRolling] = useState<Position | null>(null)
  const [avgFor, setAvgFor] = useState<Position | null>(null)
  const [switching, setSwitching] = useState(false)
  const { visible, hidden, toggle, reset } = useColumns<Position, PositionCtx>('positions', POSITION_COLUMNS)
  const { sort, toggle: toggleSort } = useSort('positions')

  const manualExit = summary?.exit_mode === 'manual'
  const breachedCount = positions.filter(p => p.sl_breached).length

  // Breached stops float to the top, ahead of whatever sort is active: they are
  // the rows that need a decision, and the user should not have to find them.
  const sorted = useMemo(() => {
    const rows = sortRows(positions, POSITION_COLUMNS, sort)
    return [...rows].sort((a, b) => Number(b.sl_breached) - Number(a.sl_breached))
  }, [positions, sort])

  const setExitMode = async (mode: 'auto' | 'manual') => {
    if (switching || manualExit === (mode === 'manual')) return
    setSwitching(true)
    try { await api.saveConfig({ exit_mode: mode }); refresh() } finally { setSwitching(false) }
  }

  const ctx: PositionCtx = {
    onStock, onEdit: p => setEditing(editing === p.id ? null : p.id), onRoll: setRolling,
    onAvg: setAvgFor,
    actions: p => (
      <div className="flex items-center justify-end gap-1">
        <button className="rounded-md border border-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 hover:bg-slate-50"
          onClick={() => setEditing(editing === p.id ? null : p.id)}>Edit</button>
        <button className="rounded-md border border-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 hover:bg-amber-50"
          onClick={() => setRolling(p)}>Roll</button>
        <ExitMenu p={p} onDone={refresh} />
      </div>
    ),
  }

  // No inner scroll: every open position is on the page at once (the user reads
  // this table top to bottom and a nested scrollbar hid rows below the fold).
  // The page scrolls instead — only the Signals feed keeps its own viewport.
  return (
    <section className="flex flex-col rounded-2xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-t-2xl border-b border-slate-100 px-3 py-1.5">
        <h2 className="text-[13px] font-bold text-slate-800">Open Positions <span className="font-medium text-slate-400">({positions.length})</span></h2>
        <div className="flex items-center gap-2">
          {manualExit && breachedCount > 0 && (
            <span className="rounded-md border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-bold text-amber-800">
              {breachedCount} stop{breachedCount > 1 ? 's' : ''} breached — awaiting you
            </span>
          )}
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Stoploss</span>
            <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
              {(['auto', 'manual'] as const).map(m => (
                <button key={m} disabled={switching} onClick={() => setExitMode(m)}
                  title={m === 'auto'
                    ? 'A stop breach squares the position off immediately'
                    : 'A stop breach only flags the position — nothing is sold until you act'}
                  className={`rounded-md px-2 py-0.5 text-[11px] font-semibold capitalize disabled:opacity-50 ${
                    (m === 'manual') === manualExit
                      ? (m === 'manual' ? 'bg-amber-500 text-white shadow-sm' : 'bg-white text-slate-800 shadow-sm')
                      : 'text-slate-500'}`}>{m}</button>
              ))}
            </div>
          </div>
          <ColumnPicker defs={POSITION_COLUMNS} hidden={hidden} onToggle={toggle} onReset={reset}
            label="Position columns" />
        </div>
      </div>
      {manualExit && (
        <div className="border-b border-amber-100 bg-amber-50/60 px-3 py-1 text-[10px] text-amber-800">
          Manual stoploss is on — a stop that breaks is highlighted and moved to the top instead of being
          sold. Switch back to <b>auto</b> and anything still below its stop is squared off at once.
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-[11px]">
          <ColumnHead cols={visible} sort={sort} onSort={toggleSort} />
          <tbody>
            {positions.length === 0 && (
              <tr><td colSpan={visible.length} className="px-3 py-10 text-center text-slate-400">No open positions.</td></tr>
            )}
            {sorted.map(p => (
              <Fragment key={p.id}>
                <tr className={`border-b border-slate-50 ${p.sl_breached
                  ? 'bg-amber-100/80 hover:bg-amber-100'
                  : 'hover:bg-slate-50/70'}`}>
                  <ColumnCells cols={visible} row={p} ctx={ctx} />
                </tr>
                {editing === p.id && <EditRow p={p} onDone={() => { setEditing(null); refresh() }} cols={visible.length} />}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {rolling && (
        <RolloverModal position={rolling} onClose={() => setRolling(null)} onDone={() => refresh()} />
      )}
      {avgFor && (
        <AveragingModal positionId={avgFor.id} symbol={avgFor.symbol} onClose={() => setAvgFor(null)} />
      )}
    </section>
  )
}
