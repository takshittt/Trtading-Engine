#!/usr/bin/env python3
"""Grid scan bridge — runs on the AmiBroker (Windows) box.

Watches the CSV that grid_scan.afl writes every 5 min and POSTs each fresh
scan to the Grid engine's /webhook/scan endpoint. Display-only: this never
places an order. Standard library only (no pip install needed).

Usage:
    python scan_bridge.py \
        --csv "C:\\grid\\scan_signals.csv" \
        --url http://192.168.133.205:8010/webhook/scan \
        --secret YOUR_SECRET

Env fallbacks: SCAN_CSV, SCAN_URL, AMI_WEBHOOK_SECRET.
"""
import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime


def parse_csv(path: str) -> list[dict]:
    """Read the scanner CSV → list of signal dicts. Skips blank/short rows."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol") or "").strip()
            if not sym:
                continue
            try:
                tf = int(row.get("timeframe_min") or 0)
            except ValueError:
                continue
            action = (row.get("action") or "NEUTRAL").strip().upper()
            try:
                price = float(row.get("price") or 0)
            except ValueError:
                price = 0.0
            out.append({
                "symbol": sym,
                "timeframe_min": tf,
                "action": action,
                "price": price,
                "bar_time": (row.get("bar_time") or "").strip(),
            })
    return out


def post_batch(url: str, secret: str, signals: list[dict]) -> dict:
    body = json.dumps({
        "secret": secret,
        "scan_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signals": signals,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def stable_mtime(path: str, settle: float) -> float | None:
    """Return the file mtime once it has stopped changing for `settle` seconds
    (so we don't read a CSV mid-write). None if the file vanished."""
    try:
        last = os.path.getmtime(path)
    except OSError:
        return None
    while True:
        time.sleep(settle)
        try:
            now = os.path.getmtime(path)
        except OSError:
            return None
        if now == last:
            return now
        last = now


def main() -> None:
    ap = argparse.ArgumentParser(description="Grid AmiBroker scan bridge")
    ap.add_argument("--csv", default=os.getenv("SCAN_CSV", r"C:\grid\scan_signals.csv"))
    ap.add_argument("--url", default=os.getenv("SCAN_URL", "http://192.168.133.205:8010/webhook/scan"))
    ap.add_argument("--secret", default=os.getenv("AMI_WEBHOOK_SECRET", ""))
    ap.add_argument("--poll", type=float, default=10.0, help="seconds between checks")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds the file must be quiet before sending")
    args = ap.parse_args()

    print(f"[bridge] watching {args.csv}")
    print(f"[bridge] posting to {args.url}")
    last_sent: float | None = None

    while True:
        try:
            mtime = stable_mtime(args.csv, args.settle)
            if mtime is None:
                print("[bridge] waiting for CSV to appear…")
            elif mtime != last_sent:
                signals = parse_csv(args.csv)
                if signals:
                    resp = post_batch(args.url, args.secret, signals)
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[bridge] {stamp} sent {len(signals)} rows → {resp}")
                    last_sent = mtime
                else:
                    print("[bridge] CSV had no usable rows; skipping")
        except urllib.error.HTTPError as e:
            print(f"[bridge] HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            print(f"[bridge] cannot reach engine: {e.reason}")
        except Exception as e:  # keep the loop alive no matter what
            print(f"[bridge] error: {e}")
        time.sleep(args.poll)


if __name__ == "__main__":
    main()
