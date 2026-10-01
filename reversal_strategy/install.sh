#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

echo "==> Installing Reversal Strategy backend dependencies"
cd "$BACKEND_DIR"
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

echo "==> Installing Reversal Strategy frontend dependencies"
cd "$FRONTEND_DIR"
npm install

echo "==> Reversal Strategy install complete"
