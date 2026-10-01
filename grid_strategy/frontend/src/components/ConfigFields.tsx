import type { InstrumentSnap } from '../types'

/** One form-state shape + one field grid shared by BOTH the automated-signal
 * ConfigModal and the manual LadderModal, so every strategy setting (targets,
 * sizing, guards, circuit breaker, rollover) is configurable in either place. */

export interface ConfigFormState {
  enabled: boolean
  target_mode: string
  target_value: number
  target_chain_pct: number
  sl_mode: string
  sl_value: number
  sl_enabled: boolean
  sizing_mode: string
  vol_formula: string
  risk_per_rung: number
  fixed_lots: number
  atr_period: number
  min_lots: number
  max_lots_per_rung: number
  max_rungs: number
  min_gap_points: number
  max_spread_points: number
  marketable_ticks: number  // marketable-limit buffer (ticks): buy @ ask+N, AMI_SELL @ bid−N
  sell_signal_mode: string
  cb_enabled: boolean
  cb_threshold: number
  rollover_days_before: number
  rollover_date_override: string   // '' = automated (system-suggested) | ISO date = manual
  fallback_symbol: string          // feed-down reference price symbol
  product_type: string      // "M" = NRML/delivery (carry) | "I" = MIS/intraday
  buy_order_type: string    // "LMT" | "MKT" — entries
  sell_order_type: string   // "LMT" | "MKT" — target exits (stop-loss always MKT)
  ladder_rearm: boolean     // ladder: re-arm a filled level once price recovers above it
}

export function configFormFromInst(inst: InstrumentSnap): ConfigFormState {
  const c = inst.config
  return {
    enabled: inst.enabled,
    target_mode: c.target_mode, target_value: c.target_value,
    target_chain_pct: c.target_chain_pct ?? 100,
    sl_mode: c.sl_mode, sl_value: c.sl_value,
    sl_enabled: c.sl_enabled !== false,
    sizing_mode: c.sizing_mode, vol_formula: c.vol_formula,
    risk_per_rung: c.risk_per_rung, fixed_lots: c.fixed_lots,
    atr_period: c.atr_period, min_lots: c.min_lots,
    max_lots_per_rung: c.max_lots_per_rung, max_rungs: c.max_rungs,
    min_gap_points: c.min_gap_points, max_spread_points: c.max_spread_points,
    marketable_ticks: c.marketable_ticks ?? 2,
    sell_signal_mode: c.sell_signal_mode,
    cb_enabled: inst.cb.enabled, cb_threshold: inst.cb.threshold,
    rollover_days_before: c.rollover_days_before,
    rollover_date_override: c.rollover_date_override || '',
    fallback_symbol: c.fallback_symbol || '',
    product_type: c.product_type || 'M',
    buy_order_type: c.buy_order_type || 'LMT',
    sell_order_type: c.sell_order_type || 'LMT',
    ladder_rearm: c.ladder_rearm ?? true,
  }
}

export const inputCls =
  'w-full bg-gray-50 border border-gray-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:border-sky-500'

export const L = ({ children }: { children: React.ReactNode }) =>
  <label className="text-[11px] uppercase tracking-wide text-gray-500">{children}</label>

