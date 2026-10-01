r"""Amibroker -> MySQL bridge (runs on the Amibroker Windows box).

Same job as scan_bridge.py, different transport: instead of POSTing to the
backend it INSERTs each scan row straight into `ami_signal_inbox` on the shared
database. The backend polls that table and processes the rows. The two machines
never have to reach each other over HTTP.

    Amibroker AFL -> C:\swing\scan_<tf>.csv -> THIS -> MySQL ami_signal_inbox
                                                            |
                                        backend polls ------+-> signals -> trades

Watches a FOLDER for the per-timeframe CSVs written by swing_scan.afl:
    C:\swing\scan_60.csv    (1H)
    C:\swing\scan_240.csv   (4H)
    C:\swing\scan_1440.csv  (1D)

Each file is a QUEUE the AFL only ever appends to. This bridge empties it once
every row it read has been committed, so a file that fails to insert is retried
rather than dropped. The backend dedups per candle, so a replay is harmless.

CSV columns, positional and headerless — identical contract to scan_bridge.py:
    symbol,timeframe_min,action,price,atr,resistance,bar_time,support
optionally followed by target_points,sl_points.

Requires PyMySQL (scan_bridge.py is stdlib-only; this one is not):
    pip install PyMySQL

Run on the Amibroker box:
    python scan_bridge_sql.py --dir C:\swing ^
        --host 129.151.44.44 --user tradingbots --password "..." --database Trading_bots

Or set SWING_SQL_* in the environment. Credentials are read from argv/env and
never written to disk by this script.
"""
import argparse
import csv
import glob
import os
import time

try:
    import pymysql
except ImportError:
    raise SystemExit(
        "PyMySQL is not installed. On the Amibroker box run:  pip install PyMySQL"
    )

DEFAULT_DIR = os.getenv("SWING_SCAN_DIR", r"C:\swing")

# Column order written by swing_scan.afl. The file is append-only, so there is
# no header line to read them from — position IS the contract, and anything new
# is appended at the end so an older row still parses.
COLUMNS = ["symbol", "timeframe_min", "action", "price", "atr", "resistance",
           "bar_time", "support", "target_points", "sl_points"]

INSERT_SQL = """
INSERT INTO ami_signal_inbox
    (symbol, timeframe_min, action, price, atr, resistance, support,
     target_points, sl_points, bar_time, processed, result, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, '', UTC_TIMESTAMP())
"""


def snapshot(paths):
    """path -> (mtime, size), skipping anything that vanished mid-scan."""
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime, st.st_size)
        except OSError:
            pass
    return out


def parse_rows(path):
    rows = []
    try:
        with open(path, newline="") as fh:
            for rec in csv.reader(fh):
                r = dict(zip(COLUMNS, rec))
                action = (r.get("action") or "").strip().upper()
                # Also skips a stray header line left by an older scan, whose
                # "action" column reads "action".
                if action not in ("BUY", "SELL"):
                    continue
                rows.append((
                    (r.get("symbol") or "").strip().upper(),
                    int(float(r.get("timeframe_min") or 60)),
                    action,
                    float(r.get("price") or 0),
                    float(r.get("atr") or 0),
                    float(r.get("resistance") or 0),
                    float(r.get("support") or 0),
                    float(r.get("target_points") or 0),
                    float(r.get("sl_points") or 0),
                    (r.get("bar_time") or "").strip(),
                ))
    except Exception as e:
        print("  parse error", path, e)
    return rows


def connect(args):
    return pymysql.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.database,
        connect_timeout=10, autocommit=False,
    )


def insert(conn, rows):
    """Insert a batch in ONE transaction. Either every row of this file lands or
    none does — a half-written file would otherwise be drained as if delivered,
    losing the remainder. Returns True on commit."""
    try:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)
        conn.commit()
        return True
    except Exception as e:
        print("  INSERT failed:", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder holding scan_*.csv")
    ap.add_argument("--csv", default="", help="(optional) watch a single file instead of the folder")
    ap.add_argument("--host", default=os.getenv("SWING_SQL_HOST", ""))
    ap.add_argument("--port", type=int, default=int(os.getenv("SWING_SQL_PORT", "3306")))
    ap.add_argument("--user", default=os.getenv("SWING_SQL_USER", ""))
    ap.add_argument("--password", default=os.getenv("SWING_SQL_PASSWORD", ""))
    ap.add_argument("--database", default=os.getenv("SWING_SQL_DATABASE", ""))
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=1.0,
                    help="seconds a file must sit unchanged before it is read")
    args = ap.parse_args()

    missing = [n for n in ("host", "user", "password", "database")
               if not getattr(args, n)]
    if missing:
        ap.error("missing required connection setting(s): " + ", ".join(missing))

    if args.csv:
        targets_desc = args.csv
        list_files = lambda: [args.csv]
    else:
        targets_desc = os.path.join(args.dir, "scan_*.csv")
        list_files = lambda: sorted(glob.glob(os.path.join(args.dir, "scan_*.csv")))

    # Fail at startup, not on the first signal of the day. A wrong password
    # discovered at 09:20 is a bad morning; discovered now it is a typo.
    conn = connect(args)
    print(f"Connected to {args.user}@{args.host}:{args.port}/{args.database}")
    print(f"Watching {targets_desc}  ->  ami_signal_inbox")

    while True:
        # Settle ONCE per cycle, not once per file: a file is safe to read when
        # its mtime and size are unchanged either side of the pause.
        files = list_files()
        before = snapshot(files)
        time.sleep(args.settle)
        after = snapshot(files)

        for path, stamp in sorted(after.items()):
            if before.get(path) != stamp:
                continue            # still being written — catch it next cycle
            if stamp[1] == 0:
                continue            # already drained
            name = os.path.basename(path)
            rows = parse_rows(path)
            if not rows:
                continue

            # Reconnect if the pooled socket died overnight; PyMySQL's ping
            # with reconnect=True handles the common "server has gone away".
            try:
                conn.ping(reconnect=True)
            except Exception:
                try:
                    conn = connect(args)
                except Exception as e:
                    print("  reconnect failed:", e)
                    continue

            if not insert(conn, rows):
                continue            # leave the queue intact and retry next cycle

            for r in rows:
                print(f"  [{name}] {r[0]} {r[2]} tf={r[1]} -> inbox")

            # Drain only what we actually read. If the scan appended while we
            # were inserting, the file is longer than the snapshot and
            # truncating would throw those rows away unsent.
            try:
                if os.path.getsize(path) == stamp[1]:
                    open(path, "w").close()
            except OSError as e:
                print("  could not drain", name, e)
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
