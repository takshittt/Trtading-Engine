# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A monorepo of NSE/MCX trading services built around one **Gateway**, which holds the only broker session (Shoonya). Every other project is a client of the Gateway over HTTP/WebSocket. None of them import its code.

| Project | Stack | Port(s) | Role |
|---|---|---|---|
| `Gateway/` | FastAPI (`gateway_backend/app/main.py`) + React/Vite UI | 8000 / UI 5173 | Identity provider, broker session, orders/positions/market data, ticker WS |
| `grid_strategy/` | FastAPI (`backend/main.py`) + React/Vite UI | 8010 | Grid ladder strategy engine; Amibroker webhook in |
| `reversal_strategy/` | FastAPI + React | 8020 / UI 5176 | Long-only NSE futures swing strategy. **Has its own [CLAUDE.md](reversal_strategy/CLAUDE.md). Read it before working there.** |
| `Options_Strategy_Advisor/` | Pure-Python package `optionsmith` | UI 8030 (`start.sh`) | Option-structure ranking. It imports nothing from other projects and gets live chains from the Gateway over HTTP. See its README. |
| `NG_Trading/`, `Copper_Trading/` | Flask + SocketIO monolith (`backend/app.py`, ~8k lines), MySQL | `FLASK_PORT` (5005) | Reverse Jade Lizard on MCX. The two are **near-copies** of each other, so a fix in one usually needs porting to the other. |

## Auth model (read [AUTH_PIPELINE.md](AUTH_PIPELINE.md) before touching auth)

- The Gateway owns the `users` and `services` tables and `JWT_SECRET` (HS256). Clients have no user store.
- **User JWT** (`typ:"user"`, 7d): a client's `/api/auth/login` proxies to the Gateway's `/api/auth/signin`. Clients verify it locally because `JWT_SECRET` is **byte-identical** across all `.env` files.
- **Service JWT** (`typ:"service"`, 1h, scoped): a client exchanges `client_id`/`client_secret` at `/api/auth/service-token`. The Gateway gates routers with `require_scope(...)` in `app/core/security.py`. Humans implicitly hold all scopes.
- Broker login/logout (`/api/connect`, `/api/disconnect`) needs the `connect` scope. The session is **shared**, so a disconnect from any dashboard logs out every strategy. See [Gateway/docs/SERVICE_BROKER_CONNECT.md](Gateway/docs/SERVICE_BROKER_CONNECT.md).
- Env var names differ per client: Grid uses `GATEWAY_CLIENT_ID`/`GATEWAY_BASE_URL`, Reversal uses a `SWING_` prefix, and NG/Copper use `GATEWAY_URL` + `USE_GATEWAY`.
- Service credentials are issued from `Gateway/gateway_backend/`:
  ```bash
  python -m scripts.register_service --name <svc>                    # create (secret printed once)
  python -m scripts.register_service --name <svc> --rotate
  python -m scripts.register_service --name <svc> --add-scope connect  # no secret/.env change needed
  ```

## Commands

Python deps are managed with Poetry. Each backend has its own `pyproject.toml` + `poetry.lock`, and `poetry.toml` puts the venv at `.venv/` in that backend directory. Run `poetry install` there to set it up, `poetry add <pkg>` to add a dependency (commit the updated `poetry.lock`), and prefix commands with `poetry run`. pytest is in the `dev` group; Grid's backtester deps (yfinance, pandas) are in the optional `backtest` group (`poetry install --with backtest`).

```bash
# Gateway backend
cd Gateway/gateway_backend && poetry run pytest tests
poetry run pytest tests/test_market_hours.py::test_name   # single test
# test_shoonya_adapter.py is a live-broker script, excluded
# from collection (conftest.py). Run them directly with python only when you mean to log in.

# Grid backend
cd grid_strategy/backend && poetry run pytest tests
cd grid_strategy/frontend && npm test          # vitest

# Reversal Strategy (reversal_strategy) backend
cd reversal_strategy/backend && poetry run pytest tests

# Everything (what the pre-push hook runs)
scripts/run-tests.sh                 # working tree
scripts/run-tests.sh <commit>        # exactly that commit, in a throwaway worktree
scripts/install-hooks.sh             # once per clone: git push then refuses on red
SKIP_TESTS=1 git push                # override, when you know why
```

The pre-push hook tests the **commit being pushed**, not the working tree, using
each backend's `.venv` and `node_modules` from the main checkout. A clean checkout
has no `.env`, so every `tests/conftest.py` must supply the env vars its app needs
at import time (and pin the DB URL to a temp SQLite file) rather than relying on
`.env`.

```bash

# OptionSmith (venv at Options_Strategy_Advisor/venv or .venv)
cd Options_Strategy_Advisor && python -m pytest tests
python -m optionsmith demo                      # offline, synthetic chain

# Frontends (Gateway/frontend, grid_strategy/frontend)
npm run lint
npm run build                                   # tsc -b && vite build
```

NG/Copper have no working tests: their `pytest.ini` points at a nonexistent `gateway/tests`. The many root-level `fix_*.py` / `debug_*.py` files there are one-off repair scripts, not part of the app.

## Databases

- `db/engine.py` in the Gateway (`DATABASE_URL`), Grid (`GRID_DB_URL`), and Reversal (`SWING_DB_URL`) defaults to a local SQLite file and switches to MySQL from `.env`. The deployed `.env` points at **shared production MySQL**.
- Grid's `tests/conftest.py` pins a throwaway SQLite URL *before* any import for exactly this reason. Keep that pattern in any new test setup.
- Do not commit or check out `*.db`, `*.db-wal`, or `*.db-shm`. Replacing the sidecars under a running backend corrupts the DB.
- `token_store*.json` holds live broker sessions and `scripmaster.csv` is runtime state. Neither is source.

## Deploy

`./deploy.sh [--dry-run] [--reconnect]` rsyncs the **working tree** (not git) to `root@192.168.133.205:/opt/gateway_system`, then reinstalls, rebuilds, and restarts only what changed. Things to know before running it:

- It deploys **only the Gateway and Grid**. Reversal and OptionSmith have their own `deploy.sh`.
- It never syncs `.env`, token stores, scripmaster, DBs, venvs (`.venv/`), or `package-lock.json`.
- **Restarting the Gateway backend drops the live broker session.** `--reconnect` restores it through the cached same-day token. Off-hours this may trigger a real Shoonya login.
- systemd units: `gw-central`, `gw-central-ui`, `grid-engine-gw`, `grid-dashboard-gw`.

## Gateway backend notes

- `tls_compat` must be the first import in `app/main.py`, because Shoonya rejects TLS 1.3.
- Brokers plug in through `brokers/base.py` + `brokers/registry.py`. Background work (position sync, persistent-order sweeper, ticker callbacks) is wired in `app/background/startup.py`.
- The DB schema is documented in `db/MODELS.md`.

## Amibroker

Signals for Grid and Reversal come from AFL scripts under `*/integration/amibroker/`, which run on a separate Windows box. `deploy.sh` does not reach that box. For AFL work, use the repo's `afl-writer` skill (`.claude/skills/afl-writer`).
