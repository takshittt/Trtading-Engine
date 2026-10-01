import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { computeSl, computeTarget } from '../ladderMath'
import type { InstrumentSnap, LadderBasis } from '../types'
import { configFormFromInst, inputCls, L } from './ConfigFields'
import { Badge, fmtPx, fmtTime } from './ui'

interface LevelDraft {
  price: string
  lots: string      // '' = auto (strategy sizing math)
  target: string    // '' = auto (chained target math on actual fill); a value pins the target
  sl: string        // '' = auto (SL offset math); a value pins this level's stop-loss
  note: string
  fireCount: number // times this level has already executed (display-only; re-arm history)
}

/** Manual-ladder editor: pick the level basis (4h-pivot strong supports or
 * fixed price intervals), generate/edit the buy levels B1…Bn (price AND lots
 * per level, plus per-level target/SL), then Confirm to arm the ladder.
 * The ladder is FINITE — exactly the configured levels exist; nothing is
 * auto-appended below the last one. */
export function LadderModal({ inst, onClose, onSaved }: {
  inst: InstrumentSnap
  onClose: () => void
  onSaved: () => void
}) {
  const lad = inst.ladder
  const [basis, setBasis] = useState<LadderBasis>(lad?.basis ?? 'fixed')
  const [anchor, setAnchor] = useState(String(lad?.anchor_price || inst.price.lp || ''))
  // Anchor defaults to the live LTP and keeps tracking it as snapshots refresh,
  // until the user types a value. A saved ladder keeps its configured anchor.
  const [anchorTouched, setAnchorTouched] = useState(false)
  const [interval, setIntervalPts] = useState(String(lad?.interval_points || ''))
  const [numLevels, setNumLevels] = useState(String(lad?.num_levels || 5))
  const [lookback, setLookback] = useState(String(lad?.sr_lookback_days || 45))
  const [levels, setLevels] = useState<LevelDraft[]>(() =>
    (lad?.levels ?? [])
      .filter((l) => l.status === 'PENDING')
      .map((l) => ({
        price: String(l.price), lots: l.lots_override ? String(l.lots_override) : '',
        target: l.target_override ? String(l.target_override) : '',
        sl: l.sl_override ? String(l.sl_override) : '', note: l.note,
        fireCount: l.fire_count || 0,
      })))
  // Read-only snapshot of the strategy config (owned by the ⚙ Config button).
  // Kept here only to drive the per-level auto-target preview below.
  const [config] = useState(() => configFormFromInst(inst))
  // Target & stop-loss are configured HERE now (not in ⚙ Config): the first-buy
  // target/SL define the offsets the chained auto-math uses for every level.
  // Stop-loss on/off is a Config-owned setting now (⚙ Config) — read-only here.
  const slEnabled = inst.config.sl_enabled !== false
  const [firstTarget, setFirstTarget] = useState('')
  const [firstTargetTouched, setFirstTargetTouched] = useState(false)
  const [firstSl, setFirstSl] = useState('')
  const [firstSlTouched, setFirstSlTouched] = useState(false)
  // grid target offset (% of interval added above one interval); 0 = target one interval up
  const [chainPctStr, setChainPctStr] = useState(String(inst.config.target_chain_pct ?? 0))
  const [ltp, setLtp] = useState(inst.price.lp)
  const ltpSnapped = useRef(false)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  // Set by "Generate levels": the drafts then ARE the whole intended ladder.
  const [regenerated, setRegenerated] = useState(false)
  // Which draft's price input has focus, and the row order captured before it
  // did — so the table stops re-sorting under a half-typed number.
  const [editingRow, setEditingRow] = useState<number | null>(null)
  const frozenOrder = useRef<string[]>([])

  // Non-draft levels shown in place: FILLED/TRIGGERED (rung open), PLACED
  // (resting order at broker), SKIPPED (user-cancelled, re-establishable),
  // AWAIT_RECOVERY (re-places when price recovers above the level).
  // CANCELLED stays VISIBLE too: a rung that expired overnight or was retired is
  // history the user needs to see, and hiding it also renumbered every rung below
  // it — the same order read as B2 here and B3 in the trades table.
  const execLevels = (lad?.levels ?? []).filter((l) => l.status !== 'PENDING')
  // A retired rung is shown, but must not anchor the target chain or block its
  // price from being used again.
  const liveExecLevels = execLevels.filter((l) => l.status !== 'CANCELLED')
  // Levels where a buy ACTUALLY HAPPENED. Only these may block a draft at the
  // same price, because only these would buy the rung a second time. PLACED (an
  // order merely resting), SKIPPED and AWAIT_RECOVERY have bought nothing, and
  // treating them as executed refused legitimate ladders with a wrong reason.
  const executedLevels = execLevels.filter(
    (l) => l.status === 'FILLED' || l.status === 'TRIGGERED')
  // Levels holding a LIVE resting order at the broker. Confirm replaces the
  // whole active ladder from the payload it is given, and these are shown in
  // this dialog as part of the ladder — so they have to travel with it. They
  // did not, and Confirm silently destroyed every one of them: a 3-level ladder
  // whose middle level had a resting buy came back as 2 levels, with no message
  // and no way to tell where the third went.
  const placedLevels = execLevels.filter((l) => l.status === 'PLACED')
  // Persisted levels safe to wipe with "Clear all levels" — mirrors the backend's
  // clearable set (no confirmed position behind them). Drives whether the button
  // shows and how many it reports.
  const clearableCount = (lad?.levels ?? []).filter(
    (l) => l.status === 'PENDING' || l.status === 'SKIPPED'
      || l.status === 'AWAIT_RECOVERY' || l.status === 'PLACED').length
  const num = (v: string) => (v === '' ? 0 : Number(v))

  // ---- keep the drafts in sync with levels that FIRE while the modal is open ----
  // The draft list is seeded once, from the PENDING levels as they were when the
  // modal opened. The ladder keeps trading behind it: a level can trigger during
  // the minute you spend editing. Confirm replaces the PENDING set with these
  // drafts, so a level that already executed gets re-inserted as PENDING and
  // fires a SECOND buy at the same price within ~1s of arming.
  //
  // `inst` refreshes on the 1s snapshot, so watch it and drop any draft whose
  // price now belongs to an executed level. Only removals — never touch prices,
  // lots or targets the user is editing.
  const [firedNotice, setFiredNotice] = useState<string[]>([])
  // Snapshot the LTP ONCE — the first non-zero price seen after the modal opens —
  // and prefill the anchor from it. `inst` keeps refreshing on the 1s snapshot
  // for the rest of the app, but this modal must not keep dragging the anchor
  // (or the displayed LTP) along with it while the user is reading/editing.
  useEffect(() => {
    if (ltpSnapped.current || !(inst.price.lp > 0)) return
    ltpSnapped.current = true
    setLtp(inst.price.lp)
    if (!lad?.anchor_price && !anchorTouched) setAnchor(String(inst.price.lp))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inst.price.lp])
  const execPriceKey = executedLevels.map((l) => l.price.toFixed(2)).sort().join(',')
  useEffect(() => {
    const execPrices = new Set(executedLevels.map((l) => l.price.toFixed(2)))
    if (!execPrices.size) return
    setLevels((ls) => {
      const gone = ls.filter((l) => execPrices.has((Number(l.price) || 0).toFixed(2)))
      if (!gone.length) return ls
      setFiredNotice((n) => [...new Set([...n, ...gone.map((g) => g.price)])])
      return ls.filter((l) => !execPrices.has((Number(l.price) || 0).toFixed(2)))
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [execPriceKey])

  const generate = async () => {
    setBusy('generate')
    setErr('')
    try {
      const r = await api.ladderPreview(inst.id, {
        basis, anchor_price: num(anchor), interval_points: num(interval),
        num_levels: Math.max(num(numLevels) || 5, 1), sr_lookback_days: num(lookback) || 45,
      })
      if (r.ltp > 0) setLtp(r.ltp)
      setLevels(r.levels.map((l) => ({ price: String(l.price), lots: '', target: '', sl: '', note: l.note, fireCount: 0 })))
      // Generating is an explicit "rebuild the ladder from these settings", so
      // the generated set is the whole ladder — a level that merely had an order
      // resting is not silently re-added on top of it.
      setRegenerated(true)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'level generation failed')
    } finally {
      setBusy('')
    }
  }

  const confirm = async () => {
    // Final guard against the fire-while-editing race (the effect above handles
    // it as the snapshot arrives; this catches a level that fired in the last
    // second). Re-posting an already-executed level buys the same rung twice.
    const execPrices = new Set(executedLevels.map((l) => l.price.toFixed(2)))
    const dupes = levels.filter((l) => execPrices.has((Number(l.price) || 0).toFixed(2)))
    if (dupes.length) {
      setErr(`${dupes.map((d) => d.price).join(', ')} already executed while this dialog was open — `
           + 'removed from the ladder so Confirm cannot buy them a second time. Review and Confirm again.')
      setLevels((ls) => ls.filter((l) => !execPrices.has((Number(l.price) || 0).toFixed(2))))
      return
    }
    const parsed = levels
      .map((l) => ({
        price: Number(l.price),
        lots_override: l.lots === '' ? null : Number(l.lots),
        target_override: l.target === '' ? null : Number(l.target),
        sl_override: !slEnabled || l.sl === '' ? null : Number(l.sl),
      }))
      .filter((l) => l.price > 0)
    // Carry the levels that hold a live resting order. Confirm cancels those
    // orders and rebuilds the ladder from this payload, so a level left out of
    // it is deleted — which is why they used to vanish: a 3-level ladder whose
    // middle level had a resting buy came back as 2, because only the PENDING
    // levels were ever editable and only the editable ones were sent. Their own
    // per-level overrides travel with them so nothing the user set is lost.
    // Skipped after Generate, where the user has deliberately rebuilt the ladder.
    if (!regenerated) {
      const draftPrices = new Set(parsed.map((l) => l.price.toFixed(2)))
      for (const lv of placedLevels) {
        if (draftPrices.has(lv.price.toFixed(2))) continue   // already present as a draft
        parsed.push({
          price: lv.price,
          lots_override: lv.lots_override ?? null,
          target_override: lv.target_override ?? null,
          sl_override: slEnabled ? (lv.sl_override ?? null) : null,
        })
      }
    }
    if (parsed.length === 0) {
      setErr('add at least one level (use Generate, or + Add level)')
      return
    }
    // target/SL are configured HERE — validate them here, not in ⚙ Config.
    const haveTargetOffset = (Number(config.target_value) || 0) > 0
    if (firstTarget === '' && !haveTargetOffset) {
      setErr('Set "Target — first buy exits at" above before arming — without it the target would be 0 and every rung would exit instantly.')
      return
    }
    if (firstTarget !== '' && b1Price > 0 && Number(firstTarget) <= b1Price) {
      setErr(`The first-buy target ${firstTarget} must be ABOVE the first buy level ${b1Price}.`)
      return
    }
    if (slEnabled) {
      const haveSlOffset = (Number(config.sl_value) || 0) > 0
      if (firstSl === '' && !haveSlOffset) {
        setErr('Set "Stop-loss — first buy stops at" above (or disable stop-losses) before arming — without it the rungs would be unprotected.')
        return
      }
      if (firstSl !== '' && b1Price > 0 && Number(firstSl) >= b1Price) {
        setErr(`The first-buy stop-loss ${firstSl} must be BELOW the first buy level ${b1Price}.`)
        return
      }
    }
    setBusy('confirm')
    setErr('')
    try {
      await api.ladderConfirm(inst.id, {                   // levels + ladder settings, arms the ladder
        basis, anchor_price: num(anchor), interval_points: num(interval),
        num_levels: Math.max(num(numLevels) || 5, 1), sr_lookback_days: num(lookback) || 45,
        levels: parsed,
        first_target: firstTarget === '' ? null : Number(firstTarget),
        first_sl: !slEnabled || firstSl === '' ? null : Number(firstSl),
        // sl_enabled is owned by ⚙ Config now — never write it from here, or a
        // stale read of it would silently flip the toggle back on Confirm.
        sl_enabled: null,
        target_chain_pct: chainPctStr === '' ? null : Math.max(Number(chainPctStr), 0),
      })
      onSaved()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'confirm failed')
    } finally {
      setBusy('')
    }
  }

  const setLevel = (i: number, patch: Partial<LevelDraft>) =>
    setLevels((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)))

  // Remove an EXECUTED / retired level from the ladder list. This drops only the
  // level row — an open rung keeps its position and its target/stop (managed from
  // the Trades tab); it just stops being part of the ladder (no more recycle).
  const removeExecLevel = async (t: NonNullable<typeof lad>['levels'][number]) => {
    const isOpen = t.status === 'FILLED' || t.status === 'TRIGGERED'
    const msg = isOpen
      ? `Remove level @ ${fmtPx(t.price)} from the ladder?\n\n`
        + 'This ONLY removes it from the levels list. The OPEN position stays open and keeps its '
        + 'target/stop — manage it in the Trades tab. The level will no longer recycle/re-arm.'
      : `Remove the retired level @ ${fmtPx(t.price)} from the ladder history?`
    if (!window.confirm(msg)) return
    setBusy(`rm${t.id}`)
    setErr('')
    try { await api.deleteLadderLevel(t.id); onSaved() }
    catch (e) { setErr(e instanceof Error ? e.message : 'remove failed') }
    finally { setBusy('') }
  }

  // ---- ONE table for the whole ladder: executed levels stay IN PLACE (flagged,
  // read-only) with the pending next-buys around them, all sorted by price.
  type LevelRowU =
    | { kind: 'exec'; lv: NonNullable<typeof lad>['levels'][number] }
    | { kind: 'draft'; i: number; l: LevelDraft }
  const rowPrice = (r: LevelRowU) => (r.kind === 'exec' ? r.lv.price : Number(r.l.price) || 0)
  // A level that is merely QUEUED — resting at the broker, skipped, or waiting
  // for price to recover — is the same rung as a draft at that price, not a
  // second one. Generating levels re-proposes prices the ladder already holds,
  // so showing both listed 15,390 twice, once read-only and once editable, as
  // if the ladder were about to buy it twice. The draft is the editable version
  // of that level and Confirm sends exactly one of them, so show exactly one.
  // FILLED/TRIGGERED rows are never folded away: those are rungs that really
  // bought, and they have to stay visible in place whatever the drafts say.
  const draftPriceSet = new Set(levels.map((l) => (Number(l.price) || 0).toFixed(2)))
  const QUEUED = ['PLACED', 'SKIPPED', 'AWAIT_RECOVERY']
  const visibleExecLevels = execLevels.filter(
    (l) => !(QUEUED.includes(l.status) && draftPriceSet.has(l.price.toFixed(2))))
  const rowKey = (r: LevelRowU) => (r.kind === 'exec' ? `x${r.lv.id}` : `d${r.i}`)
  const unsorted: LevelRowU[] = [
    ...visibleExecLevels.map((lv) => ({ kind: 'exec' as const, lv })),
    ...levels.map((l, i) => ({ kind: 'draft' as const, i, l })),
  ]
  const byPrice = [...unsorted].sort((a, b) => rowPrice(b) - rowPrice(a))  // highest first
  // Rows are ordered by price, but NOT while a price is being typed.
  //
  // Editing a price means passing through every truncation of it: clearing
  // 15390 to retype it goes 1539, 153, 15, 1, and re-sorting on each keystroke
  // dragged the row you were editing down past the others and renumbered it
  // B1 → B2 → B3 under the cursor. There is no way to edit a price without
  // going through values that sort elsewhere, so the sort has to wait until the
  // edit is finished. The order freezes on focus and re-sorts on blur.
  const rows: LevelRowU[] =
    editingRow === null
      ? byPrice
      : [...unsorted].sort((a, b) => {
          const ia = frozenOrder.current.indexOf(rowKey(a))
          const ib = frozenOrder.current.indexOf(rowKey(b))
          if (ia < 0 || ib < 0) return rowPrice(b) - rowPrice(a)   // row added mid-edit
          return ia - ib
        })
  useEffect(() => {
    if (editingRow === null) frozenOrder.current = byPrice.map(rowKey)
  })

  // GRID target preview — every level is SELF-ANCHORED and frozen:
  //   target = this level's OWN price + offset (no chaining to the rung above).
  // Frozen at Confirm, so it does not move when the buy price shifts down on a
  // cheaper fill — a cheaper fill just widens that rung's profit.
  // B1 = the highest level (exec or draft) — first-buy target/SL derive the
  // offsets from it, exactly like the backend does on Confirm.
  const allPrices = [...liveExecLevels.map((l) => l.price), ...levels.map((l) => Number(l.price) || 0)]
    .filter((p) => p > 0)
  const b1Price = allPrices.length ? Math.max(...allPrices) : 0
  // GRID offset: this % is the extra above ONE interval, as a fraction of the
  // interval — the target sits interval × (1 + %/100) above each level. Empty = 0
  // → target exactly one interval up. e.g. interval 100 @ 50% → +150 per rung.
  const chainPct = chainPctStr === '' || isNaN(Number(chainPctStr)) ? 0 : Number(chainPctStr)
  const offsetFrac = chainPct / 100
  const effTgtPts = firstTarget !== '' && b1Price > 0 ? Number(firstTarget) - b1Price : null
  // The preview no longer re-implements the formula — it feeds the SAME
  // functions the backend mirrors (src/ladderMath.ts, pinned to core/exits.py by
  // shared vectors). Typing a first-buy target simply expresses that choice as a
  // points offset, which is exactly what Confirm stores on the instrument.
  const previewTgtInst = {
    target_mode: effTgtPts != null ? 'points' : config.target_mode,
    target_value: effTgtPts != null ? effTgtPts : (Number(config.target_value) || 0),
    target_chain_pct: chainPct,
  }
  // Auto SL preview per level — same source as the engine's compute_sl; the
  // first-buy SL input overrides the offset the same way the target does.
  const slAuto = (price: number): number | null => {
    if (!slEnabled || !(price > 0)) return null
    const usingFirstSl = firstSl !== '' && b1Price > 0
    const v = computeSl({
      sl_enabled: true,
      sl_mode: usingFirstSl ? 'points' : config.sl_mode,
      sl_value: usingFirstSl ? b1Price - Number(firstSl) : (Number(config.sl_value) || 0),
    }, price)
    return v > 0 ? Math.round(v * 100) / 100 : null
  }
  // First-buy target/SL auto-default to buy1 ± the interval (fixed basis):
  // buy1 263.70 with interval 3 → target 266.70, stop 260.70. Support basis
  // (no interval) falls back to the configured offset. The auto value SEEDS the
  // input, so its up/down spinner nudges ±1 from that number (e.g. 266.70 →
  // 267.70 / 265.70) instead of starting at 1. Clearing a field re-enables auto.
  const intervalPts = Number(interval) || 0
  // GRID: auto target offset = interval + offset% of the interval, so a
  // cheaper fill still exits interval × (1 + offset%) above where we got in.
  const autoTgtOff = intervalPts > 0 ? intervalPts * (1 + offsetFrac)
    : config.target_mode === 'percent' ? b1Price * (Number(config.target_value) || 0) / 100
      : (Number(config.target_value) || 0)
  const autoSlOff = intervalPts > 0 ? intervalPts
    : config.sl_mode === 'percent' ? b1Price * (Number(config.sl_value) || 0) / 100
      : (Number(config.sl_value) || 0)
  const autoFirstTarget = b1Price > 0 && autoTgtOff > 0 ? Math.round((b1Price + autoTgtOff) * 100) / 100 : null
  const autoFirstSl = b1Price > 0 && autoSlOff > 0 ? Math.round((b1Price - autoSlOff) * 100) / 100 : null
  useEffect(() => {
    if (!firstTargetTouched) setFirstTarget(autoFirstTarget != null ? String(autoFirstTarget) : '')
  }, [autoFirstTarget, firstTargetTouched])
  useEffect(() => {
    if (!firstSlTouched) setFirstSl(autoFirstSl != null ? String(autoFirstSl) : '')
  }, [autoFirstSl, firstSlTouched])
  const editFirstTarget = (v: string) => { setFirstTarget(v); setFirstTargetTouched(v !== '') }
  const editFirstSl = (v: string) => { setFirstSl(v); setFirstSlTouched(v !== '') }

  // tick-grid validation: a limit price off the tick grid cannot rest at the
  // exchange — suggest the nearest valid price instead of letting it reject
  const tickSz = inst.config.tick_size || 0.05
  const offTick = (v: string): number | null => {
    const n = Number(v)
    if (!v || !(n > 0) || !(tickSz > 0)) return null
    const r = Number((Math.round(n / tickSz) * tickSz).toFixed(4))
    return Math.abs(r - n) > 1e-9 ? r : null
  }
  const tickHint = (v: string, apply: (s: string) => void) => {
    const fix = offTick(v)
    if (fix == null) return null
    return (
      <div className="text-[10px] text-amber-700 mt-0.5">
        not on the {tickSz} tick grid — <button type="button" className="underline font-semibold"
          onClick={() => apply(String(fix))}>use {fmtPx(fix)}</button> for clean limit execution
      </div>
    )
  }
  // What a BLANK lots field actually resolves to at fire time: the configured
  // fixed count when sizing is FIXED, else live ATR sizing ("auto").
  const lotsAuto = config.sizing_mode === 'fixed'
    ? `${Math.max(Number(config.fixed_lots) || 1, 1)} (fixed)`
    : 'auto (ATR)'
  // GRID: each level's target is SELF-ANCHORED and frozen at setup — own
  // price + offset. It never chains to the rung above, and it does NOT move when
  // the buy price later shifts down on a cheaper fill, so a cheaper fill just
  // widens that rung's profit. Passing null anchors on the level's own price and
  // matches the backend preview (core/engine.py compute_ladder_preview).
  const autoTargetByIdx: Record<number, number> = {}
  for (const r of rows) {
    const p = rowPrice(r)
    if (p <= 0 || r.kind !== 'draft') continue
    autoTargetByIdx[r.i] =
      Math.round(computeTarget(previewTgtInst, p, null).target * 100) / 100
  }

  const basisBtn = (b: LadderBasis, label: string, hint: string) => (
    <button onClick={() => setBasis(b)}
      className={`flex-1 text-left px-3 py-2 rounded-lg border ${basis === b
        ? 'bg-violet-500/15 border-violet-600 text-violet-700'
        : 'border-gray-300 text-gray-600 hover:border-gray-400'}`}>
      <div className="text-sm font-semibold">{label}</div>
      <div className="text-[11px] opacity-80">{hint}</div>
    </button>
  )

  return (
    <div className="fixed inset-0 z-50 bg-gray-900/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white border border-gray-300 rounded-2xl w-full max-w-5xl max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between px-5 py-3 border-b border-gray-200 sticky top-0 bg-white z-10">
          <div>
            <div className="font-semibold text-gray-900 flex items-center gap-2">
              {inst.sym} — manual ladder
              <Badge tone="purple">LADDER</Badge>
              {lad?.armed
                ? <Badge tone="green">ARMED</Badge>
                : <Badge tone="gray">not confirmed</Badge>}
            </div>
            <div className="text-[11px] text-gray-500">
              {inst.tsym} · {inst.exch} · lot {inst.lot_size} · LTP {fmtPx(ltp || inst.price.lp)}
            </div>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-800 text-xl leading-none">×</button>
        </header>

        <div className="p-5 space-y-5">
          {/* ---- level basis ---- */}
          <div>
            <div className="text-xs font-semibold text-violet-600 border-b border-gray-200 pb-1 mb-3">
              BUY LEVELS — where the ladder buys
            </div>
            <div className="flex gap-2">
              {basisBtn('support', 'Strong support (4h pivots)',
                'levels from swing supports on 4-hour Shoonya history')}
              {basisBtn('fixed', 'Fixed intervals',
                'buy a lot after every fixed-point drop from an anchor')}
            </div>
            <div className="grid grid-cols-4 gap-3 mt-3">
              {basis === 'fixed' ? (
                <>
                  <div>
                    <L>Anchor price (buy 1)</L>
                    <input type="number" step="any" value={anchor}
                      onChange={(e) => { setAnchorTouched(true); setAnchor(e.target.value) }}
                      placeholder={ltp > 0 ? `LTP ${ltp}` : ''} className={inputCls + ' mt-1'} />
                  </div>
                  <div>
                    <L>Interval (points per drop)</L>
                    <input type="number" step="any" value={interval} onChange={(e) => setIntervalPts(e.target.value)}
                      className={inputCls + ' mt-1'} />
                  </div>
                </>
              ) : (
                <>
                  <div>
                    <L>Reference price (blank = LTP)</L>
                    <input type="number" step="any" value={anchor}
                      onChange={(e) => { setAnchorTouched(true); setAnchor(e.target.value) }}
                      className={inputCls + ' mt-1'} />
                  </div>
                  <div>
                    <L>History lookback (days, 4h bars)</L>
                    <input type="number" value={lookback} onChange={(e) => setLookback(e.target.value)}
                      className={inputCls + ' mt-1'} />
                  </div>
                </>
              )}
              <div>
                <L>Number of levels</L>
                <input type="number" value={numLevels} onChange={(e) => setNumLevels(e.target.value)}
                  className={inputCls + ' mt-1'} />
              </div>
              <div className="self-end">
                <button onClick={generate} disabled={busy !== ''}
                  className="w-full px-3 py-1.5 rounded-lg border border-violet-600 text-violet-700 hover:bg-violet-500/10 text-sm font-semibold disabled:opacity-50">
                  {busy === 'generate' ? 'Computing…' : '⟳ Generate levels'}
                </button>
              </div>
            </div>
          </div>

          {/* ---- target & stop-loss (configured here, not in Config) ---- */}
          <div>
            <div className="text-xs font-semibold text-violet-600 border-b border-gray-200 pb-1 mb-3">
              TARGET &amp; STOP-LOSS — per rung, configured from the levels
            </div>
            <div className="grid grid-cols-4 gap-3">
              <div>
                <L><span className="whitespace-nowrap">Target — first buy exits at</span></L>
                <input type="number" step="any" value={firstTarget}
                  placeholder={b1Price > 0 ? `e.g. ${fmtPx(Math.round((b1Price + (intervalPts || 4)) * 100) / 100)}` : 'e.g. B1 + interval'}
                  onChange={(e) => editFirstTarget(e.target.value)} className={inputCls + ' mt-1'} />
                {tickHint(firstTarget, editFirstTarget)}
              </div>
              <div>
                <L><span className="whitespace-nowrap">Target offset — % of interval</span></L>
                <div className="mt-1 flex items-center gap-2">
                  <input type="number" step="any" min="0" value={chainPctStr}
                    onChange={(e) => setChainPctStr(e.target.value)} className={inputCls} />
                  <span className="text-sm text-gray-500">%</span>
                </div>
                <div className="text-[10px] text-gray-500 mt-0.5">
                  extra above one interval: target = buy + interval × (1 + {chainPct || 0}%)
                  {intervalPts > 0 ? ` = buy + ${Math.round(intervalPts * (1 + offsetFrac) * 100) / 100}` : ''}. 0 = one interval up
                </div>
              </div>
              {slEnabled && (
                <div>
                  <L><span className="whitespace-nowrap">Stop-loss — first buy stops at</span></L>
                  <input type="number" step="any" value={firstSl}
                    placeholder={b1Price > 0 ? `e.g. ${fmtPx(Math.round((b1Price - (intervalPts || 4)) * 100) / 100)}` : 'e.g. B1 − interval'}
                    onChange={(e) => editFirstSl(e.target.value)} className={inputCls + ' mt-1'} />
                  {tickHint(firstSl, editFirstSl)}
                  <div className="text-[10px] text-gray-500 mt-0.5">
                    auto = buy1 − interval ({autoFirstSl != null ? fmtPx(autoFirstSl) : '—'}); each rung stops that
                    far below its own buy. ↑/↓ nudge ±1; clear to reset to auto
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* ---- editable levels ---- */}
          <div>
            <div className="flex items-center justify-between border-b border-gray-200 pb-1 mb-2">
              <span className="text-xs font-semibold text-violet-600">
                LEVELS — executed stay flagged in place · pending buys are editable
              </span>
              <button onClick={() => setLevels((ls) => [...ls, { price: '', lots: '', target: '', sl: '', note: 'user-added', fireCount: 0 }])}
                className="px-2 py-0.5 rounded border border-gray-300 text-gray-700 hover:border-violet-600 hover:text-violet-700 text-[11px]">
                + Add level
              </button>
            </div>
            {rows.length === 0 && (
              <div className="text-gray-500 text-sm py-4 text-center">
                No levels — press “⟳ Generate levels”, or add levels manually.
              </div>
            )}
            {rows.length > 0 && (
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="text-gray-500 text-[11px] uppercase tracking-wide">
                    <th className="text-left px-2 py-1">#</th>
                    <th className="text-left px-2">Buy price</th>
                    <th className="text-left px-2">Lots</th>
                    <th className="text-left px-2">Target (blank = auto)</th>
                    {slEnabled && <th className="text-left px-2">Stoploss (blank = auto)</th>}
                    <th className="text-left px-2">Note</th>
                    <th className="text-right px-2" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, idx) => {
                    if (r.kind === 'exec') {
                      // Non-draft level — flagged in place, read-only.
                      // FILLED/TRIGGERED: its rung is OPEN (recycles to a pending buy
                      // when the rung hits target). PLACED: resting buy at the broker.
                      // SKIPPED: user-cancelled, re-establishable. AWAIT_RECOVERY:
                      // re-places once price recovers above the level.
                      const t = r.lv
                      const filled = t.status === 'FILLED' || t.status === 'TRIGGERED'
                      const skipped = t.status === 'SKIPPED'
                      const awaitRec = t.status === 'AWAIT_RECOVERY'
                      const placedRow = t.status === 'PLACED'
                      const cancelled = t.status === 'CANCELLED'
                      return (
                        <tr key={`x${t.id}`} className={`border-t border-gray-200 ${skipped
                          ? 'bg-amber-100/60 text-gray-500'
                          : (awaitRec || placedRow) ? 'text-gray-700' : 'bg-gray-100 text-gray-400'}`}>
                          <td className="px-2 py-1.5 font-semibold">
                            B{idx + 1}
                            {filled && (
                              <span
                                title={lad?.rearm === false
                                  ? 'filled — an open rung. Recycle is OFF, so this level fires only once.'
                                  : 'filled — an open rung. Becomes an active buy again once this rung hits its TARGET; retires if it hits its stop-loss.'}
                                className="ml-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-semibold bg-emerald-500/15 text-emerald-600 border border-emerald-500 align-middle">
                                ✓ FILLED{t.fire_count > 1 ? ` ↻${t.fire_count}×` : ''}
                              </span>
                            )}
                            {t.status === 'PLACED' && (
                              <span className="ml-1 align-middle"
                                title="resting limit buy live at the broker — fills only on a real trade at this price">
                                <Badge tone="amber">⏳ order at broker</Badge>
                              </span>
                            )}
                            {skipped && (
                              <span
                                title="level skipped — its resting order was cancelled. Re-establish to make it an active buy again."
                                className="ml-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-semibold bg-amber-200/60 text-amber-700 border border-amber-400 align-middle">
                                SKIPPED
                              </span>
                            )}
                            {awaitRec && (
                              <span
                                title="re-established while price is below this level — it becomes the active buy automatically once price trades back above it"
                                className="ml-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-semibold border border-amber-500 text-amber-600 align-middle">
                                active after price &gt; ₹{fmtPx(t.price)}
                              </span>
                            )}
                          </td>
                          <td className="px-2 font-mono tabular-nums">{fmtPx(t.price)}</td>
                          <td className="px-2 text-[11px]">{t.lots_override ?? lotsAuto}</td>
                          <td className="px-2 text-[11px]">
                            <LevelTarget
                              lotId={t.lot_id}
                              target={t.target_live ?? t.target_override}
                              hint={filled ? 'live rung in Trades'
                                : t.status === 'PLACED' ? 'order in Pending tab' : ''}
                              onError={setErr} />
                          </td>
                          {slEnabled && (
                            <td className="px-2 text-[11px] font-mono tabular-nums">
                              {t.sl_override ? fmtPx(t.sl_override) : t.sl_auto ? fmtPx(t.sl_auto) : '—'}
                            </td>
                          )}
                          <td className="px-2 text-[11px]">
                            {fmtTime(t.triggered_at)}{t.note ? ` · ${t.note}` : ''}
                          </td>
                          <td className="px-2 text-right text-[11px] whitespace-nowrap">
                            {skipped && <ReestablishBtn levelId={t.id} onError={setErr} />}
                            {filled && lad?.rearm !== false && (
                              <span className="mr-1" title="recycles to a pending buy when its rung hits target">⟳</span>
                            )}
                            {(filled || cancelled) && (
                              <button
                                disabled={busy !== ''}
                                onClick={() => removeExecLevel(t)}
                                title={filled
                                  ? 'Remove from the ladder — keeps the OPEN position (manage it in Trades)'
                                  : 'Remove this retired level from the ladder'}
                                className="px-1.5 py-0.5 rounded border border-gray-300 text-gray-500 hover:border-rose-500 hover:text-rose-600 disabled:opacity-40">
                                {busy === `rm${t.id}` ? '…' : '✕'}
                              </button>
                            )}
                          </td>
                        </tr>
                      )
                    }
                    const { i, l } = r
                    const below = Number(l.price) > 0 && ltp > 0 && Number(l.price) >= ltp
                    return (
                      <tr key={`d${i}`} className="border-t border-gray-200">
                        <td className="px-2 py-1.5 font-semibold text-gray-900">
                          B{idx + 1}
                          {l.fireCount > 0 && (
                            <span title={`this level has executed ${l.fireCount}× and re-armed — it will buy again on the next dip`}
                              className="ml-1 inline-block rounded px-1 py-0.5 text-[9px] font-semibold bg-violet-500/15 text-violet-700 border border-violet-300 align-middle">
                              ↻ fired {l.fireCount}×
                            </span>
                          )}
                        </td>
                        <td className="px-2">
                          <input type="number" step="any" value={l.price}
                            onChange={(e) => setLevel(i, { price: e.target.value })}
                            onFocus={() => setEditingRow(i)}
                            onBlur={() => setEditingRow((cur) => (cur === i ? null : cur))}
                            className={inputCls + (below ? ' border-amber-600' : '')} />
                          {tickHint(l.price, (v) => setLevel(i, { price: v }))}
                          {below && (
                            <div className="mt-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold
                              bg-amber-500/25 text-amber-800 border border-amber-600">
                              ≥ LTP — fires at MARKET immediately once armed
                            </div>
                          )}
                        </td>
                        <td className="px-2">
                          <input type="number" min="1" value={l.lots} placeholder={lotsAuto}
                            onChange={(e) => setLevel(i, { lots: e.target.value })}
                            className={inputCls} />
                          <div className="text-[10px] text-gray-500">
                            {l.lots !== '' ? 'override for this level'
                              : config.sizing_mode === 'fixed'
                                ? <>blank = {Math.max(Number(config.fixed_lots) || 1, 1)} lot(s) from Config</>
                                : 'blank = ATR sizing at fire time'}
                          </div>
                        </td>
                        <td className="px-2">
                          <input type="number" step="any"
                            value={l.target !== '' ? l.target : (autoTargetByIdx[i] != null ? String(autoTargetByIdx[i]) : '')}
                            placeholder="auto"
                            onChange={(e) => setLevel(i, { target: e.target.value })}
                            className={inputCls + (l.target !== '' ? ' border-emerald-600' : '')} />
                          <div className="text-[10px] text-gray-500">
                            {l.target !== ''
                              ? 'edited target'
                              : autoTargetByIdx[i]
                                ? <>auto {fmtPx(autoTargetByIdx[i])}</>
                                : 'set the first-buy target above'}
                          </div>
                          {tickHint(l.target, (v) => setLevel(i, { target: v }))}
                        </td>
                        {slEnabled && (
                          <td className="px-2">
                            <input type="number" step="any"
                              value={l.sl !== '' ? l.sl : (slAuto(Number(l.price) || 0) != null ? String(slAuto(Number(l.price) || 0)) : '')}
                              placeholder="auto"
                              onChange={(e) => setLevel(i, { sl: e.target.value })}
                              className={inputCls + (l.sl !== '' ? ' border-rose-500' : '')} />
                            <div className="text-[10px] text-gray-500">
                              {l.sl !== ''
                                ? 'edited stop'
                                : slAuto(Number(l.price) || 0) != null
                                  ? <>auto {fmtPx(slAuto(Number(l.price) || 0)!)}</>
                                  : 'set the first-buy SL above'}
                            </div>
                            {tickHint(l.sl, (v) => setLevel(i, { sl: v }))}
                          </td>
                        )}
                        <td className="px-2 text-[11px] text-gray-500">{l.note || '—'}</td>
                        <td className="px-2 text-right">
                          <button onClick={() => setLevels((ls) => ls.filter((_, j) => j !== i))}
                            className="px-2 py-0.5 rounded border border-gray-300 text-gray-600 hover:border-red-500 hover:text-red-600 text-[11px]">
                            ✕
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>

        </div>

        <footer className="flex items-center justify-between px-5 py-3 border-t border-gray-200 sticky bottom-0 bg-white">
          <span className="text-xs">
            {err && <span className="text-red-600">{err}</span>}
            {!err && firedNotice.length > 0 && (
              <span className="text-amber-700">
                {firedNotice.join(', ')} fired while this dialog was open — dropped from the draft so
                Confirm cannot buy {firedNotice.length > 1 ? 'them' : 'it'} again.
              </span>
            )}
          </span>
          <div className="flex gap-2 items-center">
            {clearableCount > 0 && (
              <button
                disabled={busy !== ''}
                title="Remove every pending level and cancel resting buy orders at the broker. Open rungs and history are kept."
                onClick={async () => {
                  if (!window.confirm(
                    `Clear all ${clearableCount} pending level(s) for ${inst.sym}?\n\n`
                    + 'Resting buy orders at the broker are cancelled. Open rungs (filled) and '
                    + 'history are kept. This cannot be undone.')) return
                  setBusy('clear')
                  setErr('')
                  try { await api.clearLadderLevels(inst.id); onSaved(); onClose() }
                  catch (e) { setErr(e instanceof Error ? e.message : 'clear failed') }
                  finally { setBusy('') }
                }}
                className="px-3 py-1.5 rounded-lg border border-rose-600 text-rose-600 hover:bg-rose-500/10 text-sm">
                {busy === 'clear' ? 'Clearing…' : 'Clear all levels'}
              </button>
            )}
            {lad?.armed && (
              <button
                disabled={busy !== ''}
                onClick={async () => {
                  setBusy('pause')
                  try { await api.ladderArm(inst.id, false); onSaved(); onClose() }
                  catch (e) { setErr(e instanceof Error ? e.message : 'pause failed') }
                  finally { setBusy('') }
                }}
                className="px-3 py-1.5 rounded-lg border border-amber-600 text-amber-600 hover:bg-amber-500/10 text-sm">
                ⏸ Pause ladder
              </button>
            )}
            <button onClick={onClose} className="px-3 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm">Cancel</button>
            <button onClick={confirm} disabled={busy !== ''}
              className="px-4 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-sm font-semibold disabled:opacity-50">
              {busy === 'confirm' ? 'Saving…' : '✓ Confirm & arm ladder'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

/** Re-activate a SKIPPED level: places the resting order now if price is above
 * the level, else parks it as AWAIT_RECOVERY. The next snapshot refresh
 * re-renders the row with its new status. */
/** A rung's exit price, editable in place — for rungs already filled AND for
 * ones whose buy is still resting at the broker.
 *
 * Both cases go through the same lot endpoint, which does the right thing for
 * each: on an open rung it moves the resting sell already sitting at the broker,
 * and on a rung still waiting to fill it stores an override, so the edit
 * survives the day-expiry cancel/re-place cycle instead of being recomputed away
 * when the buy finally fills. A level with no lot behind it has nothing to edit
 * yet, so it just reports what it will compute. */
function LevelTarget({ lotId, target, hint, onError }: {
  lotId: number | null
  target: number | null
  hint: string
  onError: (m: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    const px = Number(draft)
    if (!(px > 0)) { onError('Target must be a price above 0.'); return }
    setBusy(true)
    try {
      await api.editLot(lotId!, { target_price: px })
      setEditing(false)
      onError('')
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Could not save the target.')
    } finally { setBusy(false) }
  }

  const note = hint ? <div className="text-[10px]">{hint}</div> : null

  if (lotId == null) {
    return <>
      <span className="font-mono tabular-nums">{target ? fmtPx(target) : 'auto'}</span>
      {note}
    </>
  }
  if (editing) {
    return (
      <div className="flex items-center gap-1">
        <input autoFocus type="number" step="any" value={draft} disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') save()
            if (e.key === 'Escape') setEditing(false)
          }}
          className="w-24 px-1 py-0.5 rounded border border-gray-300 font-mono tabular-nums text-[11px]" />
        <button type="button" onClick={save} disabled={busy}
          title="Save target" className="text-emerald-600 disabled:opacity-40">✓</button>
        <button type="button" onClick={() => setEditing(false)} disabled={busy}
          title="Cancel" className="text-gray-400 disabled:opacity-40">✕</button>
      </div>
    )
  }
  return <>
    <button type="button"
      onClick={() => { setDraft(target ? String(target) : ''); setEditing(true) }}
      title="Change this rung's target. An open rung moves its resting sell at the broker."
      className="font-mono tabular-nums underline decoration-dotted underline-offset-2 hover:text-sky-600">
      {target ? fmtPx(target) : 'auto'}
    </button>
    {note}
  </>
}

function ReestablishBtn({ levelId, onError }: { levelId: number; onError: (m: string) => void }) {
  const [state, setState] = useState<'idle' | 'busy' | 'done'>('idle')

  const fire = async () => {
    setState('busy')
    try {
      await api.reestablishLevel(levelId)
      setState('done')
      setTimeout(() => setState('idle'), 2500)
    } catch (e) {
      onError(e instanceof Error ? e.message : 're-establish failed')
      setState('idle')
    }
  }

  if (state === 'done') {
    return <span className="text-[11px] text-emerald-600 whitespace-nowrap">re-established ✓</span>
  }
  return (
    <button disabled={state === 'busy'} onClick={fire}
      className="px-2 py-0.5 rounded border border-amber-500 text-amber-600 hover:bg-amber-500/10 text-[11px] disabled:opacity-40 whitespace-nowrap">
      {state === 'busy' ? '…' : 'Re-establish'}
    </button>
  )
}
