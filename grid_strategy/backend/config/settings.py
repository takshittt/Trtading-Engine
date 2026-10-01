"""Engine-level settings (env-overridable). Per-instrument config lives in the DB."""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env before any setting (incl. auth secrets) is read. Imported early by
# api.routes / core.security, so this runs before those read the environment.
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Settings:
    # Gateway connection
    gateway_base_url: str = field(default_factory=lambda: os.getenv("GATEWAY_BASE_URL", "http://localhost:8000"))
    gateway_ws_url: str = field(default_factory=lambda: os.getenv("GATEWAY_WS_URL", "ws://localhost:8000/api/ws/ticker"))

    # Service identity for authenticating TO the Gateway (client-credentials grant).
    # Issued by the Gateway's scripts/register_service.py.
    gateway_client_id: str = field(default_factory=lambda: os.getenv("GATEWAY_CLIENT_ID", ""))
    gateway_client_secret: str = field(default_factory=lambda: os.getenv("GATEWAY_CLIENT_SECRET", ""))

    # Shared HS256 secret (IDENTICAL to the Gateway's JWT_SECRET) — used only to
    # locally verify the *human user* JWT presented to grid's own dashboard.
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", ""))


    # Database
    database_url: str = field(default_factory=lambda: os.getenv("GRID_DB_URL") or os.getenv("SNOWBALL_DB_URL", "sqlite:///./grid.db"))

    # Safety-net cadences (seconds)
    reconcile_interval: float = 60.0     # broker-vs-internal position sync
    # WS *data* silence → FEED_STALE (pause new buys). Only trade/order frames
    # reset it, NOT the socket's keep-alive ping/pong — so a quiet market or an
    # illiquid/after-hours instrument that simply isn't printing trades would
    # otherwise trip a false STALE. A genuinely dead socket is caught separately
    # by the websockets ping_timeout (~20s) → feed "down", so this can be
    # generous without hiding a real outage.
    heartbeat_stale_after: float = 30.0
    quote_poll_after: float = 3.0        # per-token tick silence → REST quote fallback
    nobar_dedup_window: float = 10.0     # suppress identical webhook re-fires w/o bar_time within this window

    # Tick data-validation (outlier/spike filter — protects exits & CB from bad prints).
    # PRIMARY gate is the inside-spread rule: a trade can't print far from the live
    # bid/ask, so the book itself is a self-scaling guardrail (works identically on a
    # ₹10 stock, ₹250 NG or ₹25,000 Nifty — no percentage to tune per instrument).
    tick_spread_buffer_frac: float = field(default_factory=lambda: float(os.getenv("TICK_SPREAD_BUFFER_FRAC", "0.003")))
    # ^ LTP may sit at most this fraction BEYOND the bid/ask before it's a glitch
    #   (0.003 = 0.3%; covers normal book lag in a fast move, still rejects real
    #   glitches which are far larger). Tighten toward 0.001 for stricter filtering.
    tick_book_max_spread_frac: float = 0.05   # book wider than this (of mid) is unreliable → use the fallback rule
    tick_ghost_move_frac: float = 0.01        # a >1% move printed on ZERO traded qty is a ghost tick
    # Fallback (only when there is NO usable two-sided book): the old percentage rule.
    tick_max_move_frac: float = 0.20     # a >20% jump vs last accepted price is treated as a spike
    tick_move_window_s: float = 20.0     # (legacy, unused — spikes are now suspect at ANY gap; kept for env compat)
    tick_reaccept_after_s: float = 60.0  # persistent rejections re-baseline after this long (real gap, not glitch)

    # Feed-death fallback + manual-exit alerting
    feed_down_after: float = 45.0        # Shoonya silent this long (secs) with open lots → degraded mode + alert
    alert_repeat_after: float = 900.0    # re-send the WhatsApp exit alert at most this often while degraded (15m)
    fallback_provider: str = field(default_factory=lambda: os.getenv("FALLBACK_PROVIDER", "yfinance"))  # yfinance | http | off
    fallback_http_url: str = field(default_factory=lambda: os.getenv("FALLBACK_HTTP_URL", ""))  # your-own-API template: {sym}
    finnhub_api_key: str = field(default_factory=lambda: os.getenv("FINNHUB_API_KEY", ""))

    # Pre-trade margin gate (Shoonya SPAN)
    margin_gate_enabled: bool = field(default_factory=lambda: os.getenv("MARGIN_GATE", "1") not in ("0", "false", "False", ""))
    margin_safety_factor: float = 1.10   # require this multiple of SPAN+exposure free before a buy
    ui_push_interval: float = 1.0        # snapshot push to UI (price refresh every second)
    rollover_check_interval: float = 30.0

    # Broker-status polling (the /api/status trading gate + UI badge). A DEFINITIVE
    # answer from the (WS-primary) gateway is trusted at once; only a gateway that is
    # UNREACHABLE for this many consecutive polls flips the badge to down — so a
    # single Grid↔Gateway network blip no longer flaps "disconnected".
    broker_status_poll_interval: float = 15.0
    broker_status_grace_polls: int = 3
    log_retention_days: int = 31

    # Order verification
    order_verify_timeout: float = 45.0   # give up polling and mark UNCONFIRMED after this
    order_poll_schedule: tuple = (0.4, 0.8, 1.2, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 10.0)

    # Order routing defaults
    product_type: str = "M"              # NRML — grid is positional, carries overnight
    price_type: str = "MKT"              # user requirement: real-money market orders

    # Global circuit breaker (₹ loss across all instruments; 0 = disabled)
    global_cb_threshold: float = field(default_factory=lambda: float(os.getenv("GLOBAL_CB_THRESHOLD", "0") or 0))

    # ABSOLUTE lots-per-order ceiling — a last-resort backstop that applies even
    # when a per-instrument max_lots_per_rung is 0 ("off") or mis-set. Sizing can
    # never emit more lots than this regardless of config, so one bad field
    # (huge risk_per_rung, tiny price/ATR) can't fire a five-figure-lot order.
    max_lots_hard_cap: int = field(default_factory=lambda: int(os.getenv("MAX_LOTS_HARD_CAP", "50") or 50))


settings = Settings()
