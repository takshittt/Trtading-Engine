#!/usr/bin/env bash
# Run every test suite in the monorepo. The pre-push hook calls this; it is also
# the command to run by hand before a push.
#
#   scripts/run-tests.sh            test the working tree as it is on disk
#   scripts/run-tests.sh <commit>   test exactly <commit>, in a throwaway checkout
#
# The hook passes the commit being pushed. Testing the working tree instead would
# let an uncommitted fix make a broken commit look green, and the push would ship
# the broken one.
#
# Virtualenvs and node_modules are not in git, so a throwaway checkout borrows
# them from this one: each suite runs with the main checkout's interpreter
# against the checked-out commit's source. If a commit changes dependencies, run
# `poetry install` / `npm install` here first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMIT="${1:-}"

if [ -n "$COMMIT" ]; then
  SRC="$(mktemp -d "${TMPDIR:-/tmp}/trading-ci.XXXXXX")"
  cleanup() { git -C "$ROOT" worktree remove --force "$SRC" >/dev/null 2>&1 || rm -rf "$SRC"; }
  trap cleanup EXIT
  git -C "$ROOT" worktree add --detach --quiet "$SRC" "$COMMIT"
  echo "testing $(git -C "$ROOT" rev-parse --short "$COMMIT") in a clean checkout"
else
  SRC="$ROOT"
  echo "testing the working tree"
fi

FAILED=()

# py_suite <name> <backend dir, relative to the repo root>
py_suite() {
  local name="$1" dir="$2"
  local py="$ROOT/$dir/.venv/bin/python"
  echo
  echo "━━ $name"
  if [ ! -d "$SRC/$dir/tests" ]; then
    echo "   no tests/ in this commit — skipped"
    return
  fi
  if [ ! -x "$py" ]; then
    echo "   !! $dir/.venv missing — run: (cd $dir && poetry install)"
    FAILED+=("$name (no venv)")
    return
  fi
  # -p no:cacheprovider: never write .pytest_cache into the throwaway checkout.
  if ! (cd "$SRC/$dir" && "$py" -m pytest tests -q -p no:cacheprovider -W ignore::DeprecationWarning); then
    FAILED+=("$name")
  fi
}

# js_suite <name> <frontend dir>
js_suite() {
  local name="$1" dir="$2"
  echo
  echo "━━ $name"
  if [ ! -f "$SRC/$dir/package.json" ]; then
    echo "   not in this commit — skipped"
    return
  fi
  if [ ! -d "$ROOT/$dir/node_modules" ]; then
    echo "   !! $dir/node_modules missing — run: (cd $dir && npm install)"
    FAILED+=("$name (no node_modules)")
    return
  fi
  if [ "$SRC" != "$ROOT" ] && [ ! -e "$SRC/$dir/node_modules" ]; then
    ln -s "$ROOT/$dir/node_modules" "$SRC/$dir/node_modules"
  fi
  if ! (cd "$SRC/$dir" && npx --no-install vitest run); then
    FAILED+=("$name")
  fi
}

py_suite "Gateway backend"            Gateway/gateway_backend
py_suite "grid_strategy backend"      grid_strategy/backend
py_suite "reversal_strategy backend"  reversal_strategy/backend
js_suite "grid_strategy frontend"     grid_strategy/frontend

echo
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "✗ FAILED: ${FAILED[*]}"
  exit 1
fi
echo "✓ all suites passed"
