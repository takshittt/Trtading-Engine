"""Database engine + session helpers and one-time seeding of StrategyConfig."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import DATABASE_URL, DEFAULTS
from db.models import Base, StrategyConfig

_log = logging.getLogger("swing.db")

# SQLite by default (isolated local file); MySQL/Postgres when SWING_DB_URL is a
# server URL. The two need different engine args — check_same_thread is a
# SQLite-only connect arg and pooling only applies off SQLite.
IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    _engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Commits fire from both the event loop and executor threads (reconciler,
        # tick exits, audit log). WAL lets readers coexist with the writer; a
        # bounded busy_timeout makes brief write collisions wait a moment instead
        # of raising "database is locked", without freezing the tick path for long.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=4000")
        cur.close()
else:
    # MySQL/Postgres: pre_ping + recycle so a long-idle pooled connection doesn't
    # surface as "server has gone away". No SQLite-only connect args here.
    _engine = create_engine(
        DATABASE_URL, pool_pre_ping=True, pool_recycle=3600, future=True,
    )

SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def _sql_default(col) -> str | None:
    """Literal for a column's Python-side default, or None if it has none."""
    if col.default is None or col.default.is_callable or col.default.is_sequence:
        return None
    val = col.default.arg
    if isinstance(val, bool):
        # TRUE/FALSE, not 1/0: Postgres rejects an integer default on BOOLEAN
        # (SQLite and MySQL accept both spellings).
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    return None


def _migrate() -> None:
    """Add model columns that the existing SQLite file doesn't have yet.

    There is no migration tool here, and `create_all` only ever creates missing
    *tables* — it never alters one that already exists. Without this, any column
    added to a model would raise OperationalError on the first query against a
    database that predates it. Every column added post-v1 is nullable or has a
    scalar default, which is exactly what SQLite's ADD COLUMN accepts.

    The DDL is compiled for the active dialect, so it works on MySQL too. Each
    ALTER runs in its own transaction and is guarded: a single column MySQL
    refuses (e.g. a DEFAULT on a TEXT type) is logged and skipped rather than
    aborting startup for every other column.
    """
    insp = inspect(_engine)
    tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue                        # create_all just made it — already current
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = (f"ALTER TABLE {table.name} ADD COLUMN "
                   f"{col.name} {col.type.compile(_engine.dialect)}")
            default = _sql_default(col)
            if default is not None:
                ddl += f" DEFAULT {default}"
            try:
                with _engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception as exc:
                _log.warning("skipped ADD COLUMN %s.%s: %s", table.name, col.name, exc)


def _rename_legacy_signals() -> None:
    """Rename an existing `signals` table to `swing_signals` — but only ours.

    The Signal model used to be called `signals`. Renaming it in the model alone
    would leave every existing deployment's history stranded in an orphaned
    table and start again from empty, so the rename is applied to the database
    too, before create_all can make a second, empty one.

    The `candle_id` check is the whole safety of this function. On the shared
    MySQL server another bot owns a table called `signals` with an unrelated
    shape (sym / action / payload). Renaming that would break the other system
    and steal its data. `candle_id` exists only in ours, so a table without it
    is not ours and is left untouched.
    """
    insp = inspect(_engine)
    names = set(insp.get_table_names())
    if "signals" not in names or "swing_signals" in names:
        return
    cols = {c["name"] for c in insp.get_columns("signals")}
    if not {"candle_id", "signal_type"} <= cols:
        return                      # someone else's `signals` — hands off
    quote = _engine.dialect.identifier_preparer.quote
    with _engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {quote('signals')} "
                          f"RENAME TO {quote('swing_signals')}"))


def init_db() -> None:
    _rename_legacy_signals()    # must run BEFORE create_all sees a missing table
    Base.metadata.create_all(_engine)
    _migrate()
    with SessionLocal() as db:
        if db.get(StrategyConfig, 1) is None:
            db.add(StrategyConfig(id=1, **DEFAULTS))
            db.commit()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session() -> Session:
    """Direct session for background tasks (caller must close)."""
    return SessionLocal()


def get_config(db: Session) -> StrategyConfig:
    cfg = db.get(StrategyConfig, 1)
    if cfg is None:
        cfg = StrategyConfig(id=1, **DEFAULTS)
        db.add(cfg)
        db.commit()
    return cfg
