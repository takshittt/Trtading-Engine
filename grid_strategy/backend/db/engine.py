"""DB engine: SQLite (WAL mode + a BOUNDED busy timeout) or MySQL/Postgres.

The engine is async but SQLAlchemy/SQLite are synchronous, so every commit runs
ON the event loop. That is tolerable only while commits are fast: whatever a
blocked write waits for, the tick loop, exit checks and heartbeat wait for too.
The old 15-second busy timeout therefore turned rare write contention into a
15-second freeze of the entire trading engine — worst case precisely during
volatility, when contention (reconciler + tick exits + logging) and the cost of
being blind are both at their peak.

Two things bound that now (SQLite only — MySQL/Postgres use the pool below):

  * `busy_timeout` is 4s, not 15s. In WAL mode with one writer process and short
    transactions, real contention resolves in milliseconds; 4s is still ~1000×
    headroom for a slow fsync while capping the worst-case stall at 4s.
  * the event log — by far the chattiest writer — no longer commits inline at
    all; it queues to a background thread (see core/event_log.py).
"""

from sqlalchemy import String, create_engine, event, inspect as sa_inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.schema import CreateColumn

from config.settings import settings

IS_SQLITE = settings.database_url.startswith("sqlite")
_BUSY_TIMEOUT_MS = 4000

if IS_SQLITE:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False, "timeout": _BUSY_TIMEOUT_MS / 1000.0},
    )
