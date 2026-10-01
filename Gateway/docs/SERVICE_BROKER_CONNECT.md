# Broker Connect via the Gateway (services + humans)

> How the **Connect / Disconnect** controls in the client dashboards
> (Grid, reversal_strategy) actually log the broker in — and why the
> Gateway, not the service, is the only thing that ever touches Shoonya
> credentials. Read this before changing `/api/connect`, `/api/disconnect`, the
> `connect` scope, or any service's Connect button.
>
> Companion docs: [AUTH_PIPELINE.md](../../AUTH_PIPELINE.md) (how service/user
> JWTs are issued) — this doc assumes you've read its "two identities" model.

## TL;DR

- The **Gateway is the only holder of broker credentials.** It runs one shared
  broker session (`_auth_module._auth`). Everything else is a client of it.
- A service **cannot** connect just by holding a normal token — broker
  login/logout is gated by a dedicated **`connect`** scope.
- A service's Connect button does **not** log in locally. It calls the service's
  own backend, which relays to the Gateway's `/api/connect` using the service
  JWT. The Gateway does the real Shoonya login.
- A redundant service connect is a **no-op** (`already_connected`) instead of a
  slow forced re-login; a human always forces a fresh login.
- The broker session is **shared** — a disconnect from any dashboard logs
  Shoonya out for *every* strategy on the Gateway.

---

## The `connect` scope

`ALL_SCOPES` in [app/core/security.py](../gateway_backend/app/core/security.py)
includes `connect`. It gates exactly two routes:

| Route | Purpose | Auth |
|---|---|---|
| `POST /api/connect` | Log the broker in | `require_scope("connect", user_needs_form=True)` |
| `POST /api/disconnect` | Log the broker out | `require_scope("connect", user_needs_form=False)` |

A **human user** implicitly holds every scope, so the human Connect flow is
unchanged. A **service** passes only if its token carries `connect`. Both routes
live in [app/routers/legacy_auth.py](../gateway_backend/app/routers/legacy_auth.py).

There is no separate `disconnect` scope on purpose: a service trusted to bring
the shared session up is trusted to take it down.

---

## The call path

```
Service dashboard (Grid / Swing UI)
   │  click Connect
   ▼
Service backend  (its OWN /api/broker/connect route)
   │  POST /api/connect   with  Authorization: Bearer <service JWT>
   ▼
Gateway  (POST /api/connect, legacy_auth.py)
   │  scope check → (service) idempotency guard → broker.login(credentials)
   │  sets the shared _auth singleton, starts the ticker
   ▼
Shoonya  (headless OAuth login, ~seconds to ~180s)
```

Per service:

- **reversal_strategy**: `TopBar.tsx` → `/api/broker/connect`
  ([app/routers/account.py](../../reversal_strategy/backend/app/routers/account.py))
  → `gateway_service.connect_force()` →
  [shoonya_gateway.py](../../reversal_strategy/backend/app/brokers/shoonya_gateway.py)
  `connect_force()` which POSTs `/api/connect` through its `_AuthedSession`
  (attaches the service JWT, retries once on a token-expiry 401).
- **Grid**: `App.tsx` → `/api/broker/connect`
  ([api/routes.py](../../grid_strategy/backend/api/routes.py)) →
  [gateway_client/rest.py](../../grid_strategy/backend/gateway_client/rest.py)
  `connect_broker()` which POSTs `/api/connect` through its `_AuthedClient`
  (same JWT-attach + 401-retry behaviour).

Neither service ever sees a broker credential — those stay in the Gateway's
encrypted `Account.credentials_enc`.

---

## Human vs service behaviour on `/api/connect`

The route branches on `principal.kind`:

### Human user
Resolves the account by `principal.user.id` and **always forces a fresh login**.
This is deliberate: Shoonya's token still reads "issued today" even after the
broker has killed the session mid-day (e.g. the user logged into the Shoonya
app), so only a forced re-login replaces a stuck session — a plain status check
cannot.

### Service (the idempotency guard)
Before doing anything, the service branch probes the live session with the
**same cheap check `/api/status` uses** (`_session_answers`, a 45 s cached
`get_user_details` call). If the broker is genuinely connected it returns
immediately:

```json
{ "connected": true, "already_connected": true, "method": "rest",
  "message": "Broker already connected — no action taken." }
```

No login runs. This is what stops a redundant service Connect from burning a
slow (~180 s, Selenium) re-login on a healthy session. Only when the guard says
the session is actually down does the service branch fall through to the shared
account and log in.

