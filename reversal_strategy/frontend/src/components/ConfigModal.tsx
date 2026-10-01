import { useEffect, useState } from 'react'
import type { Config } from '../types'
import { api } from '../api'
import { Modal, Field, Toggle, inputCls, btnPrimary, btnGhost } from './ui'

const GROUPS: { title: string; fields: { key: keyof Config; label: string; step?: number }[] }[] = [
  { title: 'Budget & Caps', fields: [
    { key: 'total_budget', label: 'Total Budget (₹)', step: 10000 },
    { key: 'soft_cap_pct', label: 'Soft Cap % (warn)' },
    { key: 'hard_cap_pct', label: 'Hard Cap % (block)' },
  ] },
  { title: 'Trailing & Risk', fields: [
    { key: 'trailing_buffer_pct', label: 'Trailing Buffer %', step: 0.1 },
    { key: 'global_sl_pct', label: 'Global MTM SL %' },
    { key: 'max_averaging_buys', label: 'Max Averaging Buys' },
  ] },
  { title: 'Execution & Ops', fields: [
    { key: 'order_retries', label: 'Order Retries' },
    { key: 'slippage_ticks', label: 'Slippage Ticks' },
    { key: 'reconcile_seconds', label: 'Reconcile Interval (s)' },
  ] },
  { title: 'Automated Execution', fields: [
    { key: 'auto_risk_pct', label: 'Risk % of Budget / Trade', step: 0.25 },
    { key: 'auto_max_lots', label: 'Max Lots / Trade' },
    { key: 'auto_max_positions', label: 'Max Open Positions' },
  ] },
]

