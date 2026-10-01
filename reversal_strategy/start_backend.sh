#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

# LIVE mode — requires the Shoonya gateway running (see INTEGRATION.md).
# LIVE backend runs on 8020 so the Shoonya gateway can own 8000.
HOST="${HOST:-0.0.0.0}"
PORT="${SWING_PORT:-8020}"

cd "$BACKEND_DIR"
source venv/bin/activate

echo "▶ LIVE backend → http://$HOST:$PORT  (requires the Shoonya gateway; see INTEGRATION.md)"
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT"
