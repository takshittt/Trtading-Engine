#!/usr/bin/env bash
# Launch Reversal Strategy (backend :8000 + frontend :5173).
# Usage:  ./run.sh          (respects .env)
#         DEMO=1 ./run.sh   (dev mode: market always open, mock feeds)
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# --- Backend ---
cd "$ROOT/backend"
[ -d venv ] || python3 -m venv venv
. venv/bin/activate
pip install -q -r requirements.txt
# DEMO=1 → offline simulator (no gateway/credentials). Default → LIVE (Shoonya + Amibroker).
if [ "${DEMO:-0}" = "1" ]; then
  export SWING_IGNORE_MARKET_HOURS=true SWING_BROKER_MODE=mock SWING_SIGNAL_SOURCE=mock
  echo "▶ DEMO mode (simulated data, no broker)"
else
  echo "▶ LIVE mode — requires the Shoonya gateway running (see INTEGRATION.md)"
fi
# Live backend runs on 8020 so the Shoonya gateway can own 8000.
PORT="${SWING_PORT:-8020}"; [ "${DEMO:-0}" = "1" ] && PORT=8000
echo "▶ Backend  → http://0.0.0.0:$PORT  (reachable from the Amibroker box)"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" &
BACK=$!

# --- Frontend ---
cd "$ROOT/frontend"
[ -d node_modules ] || npm install
echo "▶ Frontend → http://localhost:5173"
SWING_BACKEND_PORT="$PORT" npm run dev &
FRONT=$!

trap "kill $BACK $FRONT 2>/dev/null" EXIT
wait
