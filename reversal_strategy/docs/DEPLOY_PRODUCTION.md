# Production deployment — client MySQL

Two independent stacks. The test stack does not change and is not a dependency
of the production one.

| | Test (ours) | Production (client, real money) |
|---|---|---|
| AmiBroker | our box | client's server, 192.168.133.51 |
| Backend | our server, port 8020 | client's production server, port 8020 |
| Database | SQLite `swing_swager.db` | MySQL `Trading_bots` @ 129.151.44.44 |
| Env file | `.env` (unchanged) | `.env` copied from `.env.production` |

Nothing is shared between them — not the database, not the broker gateway. Our
server going down cannot affect the client's trading.

Signals reach production through the DATABASE, not HTTP: AmiBroker INSERTs into
`ami_signal_inbox` and the backend polls it (`SWING_SIGNAL_SOURCE=db`). The
AmiBroker box never needs to reach the backend, and vice versa.

---

## Step 1 — database: DONE

Both bots share `Trading_bots`. No second schema was needed.

`Trading_bots` belongs to the ladder bot and already had a `signals` table of an
incompatible shape:

| | ladder `signals` (10 cols) | swing `Signal` (26 cols) |
|---|---|---|
| symbol | `sym` | `symbol` |
| direction | `action` | `signal_type` |
| time | `ts` | `signal_time`, `created_at` |
| rest | `payload` JSON, `reason`, `lot_id` | `candle_id`, `atr`, `resistance`, `support`, `target`, `stop_loss`, `signal_rr`, ... |

Resolved by renaming **our** table to `swing_signals` (`db/models.py`). That was
the only clash — the ladder bot's other nine tables share no name with anything
this backend creates.

`db/engine.py::_rename_legacy_signals()` handles deployments that predate the
rename: it renames an existing `signals` table to `swing_signals` so history
survives. It only ever touches a table carrying a `candle_id` column, which is
ours — the ladder bot's `signals` has no such column and is left alone. Both
cases are covered by test.

**Verified live** against 129.151.44.44 on 2026-08-11: our 8 tables
(`swing_signals`, `positions`, `orders`, `strategy_config`, `blacklist`,
`rollovers`, `audit_log`, `ami_signal_inbox`) were created in `Trading_bots`,
the ladder bot's 10 tables were untouched, and its `signals` columns were
confirmed identical afterwards. The full pipeline ran end to end and the test
rows were removed.

---


## Step 2 — production server

```bash
git clone <repo> swing && cd swing/gateway_backend
pipx install poetry              # once per server, if not already installed
poetry install --without dev     # creates ./.venv from poetry.lock
cp .env.production .env
```

Then fill the blank fields in `.env`: `ALLOWED_ORIGINS`,
`SWING_GATEWAY_BASE_URL`, `SWING_GATEWAY_WS_URL`, `SWING_GATEWAY_SECRET`, and a
**freshly generated** `AMI_WEBHOOK_SECRET` (do not reuse the test server's).

`SWING_DB_URL` is already filled in. Its password is URL-encoded and must stay
that way — `;` `%` `<` `$` become `%3B` `%25` `%3C` `%24`. Left raw, the bare
`%d` is parsed as a percent-escape and the login fails with a misleading
"access denied".

## Step 3 — verify before starting

```bash
cd gateway_backend
poetry run python -c "
from db.engine import _engine, IS_SQLITE
from sqlalchemy import inspect
assert not IS_SQLITE, 'still on SQLite — .env not loaded'
print('connected:', _engine.url.render_as_string(hide_password=True))
print('tables   :', sorted(inspect(_engine).get_table_names()))
"
```

Expect `IS_SQLITE=False`. If it prints a SQLite URL, `.env` was not picked up —
stop and fix that first, or the first boot writes to a local file and the
client sees an empty dashboard while AmiBroker fills a table nobody reads.

The 18 tables should already be listed: the ladder bot's 10, plus our 8
(`swing_signals`, `positions`, `orders`, `strategy_config`, `blacklist`,
`rollovers`, `audit_log`, `ami_signal_inbox`) created during the live
verification. `init_db()` is idempotent, so booting re-creates nothing.

Then start normally (`./run.sh`, or uvicorn on port 8020).

## Step 4 — production AmiBroker bridge

On the client's AmiBroker server (192.168.133.51), `scan_bridge_sql.py` writes
straight to MySQL — there is no webhook and no `--url`:

```bat
python scan_bridge_sql.py --dir C:\swing ^
  --host 129.151.44.44 --user tradingbots ^
  --password "<pw>" --database Trading_bots
```

`run_bridge_sql.bat` wraps this with preflight checks. `swing_scan.afl` writes
`scan_60.csv` / `scan_240.csv` / `scan_1440.csv` into `C:\swing` exactly as on
the test box. Full setup: `integration/amibroker/README_PRODUCTION.md`.

Confirm outbound 3306 from that box before anything else:

```bat
python -c "import socket;s=socket.create_connection(('129.151.44.44',3306),5);print('OK');s.close()"
```

---

## Operational notes

**Backups.** `_backup_db()` copies the SQLite file, which does not exist on
MySQL. It now logs `DB_BACKUP … skipped — server-hosted DB` instead of
silently doing nothing. **Schedule `mysqldump` or snapshots on the database
server** — the application does not back itself up in production.

**Two grants worth cleaning up** on the client's server, both pre-existing:

```sql
-- dead grant, database name is a typo ("Tading_bots"), no such DB exists
REVOKE ALL PRIVILEGES ON `Tading\_bots`.* FROM 'tradingbots'@'%';

-- an application login should not be able to hand its rights to other users
REVOKE GRANT OPTION ON `Trading_bots`.* FROM 'tradingbots'@'%';
```

**Rotate the MySQL password.** It was shared over WhatsApp, so treat it as
compromised before real money runs through it. Re-encode the new one with
`python -c "from urllib.parse import quote_plus; print(quote_plus('<pw>'))"`.

**No test suite.** `pytest` collects zero tests in this project. The
verification in Step 3 is the only automated check on the DB wiring.

**Both bots write to one database.** Ours is `swing_signals` / `ami_signal_inbox`;
the ladder bot owns `signals`, `instruments`, `lots`, `ladder_levels`,
`scan_signals`, `rollover_events`, `logs`, `econ_events`, `engine_state`,
`user_settings`. Never point a migration or a cleanup script at the whole
schema.
