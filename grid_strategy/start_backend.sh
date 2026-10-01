#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8010}"

cd "$BACKEND_DIR"
source venv/bin/activate

exec uvicorn main:app --host "$HOST" --port "$PORT" --reload
