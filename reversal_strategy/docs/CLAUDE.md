# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Reversal Strategy — an isolated **long-only** NSE futures strategy. Amibroker scans push signals in,
the user buys manually from a dashboard, and the backend then owns Target / Stop-Loss / averaging
/ rollover / exits. Fully decoupled from the main Gateway at `/Users/mac/gateway_system/Gateway`
(reused only as a UI template).

`README.md` has the feature→file map, `ARCHITECTURE.md` the sequence diagrams,
`INTEGRATION.md` the go-live walkthrough, `COMMANDS.md` the server ops cheatsheet.

## Commands

```bash
DEMO=1 ./run.sh      # everything, simulated broker + signals, market forced open (backend :8000, UI :5173)
./run.sh             # LIVE — needs the Shoonya gateway running (backend :8020)
./deploy.sh          # rsync → rebuild frontend → restart services on root@192.168.133.205
```

Verification before committing — **there is no test suite** (pytest is in the Poetry dev group
but no test files exist), so this is the checking path:

```bash
cd frontend && npx tsc --noEmit                                  # must be clean
cd backend && poetry run python -c "import app.main"    # catches import/wiring breakage
```

For anything touching signals, orders or exits, run a throwaway backend against a **copy** of a
DB rather than reasoning about it — `SWING_DB_PATH=<copy> SWING_BROKER_MODE=mock
SWING_SIGNAL_SOURCE=mock SWING_IGNORE_MARKET_HOURS=true poetry run uvicorn
app.main:app --port 809X` — then drive it over HTTP. Takes ~12s to start.

Live server: backend `:8020`, dashboard `:5176`, `journalctl -u swing-swager -f` for logs.

## Architecture

**Signal path.** `swing_scan.afl` (Amibroker, Windows box) → appends to `C:\swing\scan_<tf>.csv`
→ `scan_bridge_sql.py` reads new rows and INSERTs them into `ami_signal_inbox` (MySQL) →
`app/services/ami_inbox.py` polls it (`SWING_SIGNAL_SOURCE=db`) → `app/services/signals.py:ingest`.

The CSV is an **append-only queue, headerless and positional** — the AFL only appends, and the bridge
never modifies it: it keeps a byte-offset bookmark per file in `C:\swing\.scan_bridge_sql_state.json`
and advances it only once a row is delivered. Never truncate from the AFL (Explorations are
multi-threaded; a truncate races with other symbols' appends and erases rows). New columns go at
the **end**, or every existing row is silently reinterpreted. `COLUMNS` in `scan_bridge_sql.py` is the
contract.

**Ingest gates**, in order: dedup by `candle_id` → market hours → blacklist → global halt →
averaging tag → Target/SL → reward:risk. Each sets a `status`; only `NEW` and `AVERAGING` are
actionable.

`signals.refresh()` re-runs `_gate()` on **every read** of a stored signal, because signals outlive
their day. Any new status must be explicitly preserved in `_gate()` or it gets promoted back to
`NEW` on the next poll.

**Target / SL come from Amibroker's support & resistance** — resistance above is the Target,
support below is the Stop. These are *absolute prices*, so `refresh()` anchors re-derivation to
`signal_ltp`, never the live LTP (deriving from a moving price walks the target up the chart and it
is never reached). ATR × multiplier is the fallback when S&R is missing or on the wrong side of the
entry; the `default_*_pct` percentages are a last resort only.

`min_rr` (default 1.5) gates on the R:R **at the signal price**, stored as `signal_rr`. The `rr` on
the API is measured against the live price and decays as a stock approaches its target — gating on
that would accept a signal at 09:15 and reject the same one at 09:20. Below the bar → status
`LOW_RR`, stored and priced but not actionable.

**Exit engine** (`app/services/exit_engine.py`) runs on every price tick, under a non-blocking lock.
Target hit does **not** sell — it arms a trailing stop. `exit_mode` config: `auto` squares off on a
breach, `manual` flags the position (`sl_breached`) and sells nothing; flipping back to auto sweeps
and closes anything still below its stop inside the request, since after the close there is no next
tick. `on_tick` swallows exceptions, so a crash inside `_check_symbol` silently disables exits for
that symbol until restart — be careful what can raise in there.

**Execution** (`app/services/execution.py`) is LIMIT-only with slippage ticks and retries. An order
the broker leaves `OPEN` is polled for ~5s then cancelled; partial fills are honoured via
`qty_filled`. This blocks the caller, including the tick path.

## Landmines

**Config is a single DB row**, live-editable from the UI. `DEFAULTS` in `app/core/config.py` only
seeds a *fresh* database — the running server's values will differ. Always read
`/api/config` before assuming.

**SQLite or MySQL.** `db/engine.py` builds the engine from `SWING_DB_URL` — default `sqlite:///…`
(local, isolated, with WAL + a 4s busy_timeout), or `mysql+pymysql://…` to run on the shared DB
server like Gateway/grid. Models declare explicit `String(N)` lengths, so `create_all` produces
valid MySQL DDL with no VARCHAR-length pass. `IS_SQLITE` gates the SQLite-only bits (pragmas, the
file-copy backup in `background.py`).

**No migration tool.** `db/engine.py:_migrate()` runs `ALTER TABLE ADD COLUMN` for any model column
the DB lacks, compiled for the active dialect (so it works on MySQL too), using the model's
Python-side default. Each ALTER is guarded so one column MySQL refuses can't abort startup. So every
new column needs a scalar default, and **the default is what existing rows get** — e.g.
`market_holidays` is seeded with the real calendar rather than `""`, because `""` would have quietly
made every holiday tradable.

**SQLite and MySQL both return datetimes naive.** Always `as_utc()` (`app/core/timeutil.py`) before
comparing with an aware `datetime.now(timezone.utc)`, or it raises `TypeError` at runtime.

**`deploy.sh` rsyncs the working tree, not git**, and excludes `.git` and `.env` — the server is not
a checkout, so there is nothing to `git pull` there. It also does **not** reach the Amibroker
Windows box: the AFLs and `scan_bridge_sql.py` must be copied across by hand, and they must go
together (the AFL's column layout and the bridge's `COLUMNS` are one contract).

**The live server has `SWING_IGNORE_MARKET_HOURS=true`** in `backend/.env`. That
short-circuits `is_market_open()` unconditionally, so market-hours logic and out-of-session tick
rejection are *not actually in effect* there. `/api/config` reports the truth as
`market_hours_bypassed`.

**`swing_scan.afl` still contains the placeholder strategy** — an EMA(9)/EMA(21) crossover, clearly
labelled. It is not the real Reversal logic. Every signal the system has acted on came from it.

## Testing against the mock broker

Mock symbols need the `-FUT` suffix and must exist in `UNIVERSE` (`app/brokers/mock_gateway.py`);
an invented symbol gets no price and its orders fail. Exits also need `STATE.set_price(symbol, ltp)`
seeded — passing an LTP to `_check_symbol` alone is not enough, `exit_full` reads from `STATE`.
Paper trading fills off the bid/ask without touching the gateway at all.

## Conventions

Comments explain *why*, especially the non-obvious constraint a line is defending against — match
that density rather than narrating what the code does. Commit messages follow the same style: what
changed and the reasoning, in prose.