else:
    # MySQL/Postgres/etc: pre_ping + recycle so a long-idle pooled connection
    # doesn't surface as "server has gone away"; the SQLite-only connect args
    # (check_same_thread / timeout) must not be passed here.
    #
    # pool_size/max_overflow are sized for the off-site DB (~35ms RTT), not
    # SQLAlchemy's local-DB defaults (5 + 10 overflow = 15 total). Most routes
    # in api/routes.py are plain `def` on purpose (see the note there) so they
    # run concurrently in the thread pool — several dashboard panels each
    # polling their own endpoint every second can hold several connections
    # checked out at once even though the tables themselves are tiny, and the
    # old defaults made the rest queue for a free connection behind them.
    engine = create_engine(
        settings.database_url, pool_pre_ping=True, pool_recycle=3600,
        pool_size=20, max_overflow=40, pool_timeout=30,
    )


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")       # readers never block the writer
        cur.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")   # bounded wait, then raise
        cur.execute("PRAGMA synchronous=NORMAL")
        # checkpoint less eagerly: the default (1000 pages) makes a writer stop to
        # fold the WAL back mid-session. Larger batches keep the trade path snappy.
        cur.execute("PRAGMA wal_autocheckpoint=4000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_session():
    """Plain context-manager-free session for engine internals."""
    return SessionLocal()


def _table_columns(conn, table: str) -> list[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _add_missing_columns() -> None:
    """Dialect-agnostic safety net: any column defined on an ORM model but
    absent from an already-existing table gets ALTER TABLE ADD COLUMN'd.

    `create_all()` only creates missing TABLES — it never alters one that
    already exists — so a column added to models.py after a database was
    first provisioned silently never reaches it. `_migrate_existing_tables()`
    covers this for SQLite with an explicit per-column list; this covers the
    same class of bug generically, for SQLite AND MySQL/Postgres, so a new
    column can never again go live on one engine and 500 on the other.

    Uses SQLAlchemy's Inspector (works across dialects) to diff model columns
    against real ones, and compiles each missing column's DDL against the
    engine's own dialect. Columns with a static Python-side `default=` are
    backfilled on existing rows (NULL otherwise); constrained columns
    (FK/unique/PK) aren't expected here — those go through create_all on a
    fresh table instead.
    """
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table — create_all() below builds it whole
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                ddl = CreateColumn(col).compile(dialect=engine.dialect)
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
                default = col.default.arg if col.default is not None else None
                if default is not None and not callable(default):
                    conn.execute(
                        text(f"UPDATE {table.name} SET {col.name} = :v WHERE {col.name} IS NULL"),
                        {"v": default})


def _migrate_existing_tables() -> None:
    """Additive, framework-free migration for DBs created before the manual
    ladder feature. `create_all` never alters existing tables, so:

    - instruments: needs new columns AND a relaxed unique constraint
      (exch,sym) → (exch,sym,mode), which in SQLite means a table rebuild.
    - lots: plain ADD COLUMN source.
    """
    with engine.begin() as conn:
        tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "instruments" in tables and "mode" not in _table_columns(conn, "instruments"):
            # legacy rename: do NOT rewrite lots/rollover_events FK clauses to
            # point at instruments_old (we drop it right after the copy)
            conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            conn.exec_driver_sql("ALTER TABLE instruments RENAME TO instruments_old")
            conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")

        if "lots" in tables and "source" not in _table_columns(conn, "lots"):
            conn.exec_driver_sql(
                "ALTER TABLE lots ADD COLUMN source VARCHAR DEFAULT 'automated'")

        if "lots" in tables and "product_type" not in _table_columns(conn, "lots"):
            conn.exec_driver_sql(
                "ALTER TABLE lots ADD COLUMN product_type VARCHAR DEFAULT 'M'")

        if "lots" in tables:
            lot_cols = _table_columns(conn, "lots")
            for col, ddl in (
                ("temp_exit_price", "ALTER TABLE lots ADD COLUMN temp_exit_price FLOAT DEFAULT 0"),
                ("temp_exit_loss", "ALTER TABLE lots ADD COLUMN temp_exit_loss FLOAT DEFAULT 0"),
                ("temp_exit_time", "ALTER TABLE lots ADD COLUMN temp_exit_time DATETIME"),
                ("temp_exit_trigger_price", "ALTER TABLE lots ADD COLUMN temp_exit_trigger_price FLOAT DEFAULT 0"),
                ("reenter_trigger_price", "ALTER TABLE lots ADD COLUMN reenter_trigger_price FLOAT DEFAULT 0"),
                ("carry_recovery", "ALTER TABLE lots ADD COLUMN carry_recovery FLOAT DEFAULT 0"),
                ("target_order_id", "ALTER TABLE lots ADD COLUMN target_order_id VARCHAR DEFAULT ''"),
                ("target_override", "ALTER TABLE lots ADD COLUMN target_override FLOAT"),
                ("sl_override", "ALTER TABLE lots ADD COLUMN sl_override FLOAT"),
                ("filled_adopted", "ALTER TABLE lots ADD COLUMN filled_adopted INTEGER DEFAULT 0"),
            ):
                if col not in lot_cols:
                    conn.exec_driver_sql(ddl)

        # additive columns for options support + per-instrument product type.
        # (skip while an instruments_old rebuild is pending — create_all builds
        # the fresh table with these columns already, and the copy backfills.)
        if "instruments" in tables and "instruments_old" not in tables:
            inst_cols = _table_columns(conn, "instruments")
            for col, ddl in (
                ("instr_type",   "ALTER TABLE instruments ADD COLUMN instr_type VARCHAR DEFAULT 'FUT'"),
                ("opt_type",     "ALTER TABLE instruments ADD COLUMN opt_type VARCHAR DEFAULT ''"),
                ("strike",       "ALTER TABLE instruments ADD COLUMN strike FLOAT DEFAULT 0"),
                ("underlying",   "ALTER TABLE instruments ADD COLUMN underlying VARCHAR DEFAULT ''"),
                ("product_type", "ALTER TABLE instruments ADD COLUMN product_type VARCHAR DEFAULT 'M'"),
                ("buy_order_type", "ALTER TABLE instruments ADD COLUMN buy_order_type VARCHAR DEFAULT 'LMT'"),
                ("sell_order_type", "ALTER TABLE instruments ADD COLUMN sell_order_type VARCHAR DEFAULT 'LMT'"),
                ("ladder_rearm", "ALTER TABLE instruments ADD COLUMN ladder_rearm BOOLEAN DEFAULT 1"),
                ("target_chain_pct", "ALTER TABLE instruments ADD COLUMN target_chain_pct FLOAT DEFAULT 100"),
                ("min_gap_points", "ALTER TABLE instruments ADD COLUMN min_gap_points FLOAT DEFAULT 0"),
                ("max_spread_points", "ALTER TABLE instruments ADD COLUMN max_spread_points FLOAT DEFAULT 0"),
                ("rollover_date_override", "ALTER TABLE instruments ADD COLUMN rollover_date_override VARCHAR DEFAULT ''"),
                ("fallback_symbol", "ALTER TABLE instruments ADD COLUMN fallback_symbol VARCHAR DEFAULT ''"),
            ):
                if col not in inst_cols:
                    conn.exec_driver_sql(ddl)

        if "ladder_levels" in tables and "target_override" not in _table_columns(conn, "ladder_levels"):
            conn.exec_driver_sql(
                "ALTER TABLE ladder_levels ADD COLUMN target_override FLOAT")

        if "ladder_levels" in tables and "fire_count" not in _table_columns(conn, "ladder_levels"):
            conn.exec_driver_sql(
                "ALTER TABLE ladder_levels ADD COLUMN fire_count INTEGER DEFAULT 0")

        if "ladder_levels" in tables and "sl_override" not in _table_columns(conn, "ladder_levels"):
            conn.exec_driver_sql(
                "ALTER TABLE ladder_levels ADD COLUMN sl_override FLOAT")

        if "ladder_levels" in tables and "shift_applied" not in _table_columns(conn, "ladder_levels"):
            conn.exec_driver_sql(
                "ALTER TABLE ladder_levels ADD COLUMN shift_applied BOOLEAN DEFAULT 0")


def _finish_instruments_rebuild() -> None:
    """After create_all built the new-schema instruments table, copy the old
    rows across (shared columns only) and backfill the new columns."""
    with engine.begin() as conn:
        tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "instruments_old" not in tables:
            return
        old_cols = _table_columns(conn, "instruments_old")
        new_cols = _table_columns(conn, "instruments")
        shared = [c for c in old_cols if c in new_cols]
        conn.exec_driver_sql(
            f"INSERT INTO instruments ({', '.join(shared)}, mode) "
            f"SELECT {', '.join(shared)}, 'auto' FROM instruments_old")
        for col, default in (
            ("ladder_basis", "'fixed'"), ("ladder_anchor_price", "0"),
            ("ladder_interval_points", "0"), ("ladder_num_levels", "5"),
            ("ladder_sr_lookback_days", "45"), ("ladder_armed", "0"),
            ("ladder_rearm", "1"), ("buy_order_type", "'LMT'"), ("sell_order_type", "'LMT'"),
            ("target_chain_pct", "100"),
        ):
            conn.exec_driver_sql(
                f"UPDATE instruments SET {col} = {default} WHERE {col} IS NULL")
        conn.exec_driver_sql("DROP TABLE instruments_old")


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


def init_db() -> None:
    from db import models  # noqa: F401 — registers ORM classes

    if IS_SQLITE:
        # PRAGMA/sqlite_master-based additive migrations for pre-existing SQLite
        # DBs, plus the generic column-diff safety net below.
        _migrate_existing_tables()
        _add_missing_columns()
        Base.metadata.create_all(bind=engine)
        _finish_instruments_rebuild()
    else:
        _apply_varchar_lengths()
        _add_missing_columns()
        Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        from db.models import EngineState, UserSettings
        if db.get(EngineState, 1) is None:
            db.add(EngineState(id=1, engine_on=False))
            db.commit()
        if db.get(UserSettings, 1) is None:
            db.add(UserSettings(id=1))
            db.commit()
