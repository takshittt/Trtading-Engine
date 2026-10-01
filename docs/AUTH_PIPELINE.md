# Auth Pipeline — Gateway ↔ Grid / reversal_strategy

> For fellow devs: how login and service-to-service auth flow between the
> **Gateway** and the services that consume it — currently the **Grid
> engine** and **reversal_strategy**. Both follow the identical two-flow
> model described below; only their env var naming differs (see
> [Client `.env` files](#client-env-files)). Read this before touching anything
> under `Gateway/gateway_backend/app/`, `grid_strategy/backend/gateway_client/`,
> or `reversal_strategy/backend/app/brokers/shoonya_gateway.py`.

## TL;DR

- The **Gateway is the single identity provider.** It owns the `users` table and
  the JWT signing secret. Grid has **no user store**.
- There are **two kinds of JWT**, both signed with the same `JWT_SECRET` (HS256):
  - **User JWT** — a human logged into a dashboard (`typ: "user"`, 7-day TTL).
  - **Service JWT** — Grid-the-machine calling the Gateway API
    (`typ: "service"`, 1-hour TTL, carries scopes).
- Grid shares the Gateway's `JWT_SECRET` (same value in both `.env` files), so
  it can **verify a user JWT locally** without a round-trip.
- Grid authenticates itself to the Gateway with a **`client_id` +
  `client_secret`** (client-credentials grant) and gets a service JWT back.

---

## The two identities

| | Human user | Grid service |
|---|---|---|
| Who | A person logging into a browser dashboard | The Grid backend process |
| Credential | email + password | `client_id` + `client_secret` |
| Stored in Gateway DB | `users` table (bcrypt password hash) | `services` table (bcrypt secret hash) |
| Token type | `typ: "user"` | `typ: "service"` |
| TTL | 7 days | 1 hour (auto-refreshed by the engine) |
| Authorization | Implicitly holds all scopes | Only its granted `scopes` subset |

Both live in the same JWT format and share one signing key. Code:
[app/core/security.py](Gateway/gateway_backend/app/core/security.py).

---

## Flow 1 — Human login (Grid UI → Gateway)

Grid's frontend never talks to the Gateway's user store directly. It proxies
the login through its own backend, which asks the Gateway to verify.

```
Browser (Grid UI)
   │  POST /api/auth/login  { email, password }
   ▼
Grid backend  (api/user_auth.py)
   │  POST /api/auth/signin  { email, password }   ── proxied ──►
   ▼
Gateway backend
   │  looks up users table, verifies bcrypt password
   │  signs a USER JWT (typ:"user")  ── returns { token, user } ──►
   ▼
Grid backend  ── relays { token, user } ──►  Browser
```

- Grid code: [api/user_auth.py](grid_strategy/backend/api/user_auth.py)
- reversal_strategy code: [app/routers/user_auth.py](reversal_strategy/backend/app/routers/user_auth.py)
  (same proxy pattern, own `/api/auth/login` route)
- Gateway verifies against the `users` table and signs the token
  (`create_access_token`).
- Result: Grid has its **own independent login session**. Because the
  `JWT_SECRET` is shared, Grid's backend can **verify that user JWT on its own**
  for every subsequent UI request — no call back to the Gateway needed.

> You can be signed into the Gateway as one user and into Grid as a different
> (Gateway-registered) user; the two tokens live in separate origins and are never
> shared.

## Flow 2 — Service auth (Grid engine → Gateway API)

This is how Grid-the-machine calls Gateway endpoints (orders, positions,
funds, market data, the ticker WebSocket).

```
Grid backend  (gateway_client/auth.py)
   │  POST /api/auth/service-token
   │       { client_id: GATEWAY_CLIENT_ID, client_secret: GATEWAY_CLIENT_SECRET }
   ▼
Gateway backend  (app/routers/service_auth.py)
   │  looks up services table by client_id (is_active=True)
   │  verifies bcrypt(client_secret) against stored client_secret_hash
   │  signs a SERVICE JWT (typ:"service", scopes:[...], 1h TTL)
   │  ── returns { token, scopes, expires_in } ──►
   ▼
Grid backend
   attaches  Authorization: Bearer <service JWT>  to every Gateway REST call
   and appends it to the ticker WebSocket URL
```

- Grid code: [gateway_client/auth.py](grid_strategy/backend/gateway_client/auth.py)
- reversal_strategy code: [app/brokers/shoonya_gateway.py](reversal_strategy/backend/app/brokers/shoonya_gateway.py)
  (`_fetch_service_token`, `_AuthedSession`, `_ws_url_with_auth` — same flow, different file)
- Gateway code: [app/routers/service_auth.py](Gateway/gateway_backend/app/routers/service_auth.py)
- The credentials are sent in the **JSON body** of the POST (not an HTTP header).
- On the Gateway, `require_scope(...)` gates each protected route: a service is
  allowed only if its token carries the required scope; a human user implicitly
  passes. See `Principal` / `require_scope` in
  [app/core/security.py](Gateway/gateway_backend/app/core/security.py).

---

## Flow 2b — Service-triggered broker connect (the `connect` scope)

Broker login/logout (`POST /api/connect`, `/api/disconnect`) is gated by a
dedicated **`connect`** scope (in `ALL_SCOPES`,
[app/core/security.py](Gateway/gateway_backend/app/core/security.py)). A human
user implicitly holds it; a service holds it only if granted. Both Grid and
reversal_strategy expose a **Connect** control that calls their own
backend, which relays to the Gateway's `/api/connect` using the service JWT.

- **Idempotency guard (services only).** When a service calls `/api/connect`,
  the Gateway first probes the live session with the same cheap liveness check
  `/api/status` uses (`_session_answers`,
  [app/routers/legacy_auth.py](Gateway/gateway_backend/app/routers/legacy_auth.py)).
  If the broker is already connected it returns immediately with
  `{"connected": true, "already_connected": true}` and does **not** log in — a
  redundant press is a no-op, not a slow (~180s) forced re-login. A human, by
  contrast, always forces a fresh login (that is how a human recovers a stuck
  same-day session, whose token still reads "issued today").
- **Login errors are visible to the caller.** A wrong password / TOTP is not
  retried (`brokers/shoonya/auth_code.py` raises `LoginRejectedError`); the exact
  broker rejection text rides in the response `detail`, so the calling service's
  UI shows the real reason rather than a generic failure.

Grant `connect` to an existing service **without rotating its secret** — the
scopes column is read fresh from the `Service` row at every token exchange, so
widening a grant needs no new secret and no `.env` change on the service side:

```bash
python -m scripts.register_service --name snowball --add-scope connect
python -m scripts.register_service --name swing --add-scope connect
```

---

## How the `client_id` / `client_secret` get created

You run a script **on the Gateway** that mints a random `client_id` and
`client_secret`, stores only the **bcrypt hash** of the secret in the Gateway's
`services` table, and prints the raw secret **once**. You then copy both values
into Grid's `.env`.

The real script lives at
[scripts/register_service.py](Gateway/gateway_backend/scripts/register_service.py).
Run it from `Gateway/gateway_backend/`:

```bash
# create the grid service identity (default scopes)
python -m scripts.register_service --name grid

# rotate the secret later (keeps the row + scopes)
python -m scripts.register_service --name grid --rotate

# grant an extra scope WITHOUT rotating the secret (no .env change needed)
python -m scripts.register_service --name snowball --add-scope connect
```

It prints:

```
Service 'grid' created. Add these to the service's .env:

  GATEWAY_CLIENT_ID=svc_xxxxxxxxxxxx
  GATEWAY_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

  (scopes granted: orders,positions,funds,market,profile,status)

⚠  The secret is shown only once — it cannot be recovered later.
```

### The script (reference)

> Snapshot for orientation — the live script also supports `--add-scope`
> (see above). Read the file for the authoritative version.

```python
"""Register (or rotate) a machine/service identity that can authenticate to the
Gateway with client_id + client_secret and receive a scoped service JWT.

The raw client_secret is shown ONCE and is unrecoverable afterwards (only its
bcrypt hash is stored) — copy it into the service's .env immediately.

Usage (from gateway_backend/):
    python -m scripts.register_service --name grid
    python -m scripts.register_service --name grid --rotate
"""
import argparse
import secrets
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv

load_dotenv()  # JWT_SECRET must be present before importing security helpers

from app.core.security import ALL_SCOPES, hash_password
from db.engine import SessionLocal, init_db
from db.models import Service

# Default grant for grid_strategy — everything it calls, nothing more.
DEFAULT_SCOPES = "orders,positions,funds,market,profile,status"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help='service name, e.g. "grid"')
    ap.add_argument("--scopes", default=DEFAULT_SCOPES,
                    help=f"comma-separated; valid: {','.join(ALL_SCOPES)}")
    ap.add_argument("--rotate", action="store_true",
                    help="regenerate the secret for an existing service")
    args = ap.parse_args()

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    bad = [s for s in scopes if s not in ALL_SCOPES]
    if bad:
        ap.error(f"unknown scope(s): {', '.join(bad)}. Valid: {', '.join(ALL_SCOPES)}")

    init_db()  # ensure the `services` table exists
    db = SessionLocal()
    try:
        # random secret; only its bcrypt hash is persisted
        client_secret = secrets.token_urlsafe(32)
        service = db.query(Service).filter_by(name=args.name).first()

        if service is None:
            client_id = f"svc_{secrets.token_urlsafe(12)}"
            service = Service(
                name=args.name,
                client_id=client_id,
                client_secret_hash=hash_password(client_secret),
                scopes=",".join(scopes),
                is_active=True,
            )
            db.add(service)
            action = "created"
        else:
            if not args.rotate:
                ap.error(f"service '{args.name}' already exists. Use --rotate to issue a new secret.")
            service.client_secret_hash = hash_password(client_secret)
            if args.scopes != DEFAULT_SCOPES or not service.scopes:
                service.scopes = ",".join(scopes)
            service.is_active = True
            action = "rotated"

        db.commit()
        db.refresh(service)
    finally:
        db.close()

    print(f"\nService '{service.name}' {action}. Add these to the service's .env:\n")
    print(f"  GATEWAY_CLIENT_ID={service.client_id}")
    print(f"  GATEWAY_CLIENT_SECRET={client_secret}")
    print(f"\n  (scopes granted: {service.scopes})")
    print("\n⚠  The secret is shown only once — it cannot be recovered later.\n")


if __name__ == "__main__":
    main()
```

---

## Client `.env` files

Each Gateway-client service needs the same three pieces of config (shared JWT
secret, its own client credentials, where to find the Gateway) — but the env
var **names differ per service**. Don't assume one service's names when
configuring another.

### Grid

```dotenv
# --- issued by the Gateway's register_service.py (Flow 2) ---
GATEWAY_CLIENT_ID=svc_xxxxxxxxxxxx
GATEWAY_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# --- MUST be byte-identical to the Gateway's JWT_SECRET ---
# lets Grid verify human user JWTs locally (Flow 1)
JWT_SECRET=<same value as Gateway>

# where Grid reaches the Gateway
GATEWAY_BASE_URL=http://localhost:8000
GATEWAY_WS_URL=ws://localhost:8000/api/ws/ticker
```

Loaded by [config/settings.py](grid_strategy/backend/config/settings.py).

### reversal_strategy

Same values, but the Gateway-related vars are prefixed `SWING_` to avoid
collisions if it ever runs alongside another Gateway client on the same box.
`JWT_SECRET` stays unprefixed since it must be byte-identical to the Gateway's.

```dotenv
# --- issued by the Gateway's register_service.py (Flow 2) ---
SWING_GATEWAY_CLIENT_ID=svc_xxxxxxxxxxxx
SWING_GATEWAY_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# --- MUST be byte-identical to the Gateway's JWT_SECRET ---
# lets reversal_strategy verify human user JWTs locally (Flow 1)
JWT_SECRET=<same value as Gateway>

# where reversal_strategy reaches the Gateway
SWING_GATEWAY_BASE_URL=http://localhost:8000
SWING_GATEWAY_WS_URL=ws://localhost:8000/api/ws/ticker
```

Loaded by [app/brokers/shoonya_gateway.py](reversal_strategy/backend/app/brokers/shoonya_gateway.py)
(base URL / WS URL / client id / secret) and
[app/core/sso.py](reversal_strategy/backend/app/core/sso.py) (`JWT_SECRET`).

> **Key point:** `JWT_SECRET` is the *shared* secret — same value across the
> Gateway and every client service. `GATEWAY_CLIENT_ID` / `GATEWAY_CLIENT_SECRET`
> (or their `SWING_`-prefixed equivalents) are *per-service* credentials, unique
> to each service and generated by the script above.

---

## Security notes

- The raw `client_secret` is **never stored** — only `bcrypt(client_secret)` in
  `services.client_secret_hash`. Losing it means rotating (`--rotate`).
- Service tokens are short-lived (1h) and **scoped**; the engine refreshes them
  automatically. A leaked service token expires fast and can only do what its
  scopes allow.
- `JWT_SECRET` is the crown jewel: anyone with it can forge either token type.
  Keep both `.env` files out of git (they are `.gitignore`d) and rotate the shared
  secret if it ever leaks — that invalidates *all* existing tokens on both sides.
