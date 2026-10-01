import os
from dotenv import load_dotenv
from sqlalchemy import String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Nothing else in the app's import chain (app/main.py included) loads .env —
# without this, DATABASE_URL is never actually read from it, and every DB
# call here silently falls back to a local sqlite file instead of the shared
# database, regardless of what's configured in .env.
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gateway.db")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # MySQL/Postgres/etc: pre_ping + recycle so a long-idle pooled connection
    # doesn't surface as "server has gone away"; check_same_thread is a
    # SQLite-only connect arg and must not be passed here.
    #
    # pool_size/max_overflow are sized for a remote DB (~35ms RTT), not the
    # old local SQLite file: several browser tabs each polling /api/state,
    # /api/funds, /api/positions, /api/orders concurrently — plus a broker
    # login doing its own sequential session-cache round trips in a worker
    # thread — can hold several connections checked out at once. The old
    # defaults (5 + 10 overflow) queued requests behind each other, which
    # read as "login/page refresh is slow" once every query carries network
    # latency instead of being effectively free.
    engine = create_engine(
        DATABASE_URL, pool_pre_ping=True, pool_recycle=3600,
        pool_size=20, max_overflow=40, pool_timeout=30,
    )

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Sessions commit from both the event loop and executor threads. WAL
        # lets readers coexist with the writer; busy_timeout makes brief
        # write collisions wait instead of raising "database is locked".
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from db import models  # noqa: F401 — registers all ORM classes with Base

    if not IS_SQLITE:
        _apply_varchar_lengths()
    Base.metadata.create_all(bind=engine)
    # create_all only creates missing tables — it never alters existing ones —
    # so columns added after a DB (SQLite or MySQL) was first created need this
    # additive pass too.
    _apply_light_migrations()
    db = SessionLocal()
    try:
        _seed(db)
    finally:
        db.close()


def _apply_varchar_lengths(default: int = 255) -> None:
    """MySQL (unlike SQLite) requires a length on every VARCHAR. The models
    declare bare `String`, so give every unbounded String column a default
    length before create_all. `type(col.type) is String` is an exact-type
    check that deliberately excludes Text (a String subclass) so large text
    fields stay TEXT rather than being capped to VARCHAR(default)."""
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if type(col.type) is String and col.type.length is None:
                col.type.length = default


def _apply_light_migrations() -> None:
    """Add columns that were introduced after the initial schema shipped.

    `create_all` only creates missing tables — it never alters existing ones —
    so ADD COLUMN has to be issued manually. Existing columns are checked via
    the Inspector rather than by swallowing a "duplicate column" error: on
    Postgres any failed statement aborts the whole transaction, so one
    already-present column would silently roll back every addition after it.
    Booleans use TRUE/FALSE defaults — Postgres rejects `BOOLEAN DEFAULT 0`.
    """
    from sqlalchemy import inspect as sa_inspect, text

    additions = [
        ("order_lots", "target_enabled",      "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("order_lots", "target_value",        "FLOAT   NOT NULL DEFAULT 0.0"),
        ("order_lots", "persistent_order_id", "INTEGER"),
        ("order_lots", "is_external",         "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("order_lots", "description",         "TEXT    NOT NULL DEFAULT ''"),
        ("persistent_orders", "description",  "TEXT    NOT NULL DEFAULT ''"),
        ("order_lots", "carried_pnl",         "FLOAT   NOT NULL DEFAULT 0.0"),
        ("rollover_intents", "acknowledged",  "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("rollover_intents", "carry_pnl",     "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("symbol_targets", "carried_pnl",     "FLOAT   NOT NULL DEFAULT 0.0"),
        ("order_lots", "is_rollover",         "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("accounts",   "user_id",             "INTEGER"),
        ("order_lots", "is_reentry",          "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("order_lots", "is_temp_exit",        "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("order_lots", "reentry_source_lot_id", "INTEGER"),
        ("order_lots", "source_service",      "VARCHAR(255)"),
        ("symbol_targets",   "owner_uid",     "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("exchange_targets", "owner_uid",     "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("watchlist_entries", "owner_uid",    "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("order_lots", "strategy_name",       "VARCHAR(255)"),
    ]
    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    cols = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    with engine.begin() as conn:
        for table, col, coltype in additions:
            if table in tables and col not in cols[table]:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))
                cols[table].add(col)
        # per_lot_value → target_value: symbol target switched from a
        # per-lot-scaled threshold to a flat total-P&L threshold.
        st = cols.get("symbol_targets", set())
        if "per_lot_value" in st and "target_value" not in st:
            conn.execute(text('ALTER TABLE symbol_targets RENAME COLUMN per_lot_value TO target_value'))


def _seed(db) -> None:
    from db.models import Broker

    _ensure(db, Broker, name="shoonya")

    db.commit()


def _ensure(db, model, defaults: dict | None = None, **lookup):
    """Insert a row if it doesn't already exist. Matches on lookup kwargs."""
    obj = db.query(model).filter_by(**lookup).first()
    if obj is None:
        kwargs = {**lookup, **(defaults or {})}
        db.add(model(**kwargs))