export function ConfigModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [cfg, setCfg] = useState<Config | null>(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => { api.config().then(setCfg) }, [])
  if (!cfg) return <Modal title="Configuration" onClose={onClose}><div className="py-6 text-center text-slate-400">Loading…</div></Modal>

  const set = (k: keyof Config, v: any) => setCfg({ ...cfg, [k]: v })
  const reserve = Math.max(0, 100 - cfg.hard_cap_pct)

  return (
    <Modal title="Strategy Configuration" onClose={onClose} wide
      footer={
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-slate-400">Changes apply live to all new signals &amp; positions.</span>
          <div className="flex gap-2">
            <button className={btnGhost} onClick={onClose}>Cancel</button>
            <button className={btnPrimary} disabled={saving}
              onClick={async () => {
                setSaving(true)
                try { await api.saveConfig(cfg); onSaved(); onClose() } finally { setSaving(false) }
              }}>{saving ? 'Saving…' : 'Save Configuration'}</button>
          </div>
        </div>
      }>
      <div className="mb-3 rounded-xl border border-sky-100 bg-sky-50 px-3 py-2 text-[11px] text-sky-800">
        Averaging Reserve is locked at <b>{reserve.toFixed(0)}%</b> (= 100 − Hard Cap).
        These Target/SL settings apply to <b>all</b> signals; edit a single stock by clicking it.
      </div>

      {/* Paper trading */}
      <div className="mb-4 rounded-xl border border-indigo-200 bg-indigo-50/60 p-3">
        <div className="flex items-center justify-between">
          <div>
            <span className="text-xs font-bold text-indigo-900">Paper Trading</span>
            <p className="mt-0.5 text-[11px] text-indigo-700">
              Auto-executes <b>every</b> signal against the live Shoonya bid/ask — buys lift the ask,
              sells hit the bid — without sending a single broker order. Positions are tagged PAPER,
              lock no margin, and are excluded from the live budget, portfolio SL and reconciliation.
            </p>
          </div>
          <Toggle value={cfg.paper_trading ? 'on' : 'off'} options={['off', 'on']}
            onChange={v => set('paper_trading', v === 'on')} />
        </div>
        {cfg.paper_trading && (
          <div className="mt-2 w-40">
            <Field label="Lots per simulated buy">
              <input type="number" min={1} value={cfg.paper_lots}
                onChange={e => set('paper_lots', Math.max(1, +e.target.value))} className={inputCls} />
            </Field>
          </div>
        )}
      </div>

      {/* Auto rollover */}
      <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50/60 p-3">
        <div className="flex items-center justify-between">
          <div>
            <span className="text-xs font-bold text-amber-900">Automatic Rollover</span>
            <p className="mt-0.5 text-[11px] text-amber-800">
              Carries any open position to the next expiry before its contract runs out, at market,
              keeping P&amp;L and Target/SL. Without it, a position left on an expiring contract
              settles and stops matching the broker.
            </p>
          </div>
          <Toggle value={cfg.auto_rollover ? 'on' : 'off'} options={['off', 'on']}
            onChange={v => set('auto_rollover', v === 'on')} />
        </div>
        {cfg.auto_rollover && (
          <div className="mt-2 w-52">
            <Field label="Roll this many days before expiry">
              <input type="number" min={0} max={15} value={cfg.auto_rollover_days}
                onChange={e => set('auto_rollover_days', Math.max(0, +e.target.value))} className={inputCls} />
            </Field>
            <p className="mt-1 text-[10px] text-amber-700">
              Runs during market hours only, once per position per day.
            </p>
          </div>
        )}
      </div>

      {/* Market hours */}
      <div className="mb-4 rounded-xl border border-slate-200 p-3">
        <div className="flex items-center justify-between">
          <div>
            <span className="text-xs font-bold text-slate-700">Market Hours (IST)</span>
            <p className="mt-0.5 text-[11px] text-slate-500">
              The NSE F&amp;O session. Ticks arriving outside it are discarded, so a Saturday
              mock session can never move your P&amp;L. Weekends are always closed.
            </p>
          </div>
        </div>
        <div className="mt-2 grid grid-cols-2 gap-3">
          <Field label="Opens at">
            <input type="time" value={cfg.market_open_time}
              onChange={e => set('market_open_time', e.target.value)} className={inputCls} />
          </Field>
          <Field label="Closes at">
            <input type="time" value={cfg.market_close_time}
              onChange={e => set('market_close_time', e.target.value)} className={inputCls} />
          </Field>
        </div>
        <div className="mt-2">
          <Field label="Trading holidays — one date per line, YYYY-MM-DD">
            <textarea rows={3} value={cfg.market_holidays}
              onChange={e => set('market_holidays', e.target.value)}
              placeholder="2026-01-26&#10;2026-08-15"
              className={`${inputCls} font-mono text-[11px]`} />
          </Field>
          <p className="mt-1 text-[10px] text-slate-400">
            Update this each January — dates left in the past are harmless, but a year with
            no dates listed makes every holiday look like a normal session.
          </p>
        </div>

        <div className="mt-3 flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50/60 p-2.5">
          <div className="pr-3">
            <span className="text-[11px] font-bold text-rose-900">Force market open</span>
            <p className="mt-0.5 text-[11px] text-rose-700">
              Ignores the hours and holidays above and treats the market as always open — for
              mock sessions and demos. While this is on, ticks are accepted and exits can fire
              against a closed exchange. Turn it off when you are done.
            </p>
          </div>
          <Toggle value={cfg.ignore_market_hours ? 'on' : 'off'} options={['off', 'on']}
            onChange={v => set('ignore_market_hours', v === 'on')} />
        </div>
        {cfg.market_hours_bypassed && !cfg.ignore_market_hours && (
          <p className="mt-1.5 text-[10px] font-semibold text-rose-600">
            Hours are being bypassed by the SWING_IGNORE_MARKET_HOURS environment variable
            on the server — this toggle cannot switch that off.
          </p>
        )}
      </div>

      {/* Signal levels — support & resistance, then the R:R gate */}
      <div className="mb-4">
        <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-400">
          Target &amp; SL — applied to all signals
        </div>
        <div className="rounded-xl border border-slate-200 p-3">
          <p className="text-[11px] text-slate-500">
            Levels come from the signal's <b>support and resistance</b>: the resistance above the
            signal is the Target, the support below it is the Stop Loss. They are fixed prices on
            the chart, so they do not move as the stock does. The ATR multipliers below are only
            used when a signal arrives without S&amp;R.
          </p>
          <div className="mt-2 grid grid-cols-3 gap-3">
            <Field label="Minimum R:R to act">
              <input type="number" step={0.1} min={0} value={cfg.min_rr}
                onChange={e => set('min_rr', Math.max(0, +e.target.value))} className={inputCls} />
            </Field>
            <Field label="ATR × Target (fallback)">
              <input type="number" step={0.1} value={cfg.atr_target_mult}
                onChange={e => set('atr_target_mult', +e.target.value)} className={inputCls} />
            </Field>
            <Field label="ATR × SL (fallback)">
              <input type="number" step={0.1} value={cfg.atr_sl_mult}
                onChange={e => set('atr_sl_mult', +e.target.value)} className={inputCls} />
            </Field>
          </div>
          <p className="mt-1.5 text-[10px] text-slate-400">
            A signal whose reward:risk at its own signal price is below the minimum is stored and
            priced but marked <b>LOW R:R</b> and cannot be traded — so you can see what was filtered
            and why. Set to 0 to accept everything.
          </p>
        </div>
      </div>

      <div className="space-y-4">
        {GROUPS.map(g => (
          <div key={g.title}>
            <div className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-400">{g.title}</div>
            <div className="grid grid-cols-3 gap-3">
              {g.fields.map(f => (
                <Field key={f.key} label={f.label}>
                  <input type="number" step={f.step || 1} value={cfg[f.key] as number}
                    onChange={e => set(f.key, +e.target.value)} className={inputCls} />
                </Field>
              ))}
              {g.title === 'Execution & Ops' && (
                <Field label="Averaging Mode">
                  <select value={cfg.averaging_mode} onChange={e => set('averaging_mode', e.target.value)} className={inputCls}>
                    <option value="manual">Manual</option><option value="auto">Auto</option>
                  </select>
                </Field>
              )}
            </div>
            {g.title === 'Automated Execution' && (
              <p className="mt-1.5 rounded-lg border border-rose-100 bg-rose-50/60 px-2.5 py-1.5 text-[10px] leading-relaxed text-rose-800">
                Used only while <b>Execution = Auto</b>. Size is risk-based:
                {' '}<b>lots = (budget × risk%) ÷ ((entry − stop) × lot size)</b>, capped by Max Lots.
                A signal with no stop, or a stop that would risk more than one lot's worth, is
                skipped rather than guessed at. Automated execution does <b>nothing at all</b> while
                the Stoploss switch on Open Positions is set to Manual.
              </p>
            )}
          </div>
        ))}
      </div>

    </Modal>
  )
}
