# Production AmiBroker server — what to install

Three files go on the client's AmiBroker box.

| File | Goes to | Purpose |
|---|---|---|
| `swing_scan.afl` | AmiBroker `Formulas\Custom\` | the scan (EMA 9/21 crossover) |
| `scan_bridge_sql.py` | `C:\swing\bridge\` | CSV → MySQL |
| `run_bridge_sql.bat` | `C:\swing\bridge\` | launcher (edit the DB block) |

## Flow

```
swing_scan.afl  ->  C:\swing\scan_<tf>.csv
      -> scan_bridge_sql.py
      -> INSERT into  ami_signal_inbox   (MySQL Trading_bots @ 129.151.44.44)
                              |
      backend polls the table +-> swing_signals -> trades
```

This machine never calls the backend over HTTP. The **database is the handoff**.
The backend can be down, restarted or moved and signals keep accumulating in
`ami_signal_inbox` until it comes back.

`ami_signal_inbox` is a separate table from `swing_signals` on purpose. It holds
the raw scan rows exactly as AmiBroker wrote them. The backend reads them,
applies timeframe validation, IST→UTC conversion, candle dedup, target/stop and
auto-execute, and writes the finished row into `swing_signals`. Nothing should
INSERT into `swing_signals` directly — those columns are computed, not supplied.

Note the name: this database is shared with another (ladder) bot that owns the
plain `signals` table. Ours is `swing_signals`. Do not confuse the two when
querying — they are unrelated systems.

## Timeframes

One AFL covers all three — it reads the chart's own interval
(`TF_MIN = Interval() / 60`) and routes to a matching file:

| Chart | writes |
|---|---|
| 1 hour | `C:\swing\scan_60.csv` |
| 4 hour | `C:\swing\scan_240.csv` |
| Daily | `C:\swing\scan_1440.csv` |

Those three are the only intervals accepted. Anything else is stored in the
inbox with `result = "unsupported timeframe N"` and never becomes a signal.

## Setup

1. **Python 3**, with "Add python.exe to PATH" ticked.

2. **PyMySQL** — `python -m pip install PyMySQL`. The `.bat` installs it
   automatically if missing. (Note: the older HTTP bridge was stdlib-only; this
   one is not.)

3. **Edit the DB block in `run_bridge_sql.bat`** — host / user / password /
   database. Keep the `set "VAR=value"` quoted form: the password contains `<`,
   which cmd.exe treats as a redirect in the unquoted form, and `%` must be
   written `%%`.

   To keep the password out of the file, delete the `--password` argument and
   set `SWING_SQL_PASSWORD` as a Windows environment variable instead — the
   script reads it from there.

4. **AmiBroker**: load `swing_scan.afl` as an **Exploration**, Auto-repeat every
   1 minute, Analysis range "All quotations" (or n last quotations, n ≥ 30).
   Set the chart interval to 1H, 4H or Daily. Add a second/third Exploration
   for more timeframes — they write to different files and don't conflict.

   Leave **Backfill Bars at 1**. Raising it re-exports older closed candles, and
   a backfilled bar the backend never saw counts as NEW — with Paper Trading on
   it is auto-executed at today's price off a setup that fired hours ago.

5. Double-click `run_bridge_sql.bat`. Leave the window open.

## About `C:\swing`

**The folder must exist. The path itself is yours to choose.**

AmiBroker's `fopen(path, "a")` creates the *file*, never the *directory*, and
the AFL guards the handle:

```
fh = fopen(csvPath, "a");
if (fh) { fputs(out, fh); fclose(fh); }
```

so a missing folder means the write is skipped and **nothing is reported**. The
Exploration window still lists BUY/SELL rows, so the scan looks fine while zero
rows reach the CSV. The `.bat` creates the folder for this reason.

To move it, change **both** and keep them identical:
1. `swing_scan.afl` line 101 — `csvPath = "C:\\swing\\scan_" + tfStr + ".csv";`
   (backslashes stay doubled in AFL)
2. `SCANDIR` in `run_bridge_sql.bat`

The `scan_<tf>.csv` filenames are not free to change — the bridge globs
`scan_*.csv` and reads the timeframe from inside each row.

## Signals only fire on closed candles

The scan never reads the candle still forming. `lastClosed = lastBar - 1`
(swing_scan.afl:78) steps back one bar, and the export loop only ever runs up to
that. So the 11:00 candle sends its signal at 12:00, once the 12:00 bar opens.

The AmiBroker **Exploration window** does show rows for the forming candle —
that display is not gated. Those rows are not written to the CSV and are not
sent. Trust the CSV and the database, not the window.

Consequence, deliberate: the session's final stub bar (15:15–15:30) is never
exported, because no later bar ever opens to close it.

## Checking it works

The bridge prints one line per signal:

```
  [scan_240.csv] RELIANCE-FUT BUY tf=240 -> inbox
```

Then, on the database:

```sql
SELECT id, symbol, timeframe_min, action, bar_time, processed, result
FROM ami_signal_inbox ORDER BY id DESC LIMIT 20;
```

| what you see | means |
|---|---|
| no rows | problem is on **this** machine — AFL, folder, or bridge |
| rows with `processed = 0` | bridge is fine, the **backend** is not running |
| `processed = 1`, `result = ''` | accepted, now in `swing_signals` |
| `result = 'duplicate'` | same candle already stored — normal on a replay |
| `result = 'unsupported timeframe N'` | chart interval isn't 1H/4H/1D |

Rows are never deleted, so this table is also the audit trail of exactly what
AmiBroker sent and what became of it.