export function ConfigFields({ form, set, isLadder = false }: {
  form: ConfigFormState
  set: (k: string, v: unknown) => void
  isLadder?: boolean
  /** roll to the next contract NOW (window-override); hidden if not provided */
}) {
  const num = (v: string) => (v === '' ? 0 : Number(v))
  const ModeBtns = ({ field, value }: { field: string; value: string }) => (
    <div className="flex gap-1">
      {(['points', 'percent'] as const).map((m) => (
        <button key={m} onClick={() => set(field, m)}
          className={`px-2 py-1 rounded text-[11px] border ${value === m ? 'bg-sky-500/20 border-sky-500 text-sky-700' : 'border-gray-300 text-gray-600'}`}>
          {m === 'points' ? 'pts' : '%'}
        </button>
      ))}
    </div>
  )
  // render-helper (a plain function, NOT a component) so per-instrument order-type
  // buttons stay lint-clean and don't reset state on re-render.
  const orderTypeBtns = (field: string, value: string) => (
    <div className="flex gap-1 mt-1">
      {([['LMT', 'Limit'], ['MKT', 'Market']] as const).map(([v, lbl]) => (
        <button key={v} type="button" onClick={() => set(field, v)}
          className={`flex-1 px-2 py-1.5 rounded-lg text-xs font-semibold border ${value === v
            ? 'bg-sky-500/20 border-sky-500 text-sky-700'
            : 'border-gray-300 text-gray-600 hover:text-gray-800'}`}>
          {lbl}
        </button>
      ))}
    </div>
  )

  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-4">
      <div className="col-span-2 flex items-center justify-between bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
        <span className="text-sm text-gray-700">Strategy enabled for this instrument</span>
        <input type="checkbox" checked={form.enabled} onChange={(e) => set('enabled', e.target.checked)}
          className="w-4 h-4 accent-emerald-500" />
      </div>

      {/* ---- stop-loss master switch — applies to new AND already-open rungs ---- */}
      <div className="col-span-2 flex items-center justify-between bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
        <div>
          <div className="text-sm text-gray-700">Stop-losses</div>
          <div className="text-[11px] text-gray-500">
            {form.sl_enabled
              ? 'Enabled — applies to new buys and recomputes stops on already-open rungs.'
              : 'DISABLED — every rung (new and open) trades WITHOUT a stop.'}
          </div>
        </div>
        <button type="button" onClick={() => set('sl_enabled', !form.sl_enabled)}
          className={`px-3 py-1.5 rounded-lg text-xs font-semibold border whitespace-nowrap ${form.sl_enabled
            ? 'border-emerald-600 text-emerald-700 bg-emerald-500/10'
            : 'border-rose-600 text-rose-700 bg-rose-500/10'}`}>
          {form.sl_enabled ? '● ENABLED · disable' : '○ DISABLED · enable'}
        </button>
      </div>

      {/* ---- order routing: delivery (NRML) vs intraday (MIS) ---- */}
      <div className="col-span-2 flex items-center justify-between bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
        <div>
          <div className="text-sm text-gray-700">Order product</div>
          <div className="text-[11px] text-gray-500">
            {form.product_type === 'I'
              ? 'Intraday (MIS) — auto-squared off by the broker at session end.'
              : 'Delivery (NRML) — positional, carries overnight. Default.'}
          </div>
        </div>
        <div className="flex gap-1">
          {([['M', 'Delivery'], ['I', 'Intraday']] as const).map(([v, lbl]) => (
            <button key={v} onClick={() => set('product_type', v)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold border ${form.product_type === v
                ? (v === 'M' ? 'bg-emerald-500/20 border-emerald-600 text-emerald-700'
                  : 'bg-amber-500/20 border-amber-600 text-amber-600')
                : 'border-gray-300 text-gray-600 hover:text-gray-800'}`}>
              {lbl}
            </button>
          ))}
        </div>
      </div>

      {/* ---- order type: limit (default) avoids slippage; SL always market ---- */}
      <div className="col-span-2 text-xs font-semibold text-sky-600 border-b border-gray-200 pb-1">
        ORDER TYPE (limit = no slippage buffer; market = always fills)
      </div>
      <div>
        <L>Buy orders (entries)</L>
        {orderTypeBtns('buy_order_type', form.buy_order_type)}
      </div>
      <div>
        <L>Sell orders (target exits)</L>
        {orderTypeBtns('sell_order_type', form.sell_order_type)}
      </div>
      {/* ---- ladder re-arm (range-bound re-buy) ---- */}
      {isLadder && (
        <div className="col-span-2 flex items-center justify-between bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
          <div>
            <div className="text-sm text-gray-700">Recycle levels (range-bound re-buy)</div>
            <div className="text-[11px] text-gray-500">
              A filled level stays <span className="font-semibold">executed</span> until its rung hits
              <span className="font-semibold"> target</span> — then it goes active again and re-buys on the next dip.
              If the rung hits its <span className="font-semibold">stop-loss</span> instead, the level retires (no re-buy).
              Off = each level fires exactly once (the ladder is finite either way — it never extends downward).
            </div>
          </div>
          <input type="checkbox" checked={form.ladder_rearm} onChange={(e) => set('ladder_rearm', e.target.checked)}
            className="w-4 h-4 accent-emerald-500" />
        </div>
      )}

      {/* ---- exits (AUTO instruments only — ladder target/SL/chaining live in the Levels view) ---- */}
      {!isLadder && (<>
      <div className="col-span-2 text-xs font-semibold text-sky-600 border-b border-gray-200 pb-1">
        TARGET &amp; STOP-LOSS (per rung)
      </div>
      <div>
        <L>Target offset — first rung exits at its own buy + this</L>
        <div className="flex gap-2 mt-1">
          <input type="number" step="any" value={form.target_value} onChange={(e) => set('target_value', num(e.target.value))} className={inputCls} />
          <ModeBtns field="target_mode" value={form.target_mode} />
        </div>
      </div>
      <div>
        <L>Chained-rung target — % of the offset added to the previous buy</L>
        <div className="flex gap-2 mt-1 items-center">
          <input type="number" step="any" min="0" value={form.target_chain_pct}
            onChange={(e) => set('target_chain_pct', num(e.target.value))} className={inputCls} />
          <span className="text-sm text-gray-500">%</span>
        </div>
        <div className="text-[10px] text-gray-500 mt-1">
          Lower rung target = previous buy + {(((Number(form.target_chain_pct) || 0) / 100) * (Number(form.target_value) || 0)).toFixed(2)}
          {form.target_mode === 'percent' ? '%' : ' pts'} ({form.target_chain_pct || 0}% of {form.target_value || 0}).
          100 = full offset (old behaviour).
        </div>
      </div>
      <div>
        <L>Stop-loss offset — rung N stops at its own buy − this</L>
        <div className="flex gap-2 mt-1">
          <input type="number" step="any" value={form.sl_value} onChange={(e) => set('sl_value', num(e.target.value))} className={inputCls} />
          <ModeBtns field="sl_mode" value={form.sl_mode} />
        </div>
      </div>
      <div>
        <L>On SELL signal</L>
        <select value={form.sell_signal_mode} onChange={(e) => set('sell_signal_mode', e.target.value)} className={inputCls + ' mt-1'}>
          <option value="exit_all">Exit ALL open rungs</option>
          <option value="ignore">Ignore (targets/SL only)</option>
        </select>
      </div>
      <div>
        <L>Max rungs (max simultaneous open buys)</L>
        <input type="number" value={form.max_rungs} onChange={(e) => set('max_rungs', num(e.target.value))} className={inputCls + ' mt-1'} />
      </div>
      </>)}

      {/* ---- sizing ---- */}
      <div className="col-span-2 text-xs font-semibold text-sky-600 border-b border-gray-200 pb-1">
        POSITION SIZING (volatility targeting — anti-martingale)
      </div>
      <div>
        <L>Sizing mode</L>
        <select value={form.sizing_mode} onChange={(e) => set('sizing_mode', e.target.value)} className={inputCls + ' mt-1'}>
          <option value="vol_target">Volatility targeting (ATR)</option>
          <option value="fixed">Fixed lots</option>
        </select>
      </div>
      <div>
        <L>{form.sizing_mode === 'fixed'
          ? `Fixed lots per ${isLadder ? 'level' : 'signal'}`
          : 'Fixed lots (fallback when ATR unavailable)'}</L>
        <input type="number" value={form.fixed_lots} onChange={(e) => set('fixed_lots', num(e.target.value))} className={inputCls + ' mt-1'} />
      </div>
      {form.sizing_mode === 'vol_target' && (
        <>
          <div>
            <L>Formula</L>
            <select value={form.vol_formula} onChange={(e) => set('vol_formula', e.target.value)} className={inputCls + ' mt-1'}>
              <option value="notional">risk ÷ (price × ATR) — spec</option>
              <option value="atr_rupee">risk ÷ (ATR × lot size) — ₹ risk</option>
            </select>
          </div>
          <div>
            <L>Target risk per rung (₹)</L>
            <input type="number" value={form.risk_per_rung} onChange={(e) => set('risk_per_rung', num(e.target.value))} className={inputCls + ' mt-1'} />
          </div>
          <div>
            <L>ATR period{isLadder ? ' (computed on 4h candles)' : " (on the signal's timeframe)"}</L>
            <input type="number" value={form.atr_period} onChange={(e) => set('atr_period', num(e.target.value))} className={inputCls + ' mt-1'} />
          </div>
          <div>
            <L>Min lots (0 = skip buy when unaffordable)</L>
            <input type="number" value={form.min_lots} onChange={(e) => set('min_lots', num(e.target.value))} className={inputCls + ' mt-1'} />
          </div>
          <div>
            <L>Max lots per rung</L>
            <input type="number" value={form.max_lots_per_rung} onChange={(e) => set('max_lots_per_rung', num(e.target.value))} className={inputCls + ' mt-1'} />
          </div>
        </>
      )}

      {/* ---- guards ---- */}
      <div className="col-span-2 text-xs font-semibold text-sky-600 border-b border-gray-200 pb-1">
        ENTRY GUARDS &amp; CIRCUIT BREAKER
      </div>
      <div>
        <L>Min dip gap below last rung (pts, 0 = off)</L>
        <input type="number" step="any" value={form.min_gap_points} onChange={(e) => set('min_gap_points', num(e.target.value))} className={inputCls + ' mt-1'} />
      </div>
      <div>
        <L>Max bid-ask spread to enter (pts, 0 = off)</L>
        <input type="number" step="any" value={form.max_spread_points} onChange={(e) => set('max_spread_points', num(e.target.value))} className={inputCls + ' mt-1'} />
      </div>
      <div>
        <L>Circuit breaker enabled</L>
        <div className="mt-1.5">
          <input type="checkbox" checked={form.cb_enabled} onChange={(e) => set('cb_enabled', e.target.checked)}
            className="w-4 h-4 accent-emerald-500" />
        </div>
      </div>
      <div>
        <L>Loss threshold ₹ (trips breaker, blocks new buys)</L>
        <input type="number" value={form.cb_threshold} onChange={(e) => set('cb_threshold', num(e.target.value))} className={inputCls + ' mt-1'} />
      </div>

      {/* ---- feed-down fallback price source ---- */}
      <div className="col-span-2 text-xs font-semibold text-sky-600 border-b border-gray-200 pb-1">
        FEED-DOWN FALLBACK (reference price if Shoonya feed dies — informational, never auto-trades)
      </div>
      <div className="col-span-2">
        <L>Fallback symbol (yfinance/your source) — e.g. NG=F, CL=F, GC=F, ^NSEI</L>
        <input type="text" value={form.fallback_symbol}
          onChange={(e) => set('fallback_symbol', e.target.value)}
          placeholder="blank = no fallback reference for this instrument"
          className={inputCls + ' mt-1'} />
        <div className="text-[11px] text-gray-500 mt-1">
          Shown as a DELAYED proxy in the manual-exit alert when the broker feed is down — not the exact MCX price.
        </div>
      </div>
    </div>
  )
}
