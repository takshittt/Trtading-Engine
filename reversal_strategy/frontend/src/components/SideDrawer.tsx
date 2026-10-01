import { useEffect, useMemo, useState } from 'react'
import type { JournalStats, Position, Signal, LogLine } from '../types'
import { api } from '../api'
import { inr2, latency, pnlColor, dateShort, tfColor } from '../util'
import { Badge } from './ui'
import { ColumnCells, ColumnHead, ColumnPicker, useColumns, useSort, sortRows } from './ColumnPicker'
import { JOURNAL_COLUMNS, type JournalCtx } from './journalColumns'
import { AveragingModal } from './AveragingModal'

type Tab = 'journal' | 'blacklist' | 'history' | 'logs'
type Book = 'all' | 'live' | 'paper'
const TABS: [Tab, string][] = [['journal', 'Journal'], ['blacklist', 'Blacklist'], ['history', 'History'], ['logs', 'Logs']]

const STAT_CELLS: [string, (s: JournalStats) => string, boolean][] = [
  ['Trades', s => String(s.trades), false],
  ['Win Rate', s => `${s.win_rate}%`, false],
  ['Total P&L', s => inr2(s.total_pnl), true],
  ['Avg P&L', s => inr2(s.avg_pnl), true],
  ['Avg Win', s => inr2(s.avg_win), true],
  ['Avg Loss', s => inr2(s.avg_loss), true],
  // null means wins with no losses — mathematically undefined, not zero.
  ['Profit Factor', s => s.profit_factor === null ? '\u221e' : String(s.profit_factor), false],
  ['Max DD', s => inr2(s.max_drawdown), true],
  ['Best', s => inr2(s.best), true],
  ['Worst', s => inr2(s.worst), true],
  ['Avg Entry Lat.', s => latency(s.avg_exec_latency_ms), false],
  ['Avg Exit Lat.', s => latency(s.avg_exit_latency_ms), false],
  ['Avg Hold', s => `${s.avg_hold_minutes}m`, false],
  ['Avg MFE', s => inr2(s.avg_mfe), true],
  ['Avg MAE', s => inr2(s.avg_mae), true],
]