> A service token carries no human user, so it can't scope by `user_id`. The
> live engine trades a **single** broker account (orders/positions all ride the
> one shared `_auth` singleton), so the service branch uses that sole account
> (`db.query(Account).first()`).

---

## Login errors are visible to the caller

A wrong password or wrong TOTP is **not retried** — the headless login raises
`LoginRejectedError` ([brokers/shoonya/auth_code.py](../gateway_backend/brokers/shoonya/auth_code.py)),
which becomes a `BrokerError` → `HTTPException(401, detail=<exact broker text>)`.
That `detail` rides all the way back through each service's `connect` client
into the dashboard, so the operator sees the real reason ("Invalid Input", bad
OTP, etc.) rather than a generic failure. This falls out for free — no
per-service error mapping needed.

---

## Cross-service state propagation (the liveness write-through)

Services learn broker state only by **polling `/api/status`**, whose answer is
cached for `_LIVENESS_TTL = 45 s`. Without help, a connect done in one dashboard
could stay invisible to the others for up to 45 s.

So on a connect/disconnect the Gateway *just performed*, it overwrites that cache
with the known truth — `_mark_session_liveness(True/False)` in
[legacy_auth.py](../gateway_backend/app/routers/legacy_auth.py). The next poll
from any other service is then correct at once, **without** shortening the TTL
(which would re-probe the broker far more often). Result: cross-service lag drops
from ~45 s to the other service's next poll (~1–15 s).

This is not instant/push. Genuinely sub-second cross-service updates would
require the Gateway to *push* connection changes over the ticker WebSocket
instead of everyone polling — a larger change, not currently done.

---

## Disconnect is a shared action

`/api/disconnect` stops the ticker and drops the shared `_auth` singleton, so it
logs Shoonya out for **every** strategy on the Gateway. Both dashboards therefore
gate it behind a confirmation:

- **Swing**: inline Confirm/No in `TopBar.tsx`.
- **Grid**: inline Confirm/No in `App.tsx` (a permanent Connect/Disconnect
  pair in the navbar).

While disconnected, prices stop and stop-losses are not monitored anywhere until
someone reconnects.

---

## Granting the `connect` scope (no secret rotation)

Scopes are stored on the `Service` row and read **fresh at every token
exchange** — they are independent of the client secret. So widening a service's
grant needs no new secret and no `.env` change on the service side:

```bash
# from Gateway/gateway_backend/
python -m scripts.register_service --name snowball --add-scope connect
python -m scripts.register_service --name swingBottom --add-scope connect
```

`--add-scope` (in
[scripts/register_service.py](../gateway_backend/scripts/register_service.py))
updates only the `scopes` column, leaving `client_secret_hash` untouched.
`--rotate`, by contrast, reissues the secret and *would* require a `.env` update.

---

## Gotchas

- **Scopes are baked into the JWT at issue time.** `require_scope` checks the
  *token's* scopes, not the live DB row. So after granting `connect`, a service
  still holding a token minted **before** the grant gets a **403** — and a 403
  does **not** trigger the client's re-login (only specific 401 hints do). Fix:
  **restart the service** so it fetches a fresh token (or wait out the ~1 h TTL).
- **The service `.env` client_id must match a service row that has `connect`.**
  Verify with a read-only query on the `services` table; the running process may
  be authenticating as a different service than you expect.
- **`/api/status` still reflects a 45 s liveness cache** except right after a
  connect/disconnect we performed (see the write-through above).
- **This is Gateway backend code** — changes to the connect/disconnect flow take
  effect when the **Gateway** restarts, not when a service restarts. (A service
  restart only refreshes *its* token / picks up *its own* code.)

---

## Endpoints reference

| Method | Endpoint | Scope | Notes |
|---|---|---|---|
| POST | `/api/connect` | `connect` | Human: always fresh login. Service: guard → login. |
| POST | `/api/disconnect` | `connect` | Shared teardown; also marks liveness `False`. |
| GET | `/api/status` | `status` | Cached liveness (45 s), write-through on connect/disconnect. |

Service-side wrappers that call the above:

| Service | UI | Backend route | Gateway client |
|---|---|---|---|
| Swing | `TopBar.tsx` | `/api/broker/connect`, `/api/broker/disconnect` | `shoonya_gateway.py` (`_AuthedSession`) |
| Grid | `App.tsx` | `/api/broker/connect`, `/api/broker/disconnect` | `gateway_client/rest.py` (`_AuthedClient`) |
