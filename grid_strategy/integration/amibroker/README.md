# AmiBroker F&O Scanner → Grid "Check Signals"

Display-only pipeline: AmiBroker scans every F&O stock across 4 timeframes every
5 minutes and the latest BUY / SELL / NEUTRAL per stock shows up in the dashboard
under **📡 Check Signals**. **No orders are ever placed.**

```
AmiBroker Exploration (grid_scan.afl)   every 5 min
        │  writes scan_signals.csv
        ▼
scan_bridge.py  (this box)                  reads CSV, POSTs a batch
        │  POST http://192.168.133.205:8010/webhook/scan
        ▼
Grid engine → scan_signals table → GET /api/scan-signals → dashboard grid
```

## 1. AmiBroker (Windows box, 192.168.133.51)

1. Open **grid_scan.afl**, edit `CSV_PATH` (default `C:\grid\scan_signals.csv`)
   and create that folder.
2. Replace the EMA-crossover block (the two `bs`/`ss` lines) with your real
   buy/sell strategy. Nothing else needs changing.
3. Analysis window → apply the formula → set the **filter to your F&O watchlist**
   → Range = "n last bars" (a few hundred; base DB interval must be ≤ 5-minute).
4. Click **Explore**, then enable **Auto-Repeat (AR) = 5 minutes**.

## 2. The bridge (same box)

```bat
python scan_bridge.py --csv "C:\grid\scan_signals.csv" ^
    --url http://192.168.133.205:8010/webhook/scan --secret YOUR_SECRET
```

- `--secret` must match the engine's `AMI_WEBHOOK_SECRET` (leave blank if the
  engine has none set — local/testing only).
- Standard-library only; any Python 3.9+ works. Leave it running (or install it
  as a service / Task Scheduler task).

## 3. Verify

Open the dashboard → **📡 Check Signals**. The banner at the top shows when the
last scan arrived:

- 🟢 **green** — a scan landed in the last 6 min → pipeline is live.
- 🟠 **amber** — overdue (>6 min).
- 🔴 **red** — nothing recent / never → AFL or bridge is down.

## CSV format

```
symbol,timeframe_min,action,price,bar_time
RELIANCE,5,BUY,2950.5,2026-07-22T14:30:00
RELIANCE,240,SELL,2951,2026-07-22T12:15:00
TCS,15,NEUTRAL,0,
```

`timeframe_min`: 5 / 15 / 60 / 240. `action`: BUY / SELL / NEUTRAL. `bar_time`:
ISO time the signal fired (blank for NEUTRAL). Signals are **sticky** — a BUY/SELL
stays until a strictly newer one replaces it; NEUTRAL never overwrites a real signal.
