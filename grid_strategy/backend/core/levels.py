"""Manual-ladder buy-level generation.

Two bases, both producing a descending list of buy prices (B1 highest):

  "fixed"   — arithmetic ladder from an anchor: B1 = anchor,
              B(k+1) = B(k) − interval. E.g. anchor 2000, interval 500
              → 2000, 1500, 1000, … (a lot bought after every 500-point drop).

  "support" — strong supports from 4-hour swing pivots. A swing low is a bar
              whose low is the minimum of its neighbourhood (`wing` bars each
              side). Nearby pivots are clustered (tolerance = CLUSTER_PCT of
              price); a cluster touched multiple times is a *strong* support.
              Candidates below the reference price are returned nearest-first.

Both bases produce a FINITE ladder: the `n` levels computed here are all the
levels there will ever be. When a level triggers the engine does NOT append a
new one below the bottom — after the last level fires there is no further buy
until the user adds/increases levels and re-confirms (see GridEngine, the
"the ladder is FINITE" note). `ladder_num_levels` is the count generated at
preview/confirm time, not a live pending-depth the engine maintains.
"""

from dataclasses import dataclass

CLUSTER_PCT = 0.0035   # pivots within 0.35% of each other = same support zone
DEFAULT_WING = 3       # bars on each side that must be higher for a swing low


@dataclass
class SupportZone:
    price: float       # zone price (lowest pivot low in the cluster)
    touches: int       # how many pivots landed in the zone (strength)
    last_ts: int       # epoch of the most recent touch


def fixed_levels(anchor: float, interval: float, n: int) -> list[float]:
    """n descending levels: anchor, anchor−interval, …"""
    if anchor <= 0 or interval <= 0 or n <= 0:
        return []
    out = []
    p = anchor
    for _ in range(n):
        if p <= 0:
            break
        out.append(round(p, 4))
        p -= interval
    return out


def swing_lows(candles: list[dict], wing: int = DEFAULT_WING) -> list[tuple[float, int]]:
    """(low, ts) of every bar whose low is the minimum of the ±wing window."""
    lows = [c["low"] for c in candles]
    out: list[tuple[float, int]] = []
    for i in range(wing, len(lows) - wing):
        window = lows[i - wing:i + wing + 1]
        if lows[i] == min(window):
            out.append((lows[i], candles[i].get("ts", 0)))
    return out


def support_zones(candles: list[dict], wing: int = DEFAULT_WING) -> list[SupportZone]:
    """Cluster swing lows into support zones, strongest data first computed,
    returned sorted by price descending."""
    pivots = sorted(swing_lows(candles, wing))          # by price asc
    zones: list[SupportZone] = []
    for price, ts in pivots:
        if zones and price <= zones[-1].price * (1 + CLUSTER_PCT):
            z = zones[-1]
            z.touches += 1
            z.last_ts = max(z.last_ts, ts)
        else:
            zones.append(SupportZone(price=round(price, 4), touches=1, last_ts=ts))
    zones.sort(key=lambda z: z.price, reverse=True)
    return zones


def supports_below(candles: list[dict], reference_price: float, n: int,
                   wing: int = DEFAULT_WING) -> list[SupportZone]:
    """The n support zones nearest below reference_price (strong zones kept
    even when a weak one is nearer — strength breaks ties within 1 interval)."""
    below = [z for z in support_zones(candles, wing) if z.price < reference_price]
    return below[:n]