/** Collapsible right-side drawer opened by a vertical tab button. */
export function SideDrawer({ onStock, tick }: { onStock: (s: string) => void; tick: number }) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<Tab>('journal')
  const [book, setBook] = useState<Book>('all')
  const jcols = useColumns<Position, JournalCtx>('journal', JOURNAL_COLUMNS)
  const { sort: jSort, toggle: toggleJSort } = useSort('journal')
  const [trades, setTrades] = useState<Position[]>([])
  const [stats, setStats] = useState<JournalStats | null>(null)
  const [paperStats, setPaperStats] = useState<JournalStats | null>(null)
  const [blacklist, setBlacklist] = useState<{ symbol: string; reason: string }[]>([])
  const [history, setHistory] = useState<Signal[]>([])
  const [logs, setLogs] = useState<LogLine[]>([])
  const [avgFor, setAvgFor] = useState<Position | null>(null)

  const load = () => {
    api.journal(book).then(j => { setTrades(j.trades); setStats(j.stats); setPaperStats(j.paper_stats) })
    api.blacklist().then(setBlacklist)
    api.signalHistory().then(setHistory)
    api.logs().then(setLogs)
  }
  useEffect(() => { if (open) load() }, [tick, open, book])

  // The journal is a wide execution table; the other tabs are simple lists and
  // look stranded at that width, so the drawer sizes itself to the tab.
  const width = tab === 'journal' ? 'w-[1460px]' : 'w-[440px]'
  // Which stats block to headline — the book you're actually looking at.
  const shown = book === 'paper' ? paperStats : stats

  return (
    <>
      {/* Vertical tab button (always visible on the right edge) */}
      <button onClick={() => setOpen(true)}
        className="fixed right-0 top-1/2 z-30 -translate-y-1/2 rounded-l-xl border border-r-0 border-slate-200 bg-white px-2 py-4 text-xs font-bold text-slate-600 shadow-md hover:bg-slate-50"
        style={{ writingMode: 'vertical-rl' }}>
        Journal · Logs · Blacklist
      </button>

      {open && (
        <div className="fixed inset-0 z-40" onClick={() => setOpen(false)}>
          <div className="absolute inset-0 bg-slate-900/20" />
          <aside className={`slide-in absolute right-0 top-0 flex h-full ${width} max-w-[97vw] flex-col border-l border-slate-200 bg-white shadow-2xl`}
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center border-b border-slate-100">
              {TABS.map(([t, label]) => (
                <button key={t} onClick={() => setTab(t)}
                  className={`flex-1 px-2 py-3 text-[11px] font-bold ${tab === t ? 'border-b-2 border-sky-500 text-sky-600' : 'text-slate-400 hover:text-slate-600'}`}>{label}</button>
              ))}
              <button onClick={() => setOpen(false)} className="px-3 text-lg text-slate-400 hover:text-slate-700">×</button>
            </div>

            <div className="flex-1 overflow-auto p-3 text-xs">
              {tab === 'journal' && (
                <>
                  <div className="mb-2 flex items-center gap-2">
                    <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
                      {(['all', 'live', 'paper'] as Book[]).map(b => (
                        <button key={b} onClick={() => setBook(b)}
                          className={`rounded-md px-2.5 py-0.5 text-[11px] font-semibold capitalize ${book === b ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}>{b}</button>
                      ))}
                    </div>
                    <span className="text-[10px] text-slate-400">
                      Stats below are for the <b>{book === 'paper' ? 'paper' : 'live'}</b> book — the two are never mixed.
                    </span>
                    <div className="ml-auto">
                      <ColumnPicker defs={JOURNAL_COLUMNS} hidden={jcols.hidden}
                        onToggle={jcols.toggle} onReset={jcols.reset} label="Journal columns" />
                    </div>
                  </div>

                  {shown && (
                    <div className="mb-3 grid grid-cols-5 gap-2">
                      {STAT_CELLS.map(([k, fn, money]) => {
                        const v = fn(shown)
                        return (
                          <div key={k} className="rounded-xl border border-slate-100 bg-slate-50 p-2 text-center">
                            <div className="text-[10px] text-slate-400">{k}</div>
                            <div className={`text-sm font-bold ${money ? pnlColor(parseFloat(v.replace(/[₹,]/g, ''))) : 'text-slate-800'}`}>{v}</div>
                          </div>
                        )
                      })}
                    </div>
                  )}

                  {shown && Object.keys(shown.by_exit_reason).length > 0 && (
                    <div className="mb-3 flex flex-wrap items-center gap-1.5">
                      <span className="text-[10px] uppercase tracking-wide text-slate-400">Exits:</span>
                      {Object.entries(shown.by_exit_reason).map(([reason, v]) => (
                        <Badge key={reason} className="border-slate-200 bg-white text-slate-600">
                          {reason} ×{v.trades} <span className={pnlColor(v.pnl)}>{inr2(v.pnl)}</span>
                        </Badge>
                      ))}
                    </div>
                  )}

                  {trades.length === 0 && <div className="py-8 text-center text-slate-400">No closed trades yet.</div>}
                  {trades.length > 0 && (() => {
                    const sortedTrades = sortRows(trades, JOURNAL_COLUMNS, jSort)
                    return (
                    <div className="overflow-x-auto rounded-xl border border-slate-100">
                      <table className="w-full text-left text-[11px]">
                        <ColumnHead cols={jcols.visible} className="text-[9px]" sort={jSort} onSort={toggleJSort} />
                        <tbody>
                          {sortedTrades.map(t => (
                            <tr key={t.id} className={`border-b border-slate-50 ${t.is_paper ? 'bg-indigo-50/40' : ''}`}>
                              <ColumnCells cols={jcols.visible} row={t} ctx={{ onStock, onAvg: setAvgFor }}
                                pad="px-1.5 py-1" />
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    )
                  })()}
                </>
              )}

              {tab === 'blacklist' && (
                <>
                  {blacklist.length === 0 && <div className="py-8 text-center text-slate-400">Blacklist is empty.</div>}
                  {blacklist.map(b => (
                    <div key={b.symbol} className="mb-1.5 flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2">
                      <button className="font-bold text-slate-800 hover:text-sky-600" onClick={() => onStock(b.symbol)}>{b.symbol}</button>
                      <button className="rounded-lg border border-slate-200 px-2 py-0.5 text-[10px] text-slate-600 hover:border-rose-300 hover:text-rose-600"
                        onClick={async () => { await api.removeBlacklist(b.symbol); load() }}>Remove</button>
                    </div>
                  ))}
                </>
              )}

              {tab === 'history' && history.map(s => (
                <div key={s.id} className="mb-1.5 flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2">
                  <div>
                    <span className="font-bold text-slate-800">{s.symbol}</span>
                    <span className={`ml-1.5 text-[10px] font-bold ${s.signal_type === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>{s.signal_type}</span>
                    <Badge className={`ml-1.5 ${tfColor[s.timeframe]}`}>{s.timeframe}</Badge>
                  </div>
                  <div className="text-right">
                    <div className="text-[10px] text-slate-400">{dateShort(s.created_at)}</div>
                    <Badge className="border-slate-200 bg-slate-50 text-slate-500">{s.status}</Badge>
                  </div>
                </div>
              ))}

              {tab === 'logs' && (
                <div className="space-y-0.5 font-mono text-[10px]">
                  {logs.map((l, i) => (
                    <div key={i} className={`flex gap-2 ${l.level === 'ERROR' ? 'text-rose-600' : l.level === 'WARN' ? 'text-amber-600' : 'text-slate-500'}`}>
                      <span className="text-slate-300">{dateShort(l.created_at).split(', ')[1]}</span>
                      <span className="font-bold">{l.action}</span>
                      <span className="text-slate-400">{l.symbol}</span>
                      <span className="truncate">{l.detail}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
      {/* Outside the drawer's click-away wrapper — a click inside the dialog
          must not close the drawer underneath it. */}
      {avgFor && (
        <AveragingModal positionId={avgFor.id} symbol={avgFor.symbol} onClose={() => setAvgFor(null)} />
      )}
    </>
  )
}
