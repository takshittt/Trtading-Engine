import { useEffect, useState } from 'react'
import { api } from '../api'
import type { LogRow } from '../types'
import { Badge, Card, fmtTime } from './ui'

const levelTone = (lvl: string): 'green' | 'red' | 'amber' | 'gray' | 'blue' =>
  lvl === 'error' ? 'red' : lvl === 'warn' ? 'amber' : lvl === 'trade' ? 'green' : lvl === 'math' ? 'blue' : 'gray'

function LogLine({ l }: { l: LogRow }) {
  return (
    <div className="flex items-start gap-2 px-2 py-1 rounded hover:bg-gray-100 text-[12px] leading-relaxed">
      <span className="text-gray-500 font-mono shrink-0 tabular-nums">{fmtTime(l.ts)}</span>
      <Badge tone={levelTone(l.level)}>{l.category || l.level}</Badge>
      {l.sym && <span className="text-sky-600 shrink-0 font-medium">{l.sym}</span>}
      <span className="text-gray-700 break-words min-w-0">{l.message}</span>
    </div>
  )
}

/** Latest-5 strip on the dashboard + full-history modal (1-month retention). */
export function LogsPanel({ liveLogs }: { liveLogs: LogRow[] }) {
  const [showAll, setShowAll] = useState(false)
  return (
    <Card title="System Logs" right={
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-gray-500">retained 1 month · live</span>
        <button onClick={() => setShowAll(true)}
          className="px-2 py-1 rounded border border-gray-300 text-gray-700 hover:border-sky-500 hover:text-sky-700 text-[11px]">
          View All Logs
        </button>
      </div>
    }>
      <div className="space-y-0.5">
        {liveLogs.length === 0 && <div className="text-gray-500 text-xs px-2 py-3">No events yet.</div>}
        {liveLogs.slice(0, 5).map((l, i) => <LogLine key={l.id ?? `${l.ts}-${i}`} l={l} />)}
      </div>
      {showAll && <LogsModal onClose={() => setShowAll(false)} />}
    </Card>
  )
}

export function LogsModal({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<LogRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [level, setLevel] = useState('')
  const [category, setCategory] = useState('')
  const [sym, setSym] = useState('')
  const [loading, setLoading] = useState(false)
  const PAGE = 100

  useEffect(() => {
    setLoading(true)
    api.logs(PAGE, page * PAGE, { level, category, sym })
      .then((r) => { setRows(r.logs); setTotal(r.total) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [page, level, category, sym])

  const sel = 'bg-gray-50 border border-gray-300 rounded-md px-2 py-1 text-xs text-gray-700'
  return (
    <div className="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-4xl h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <div className="font-semibold text-gray-900">All Logs <span className="text-gray-500 font-normal text-sm">({total})</span></div>
          <div className="flex items-center gap-2">
            <input placeholder="symbol…" value={sym} onChange={(e) => { setSym(e.target.value.toUpperCase()); setPage(0) }} className={sel + ' w-28'} />
            <select value={category} onChange={(e) => { setCategory(e.target.value); setPage(0) }} className={sel}>
              <option value="">All categories</option>
              {['SIGNAL', 'MATH', 'ORDER', 'EXIT', 'ROLLOVER', 'CB', 'RECONCILE', 'FEED', 'SYSTEM'].map((c) =>
                <option key={c} value={c}>{c}</option>)}
            </select>
            <select value={level} onChange={(e) => { setLevel(e.target.value); setPage(0) }} className={sel}>
              <option value="">All levels</option>
              {['info', 'math', 'trade', 'warn', 'error'].map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
            <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-2xl leading-none px-1">×</button>
          </div>
        </header>
        <div className="flex-1 overflow-y-auto p-3 space-y-0.5">
          {loading && <div className="text-gray-500 text-xs p-3">loading…</div>}
          {rows.map((l, i) => <LogLine key={l.id ?? i} l={l} />)}
          {!loading && rows.length === 0 && <div className="text-gray-500 text-sm p-4">Nothing matches.</div>}
        </div>
        <footer className="flex items-center justify-between px-5 py-2.5 border-t border-gray-200 text-xs text-gray-600">
          <span>Note: system retains logs for the last 1 month only.</span>
          <div className="flex items-center gap-2">
            <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}
              className="px-2 py-1 rounded border border-gray-300 disabled:opacity-40">‹ newer</button>
            <span>page {page + 1} / {Math.max(Math.ceil(total / PAGE), 1)}</span>
            <button disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}
              className="px-2 py-1 rounded border border-gray-300 disabled:opacity-40">older ›</button>
          </div>
        </footer>
      </div>
    </div>
  )
}
