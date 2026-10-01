# Reversal Strategy — Live Integration (Shoonya + Amibroker)

This guide takes the system from the simulated demo to live trading. Nothing in
the strategy core changes — you only flip env vars and point two network hops.

```
Amibroker (Windows)  --MySQL ami_signal_inbox-->  Reversal Strategy backend  --REST/WS-->  Shoonya Gateway  --WireGuard-->  Shoonya (Finvasia)
   swing_scan*.afl                                   (this project)         (isolated, holds creds)     static IP
   scan_bridge_sql.py
```

The strategy backend never holds broker credentials or talks to Shoonya
directly — it speaks REST + WebSocket to an **isolated Shoonya gateway process**
(same pattern as the Grid system). A crash or auth failure in the gateway
can't reach the strategy.

---

## 1. WireGuard → Shoonya static IP

Shoonya's API is registered against a private static IP, reached over WireGuard.
Bring the tunnel up first (on the host running the Shoonya gateway):

```bash
# start the WireGuard tunnel, then confirm the gateway host is reachable
ssh root@192.168.133.205        # the box where the Shoonya IP is registered
```

The Shoonya gateway runs on that box (or one behind the tunnel) on port 8000.

## 2. Point the strategy at the gateway

In `backend/.env`:

```ini
SWING_BROKER_MODE=shoonya
SWING_GATEWAY_BASE_URL=http://192.168.133.205:8000
SWING_GATEWAY_WS_URL=ws://192.168.133.205:8000/api/ws/ticker
SWING_GATEWAY_SECRET=            # if the gateway requires one
```

The gateway is expected to expose (Grid/gateway contract — adjust the field
maps in `app/brokers/shoonya_gateway.py` if yours differs):

| Method | Endpoint | Used for |
|---|---|---|
| GET | `/api/status` | connection health |
| GET | `/api/funds` | Total Money in Shoonya (top bar) |
| GET | `/api/positions` | 1-minute reconciliation truth |
| POST | `/api/orders` | LIMIT order placement |
| POST | `/api/order_margin` | SPAN margin per lot (budget) |
| GET | `/api/quote` | LTP fallback |
| GET | `/api/search` | Search & Add |
| WS | `/api/ws/ticker` | live LTP stream |

### Shoonya gateway credentials (kept in the GATEWAY, not here)

The gateway's own `.env` holds the broker secrets (from the Grid gateway):

```
SHONYA_USER_ID, SHONYA_PASSWORD, SHONYA_TWO_FA (TOTP), SHONYA_VENDOR_CODE,
SHONYA_API_KEY, SHONYA_API_SECRET, SHONYA_IMEI, SHOONYA_API_HOST, FERNET_KEY
```

> Gateway resilience notes worth keeping (from the Grid gateway): Shoonya
> rejects TLS 1.3 — pin TLS 1.2; bypass `NorenApi.place_order` and POST
> `PlaceOrder` yourself to preserve the reject `emsg`; probe `get_limits` for an
> honest "connected" status rather than trusting the token date.

## 3. Margin (NSE SPAN)

Budget utilisation is measured in **margin**, not notional (one futures lot locks
only SPAN + Exposure, ~10–20% of contract value). The authoritative daily figure
comes from Shoonya's **SPAN calculator** via the gateway `/api/order_margin`
(`span + expo`) — this *is* the exchange SPAN margin, delivered through the broker
rather than as raw `.spn` files. It refreshes on startup and can be re-pulled from
the UP (`POST /api/margin/refresh`).

To parse raw NSE SPAN files instead, set `SWING_ENABLE_NSE_SPAN=true` and
implement `_from_nse_span_file()` in `app/services/margin.py`.

## 4. Amibroker → MySQL inbox

Amibroker sends **closed-candle** signals with ATR + support/resistance (which
power the AUTO Target/SL methods). Two moving parts run on the Amibroker box:

1. **`integration/amibroker/swing_scan*.afl`** — Explorations that append to
   `C:\swing\scan_60.csv` / `scan_240.csv` / `scan_1440.csv`
   (`symbol,timeframe_min,action,price,atr,resistance,bar_time,support`).

2. **`integration/amibroker/scan_bridge_sql.py`**, started by
   `run_bridge_sql.bat` — reads the rows appended since its bookmark and
   INSERTs them into `ami_signal_inbox` on the shared MySQL database. The
   Amibroker box never talks to the backend directly.

In `backend/.env`, have the backend poll that table:

```ini
SWING_SIGNAL_SOURCE=db
```

`timeframe_min` maps 60→1H, 240→4H, 1440→1D. Full setup, including the
outbound-3306 check: `integration/amibroker/README_PRODUCTION.md`.

## 5. Ports when running live

The Shoonya gateway uses port 8000, so run the Reversal Strategy backend on a
different port and update the Vite proxy + `ALLOWED_ORIGINS`:

```bash
python -m uvicorn app.main:app --port 8020
# frontend/vite.config.ts proxy target -> http://localhost:8020
```

## 6. Go-live checklist

- [ ] WireGuard tunnel up; Shoonya gateway reachable and logged in.
- [ ] `SWING_BROKER_MODE=shoonya`, gateway URLs set, `SWING_IGNORE_MARKET_HOURS=false`.
- [ ] `POST /api/margin/refresh` returns today's date and a symbol count.
- [ ] `SWING_SIGNAL_SOURCE=db`; the Amibroker box can reach MySQL on 3306.
- [ ] Amibroker Exploration writing the CSV; `run_bridge_sql.bat` inserting (watch its console).
- [ ] Top bar shows green Ami + Shoonya dots and real Shoonya cash.
- [ ] Place one 1-lot test buy; confirm it appears at the broker and reconciliation stays clean.
