# Trading Engine

A monorepo of NSE/MCX trading services built around a single **Gateway**. The Gateway holds the only broker session (Shoonya). Every strategy service is a client of the Gateway over HTTP/WebSocket and imports none of its code.

```
            ┌──────────────────────────┐
 Broker ◄──►│  Gateway  :8000 / UI 5173│  auth, broker session, orders,
            └────────────┬─────────────┘  positions, market data, ticker WS
                         │ HTTP + WS (service JWT)
          ┌──────────────┴──────────────┐
 ┌────────▼─────────┐         ┌─────────▼──────────┐
 │ grid_strategy    │         │ reversal_strategy  │
 │ :8010 / UI 5175  │         │ :8020 / UI 5176    │
 └──────────────────┘         └────────────────────┘
        ▲                              ▲
        └──── Amibroker signals ───────┘
```

## Projects

| Directory | What it is | Stack | Ports |
|---|---|---|---|
| [Gateway/](Gateway/) | Identity provider and the single broker session. Exposes orders, positions, market data, and a ticker WebSocket. | FastAPI + React/Vite | API 8000, UI 5173 |
| [grid_strategy/](grid_strategy/) | **Grid** ladder (grid) strategy engine. Takes signals from an Amibroker webhook and includes a backtester. | FastAPI + React/Vite | API 8010, UI 5175 |
| [reversal_strategy/](reversal_strategy/) | **Reversal Strategy**, a long-only NSE futures swing strategy. The user enters manually; the backend manages target, stop-loss, averaging, rollover, and exits. | FastAPI + React/Vite | API 8020, UI 5176 |
| [docs/](docs/) | Cross-project docs: [AUTH_PIPELINE.md](docs/AUTH_PIPELINE.md) and [CLAUDE.md](docs/CLAUDE.md). | | |

## Getting started

Requirements: Python 3.11+, Node 18+, [Poetry](https://python-poetry.org/) 2.x (`pipx install poetry`).

Each backend (`Gateway/gateway_backend`, `grid_strategy/backend`, `reversal_strategy/backend`) is its own Poetry project: `pyproject.toml` declares the dependencies, `poetry.lock` pins them, and `poetry.toml` keeps the virtualenv in `.venv/` inside the backend directory. Commit `poetry.lock` whenever dependencies change (`poetry add <pkg>`, `poetry add --group dev <pkg>`).

Start the **Gateway first**. Both strategies depend on it for auth and broker access.

### 1. Gateway

```bash
cd Gateway/gateway_backend
poetry install                  # creates .venv/ from poetry.lock
cp .env.example .env            # fill in JWT_SECRET, DATABASE_URL, broker creds
poetry run uvicorn app.main:app --port 8000

cd ../frontend
npm install && npm run dev      # http://localhost:5173
```

### 2. Register each strategy as a service

Run these from `Gateway/gateway_backend`. Each strategy needs its own `client_id`/`client_secret`. The secret is printed **once**.

```bash
poetry run python -m scripts.register_service --name grid
poetry run python -m scripts.register_service --name <svc> --rotate              # new secret
poetry run python -m scripts.register_service --name <svc> --add-scope connect   # allow broker login/logout
```

### 3. Strategies

```bash
# Grid (grid_strategy)
cd grid_strategy/backend
poetry install                  # add --with backtest for the optional backtester (yfinance, pandas)
cp .env.example .env            # GATEWAY_BASE_URL, GATEWAY_CLIENT_ID/SECRET, JWT_SECRET
poetry run uvicorn main:app --port 8010
cd ../frontend && npm install && npm run dev

# Reversal Strategy (reversal_strategy)
cd reversal_strategy/backend
poetry install
cp .env.example .env            # SWING_-prefixed gateway settings, JWT_SECRET
poetry run uvicorn app.main:app --port 8020
cd ../frontend && npm install && npm run dev
```

## Auth model

The full details are in [docs/AUTH_PIPELINE.md](docs/AUTH_PIPELINE.md).

- The Gateway owns the `users` and `services` tables and the HS256 `JWT_SECRET`. Strategies have no user store of their own.
- **User JWT** (7 days): a strategy's `/api/auth/login` proxies to the Gateway. Each strategy verifies the token locally, so **`JWT_SECRET` must be byte-identical in every `.env`**.
- **Service JWT** (1 hour, scoped): a strategy exchanges its `client_id`/`client_secret` at `/api/auth/service-token`.
- The broker session is **shared**. A disconnect from any dashboard logs out every strategy. See [Gateway/docs/SERVICE_BROKER_CONNECT.md](Gateway/docs/SERVICE_BROKER_CONNECT.md).

## Testing

```bash
# Gateway
cd Gateway/gateway_backend && poetry run pytest tests
# test_shoonya_adapter.py performs a live broker login
# and is excluded from collection. Run them directly only when you mean it.

# Grid
cd grid_strategy/backend && poetry run pytest tests
cd grid_strategy/frontend && npm test          # vitest

# Frontend lint / type-check build (Gateway, grid_strategy)
npm run lint
npm run build
```

`reversal_strategy` has no automated test suite.

## Databases and runtime state

- Every backend defaults to a local SQLite file. Setting `DATABASE_URL` / `GRID_DB_URL` / `SWING_DB_URL` in `.env` switches it to MySQL. **A deployed `.env` may point at shared production MySQL**, so check it before running anything locally.
- Never commit `.env`, `*.db`, `*.db-wal`, `*.db-shm`, `token_store*.json`, or scripmaster dumps. These are live state, and `.gitignore` already excludes them. Replacing the SQLite sidecar files under a running backend corrupts the database.

## Further reading

- [docs/AUTH_PIPELINE.md](docs/AUTH_PIPELINE.md): users, services, scopes, token flow
- [Gateway/gateway_backend/db/MODELS.md](Gateway/gateway_backend/db/MODELS.md): Gateway DB schema
- [reversal_strategy/docs/](reversal_strategy/docs/): architecture, integration, deployment, and ops cheatsheet for Reversal Strategy
- `.claude/skills/afl-writer`: Claude skill for writing the Amibroker AFL scripts that feed signals to the strategies
