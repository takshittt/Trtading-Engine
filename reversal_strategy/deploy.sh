#!/usr/bin/env bash
# Deploy Reversal Strategy to the trading server. Run from the project root on your Mac.
#   ./deploy.sh
set -e

SERVER="${SWING_SERVER:-root@192.168.133.205}"
DEST=/opt/Bottom_Swing_Automation

echo "▶ 1/4  Syncing code to $SERVER:$DEST (skips .env, venv, node_modules, db) ..."
rsync -az --delete \
  --exclude='venv' --exclude='node_modules' --exclude='dist' \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='*.db' --exclude='*.db.*' --exclude='*.bak' \
  --exclude='*.log' --exclude='data/margins.json' --exclude='tsconfig.tsbuildinfo' \
  ./ "$SERVER:$DEST/"

echo "▶ 2/4  Updating backend dependencies ..."
ssh "$SERVER" "cd $DEST/backend && ./venv/bin/pip install -q -r requirements.txt"

echo "▶ 3/4  Rebuilding frontend ..."
ssh "$SERVER" "bash -lc 'cd $DEST/frontend && npm install --no-fund --no-audit >/dev/null 2>&1 && SWING_BACKEND_PORT=8020 npm run build >/dev/null 2>&1'"

echo "▶ 4/4  Restarting services ..."
ssh "$SERVER" "systemctl restart swing-swager swing-swager-ui && sleep 3 && \
  echo '   backend:' \$(systemctl is-active swing-swager) '  ui:' \$(systemctl is-active swing-swager-ui)"

echo "✓ Deployed.  Dashboard: http://192.168.133.205:5176   API: http://192.168.133.205:8020"
