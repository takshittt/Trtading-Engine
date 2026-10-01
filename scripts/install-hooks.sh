#!/usr/bin/env bash
# Point this clone's git hooks at the versioned .githooks/ directory.
# Hooks are not cloned with a repo, so every fresh clone runs this once.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$ROOT/.githooks/"* "$ROOT/scripts/"*.sh
git -C "$ROOT" config core.hooksPath .githooks
echo "hooks installed: git push now runs scripts/run-tests.sh on the pushed commit."
echo "deploy-on-push is $(git -C "$ROOT" config --bool hooks.deploy || echo false)" \
     "(enable with: git config hooks.deploy true)"
