#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

# LIVE backend runs on 8020; the Vite proxy (build + preview) must target it.
export SWING_BACKEND_PORT="${SWING_BACKEND_PORT:-8020}"

cd "$FRONTEND_DIR"
npm run build
exec npm run preview
