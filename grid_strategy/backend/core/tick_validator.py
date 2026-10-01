"""Tick sanity / outlier filter — the data-validation layer.

A single bad print from the feed (a 0, a NaN, or a fat-finger spike) must never
reach the exit checks or the circuit breaker, or it can market-sell a rung or
trip the CB on a price that never really traded. Every incoming price (WS tick
AND REST-poll quote) passes through `TickValidator.check()` first.

The gate, strongest first (all per-token):

  1. lp must be a finite number > 0.

  2. Crossed / degenerate book: if bid > ask (a glitch signature) the two-sided
     book is distrusted for this tick — both quotes are dropped and we fall to
     rule 5. A book wider than `tick_book_max_spread_frac` (of mid) is likewise
     treated as unreliable.

  3. Zero-quantity ghost: a *real* trade moves size. A large jump printed on a
     traded quantity of 0 is a guaranteed ghost/freak print → REJECT. (Only used
     when the feed actually carries last-trade-qty; absent → skip.)

  4. PRIMARY — the inside-spread rule (what HFT desks use): a genuine trade
     cannot happen far from the best bid/ask. So the LTP must sit inside
     [bid − buffer, ask + buffer]; anything beyond that boundary is a glitch,
     no matter the percentage. Because the *market's own* bid/ask is the
     guardrail, this self-scales across every price level with no per-instrument
     tuning — and it still ACCEPTS real fast moves, because in a real move the
     bid/ask travel with the price so the LTP stays inside the (moving) book.

  5. FALLBACK (only when there is no usable two-sided book — one-sided, crossed,
     or absent quotes): the classic percentage-move rule vs the last accepted
     price (`tick_max_move_frac`).

Rules 4 and 5 share a never-stuck valve: if rejections persist for
`tick_reaccept_after_s` (timed from the FIRST rejection since the last accept),
the next print is accepted and re-baselines — so a genuine gap where the book
froze can never wedge the feed permanently.

`check()` returns (ok, reason, clean_bid, clean_ask). On ok=False the caller
must NOT update the cached price (its age keeps growing so the poll/fallback
path takes over) and should log the reason (throttled).
"""

import math
import time as _time
from dataclasses import dataclass

from config.settings import settings

_CROSS_TOL = 0.001   # tolerate a hair of bid>ask (rounding noise) before calling the book crossed


@dataclass
class _Ref:
    lp: float = 0.0
    mono_ts: float = 0.0
    pending_level: float = 0.0     # last rejected level (diagnostics)
    pending_since: float = 0.0     # first rejection since the last accepted print


class TickValidator:
    def __init__(self):
        self._ref: dict[str, _Ref] = {}

    @staticmethod
    def _finite_pos(x: float) -> bool:
        return isinstance(x, (int, float)) and math.isfinite(x) and x > 0

    def _accept(self, ref: _Ref, lp: float, now: float, cb: float, ca: float):
        ref.lp = lp
        ref.mono_ts = now
        ref.pending_level = 0.0
        ref.pending_since = 0.0
        return True, "", cb, ca

    def _persisted(self, ref: _Ref, now: float) -> bool:
        """Never-stuck valve: rejections have persisted long enough to re-baseline."""
        return bool(ref.pending_since) and (now - ref.pending_since) >= settings.tick_reaccept_after_s

    def _reject(self, ref: _Ref, lp: float, now: float, reason: str, cb: float, ca: float):
        if not ref.pending_since:
            ref.pending_since = now
        ref.pending_level = lp
        return False, reason, cb, ca

    def check(self, key: str, lp: float, bid: float, ask: float,
              ltq: float | None = None) -> tuple[bool, str, float, float]:
        now = _time.monotonic()

        # ---- clean the book: finite, positive, not crossed ----
        clean_bid = bid if self._finite_pos(bid) else 0.0
        clean_ask = ask if self._finite_pos(ask) else 0.0
        if clean_bid and clean_ask and clean_bid > clean_ask * (1 + _CROSS_TOL):
            clean_bid = clean_ask = 0.0     # crossed → distrust both (keep the cached book upstream)

        if not self._finite_pos(lp):
            return False, f"non-positive/NaN price ({lp!r})", clean_bid, clean_ask

        # a sane, reasonably-tight two-sided book can be used as the guardrail
        book_ok = clean_bid > 0 and clean_ask > 0
        if book_ok:
            mid = 0.5 * (clean_bid + clean_ask)
            if mid <= 0 or (clean_ask - clean_bid) / mid > settings.tick_book_max_spread_frac:
                book_ok = False             # abnormally wide/degenerate → don't trust for inside-spread

        ref = self._ref.get(key)
        if ref is None or ref.lp <= 0:
            self._ref[key] = _Ref(lp=lp, mono_ts=now)     # first good print seeds the reference
            return True, "", clean_bid, clean_ask

        # ---- zero-quantity ghost (runs before everything; catches whole-book glitches too) ----
        if ltq is not None and ltq <= 0 and abs(lp - ref.lp) / ref.lp > settings.tick_ghost_move_frac:
            return self._reject(ref, lp, now,
                                f"ghost tick: {lp:g} on ZERO trade-qty "
                                f"({abs(lp - ref.lp) / ref.lp * 100:.1f}% off {ref.lp:g})",
                                clean_bid, clean_ask)

        # ---- PRIMARY: inside-spread rule ----
        if book_ok:
            buf = max(settings.tick_spread_buffer_frac, 0.0)
            lo = clean_bid * (1.0 - buf)
            hi = clean_ask * (1.0 + buf)
            if lo <= lp <= hi:
                return self._accept(ref, lp, now, clean_bid, clean_ask)
            if self._persisted(ref, now):
                return self._accept(ref, lp, now, clean_bid, clean_ask)
            side = "below bid" if lp < lo else "above ask"
            return self._reject(ref, lp, now,
                                f"outside book: {lp:g} {side} [{clean_bid:g} / {clean_ask:g}] "
                                f"by >{buf * 100:.2g}% — glitch dropped",
                                clean_bid, clean_ask)

        # ---- FALLBACK: percentage move vs reference (no usable book) ----
        move = abs(lp - ref.lp) / ref.lp
        max_move = max(settings.tick_max_move_frac, 0.0)
        if max_move <= 0 or move <= max_move:
            return self._accept(ref, lp, now, clean_bid, clean_ask)
        if self._persisted(ref, now):
            return self._accept(ref, lp, now, clean_bid, clean_ask)
        return self._reject(ref, lp, now,
                            f"spike (no book): {lp:g} is {move * 100:.1f}% off last {ref.lp:g} "
                            f"(re-accepted if it persists {settings.tick_reaccept_after_s:g}s)",
                            clean_bid, clean_ask)

    def reset(self, key: str) -> None:
        self._ref.pop(key, None)
