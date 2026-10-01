/** The ladder's target/stop math — the dashboard's copy of `core/exits.py`.
 *
 * This math necessarily exists twice: the backend computes what is actually
 * traded, and the levels dialog has to preview it live, per keystroke, before
 * anything is saved. A round trip per keystroke is not an option, so the
 * formula is duplicated — and duplicated formulas drift. This one already did:
 * the dialog measured the first-buy offset from the highest level it displayed
 * while the backend measured it from the highest level posted, so a 200-point
 * target was stored as 600 and inherited by every rung below it. The preview
 * also never had the anchor clamp at all, so it could show a chained target the
 * engine would never actually use.
 *
 * Both sides are now pinned to the same vectors — see
 * `backend/tests/ladder_math_vectors.json`, asserted by `ladderMath.test.ts`
 * here and `backend/tests/test_exit_math.py` there. Change the formula in one
 * place and the other side's test fails, which is the entire point.
 *
 * Keep this file a direct translation of core/exits.py. No preview-only
 * cleverness belongs here.
 */

export interface TargetInst {
  target_mode: string          // "percent" | anything else = points
  target_value: number
  target_chain_pct?: number    // undefined/null = 100 (full offset chained)
}

export interface SlInst {
  sl_enabled: boolean
  sl_mode: string              // "percent" | anything else = points
  sl_value: number
}

/** offset_of() in core/exits.py — percent offsets are computed on the ANCHOR. */
export function offsetOf(mode: string, value: number, anchorPrice: number): number {
  return mode === 'percent' ? anchorPrice * (value / 100) : value
}

const round4 = (n: number) => Math.round(n * 10000) / 10000

/**
 * compute_target() in core/exits.py.
 *
 *   first rung (no rung above): target = own entry + FULL offset
 *   chained rung:               target = the rung above's entry + chain% × offset
 *
 * The clamp matters: a recycled level can refire ABOVE still-open lower rungs,
 * which would put the chained target at or below this rung's own entry and make
 * it round-trip instantly for nothing. Falls back to self-anchored.
 */
export function computeTarget(
  inst: TargetInst, entryPrice: number, prevOpenEntry: number | null,
): { target: number; anchor: number } {
  let anchor = prevOpenEntry ?? entryPrice
  let offset = offsetOf(inst.target_mode, inst.target_value, anchor)
  if (prevOpenEntry != null) {
    const pct = inst.target_chain_pct == null ? 100 : Number(inst.target_chain_pct)
    offset = offset * (pct / 100)
  }
  let target = anchor + offset
  if (prevOpenEntry != null && target <= entryPrice) {
    anchor = entryPrice
    target = entryPrice + offsetOf(inst.target_mode, inst.target_value, entryPrice)
  }
  return { target: round4(target), anchor }
}

/**
 * compute_sl() in core/exits.py.
 *
 * Zero is not "no value" here — it is the engine's marker for "this rung has NO
 * stop", which the tick loop reads literally. So a disabled stop returns exactly
 * that, and a stop wider than the price is allowed to compute negative rather
 * than being quietly clamped: the engine refuses such a rung at entry, and
 * hiding it in the preview would hide the refusal too.
 */
export function computeSl(inst: SlInst, entryPrice: number): number {
  if (inst.sl_enabled === false) return 0
  return round4(entryPrice - offsetOf(inst.sl_mode, inst.sl_value, entryPrice))
}
