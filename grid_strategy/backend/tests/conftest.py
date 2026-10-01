"""Test setup for the engine.

Two things have to happen before anything from the engine is imported:

* the database URL is pinned to a throwaway SQLite file. `db/engine.py` builds
  its Engine at import time from settings, and the deployed .env points at the
  shared production MySQL. A test run must never be one stray `db.commit()`
  away from the real ledger.
* JWT_SECRET is given a value, because `core/security.py` refuses to import
  without one.

`load_dotenv()` does not overwrite variables that already exist, so setting
them here wins over the deployed .env.
"""

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ["GRID_DB_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="grid-test-"), "test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-key")
os.environ.setdefault("MARGIN_GATE", "0")
