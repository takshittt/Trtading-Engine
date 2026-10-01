import os
import sys
import tempfile

# Make the backend root importable (app/, db/, brokers/, ...) when pytest
# runs from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Pinned here, before any test module imports the app, rather than left to the
# individual files. db/engine.py builds its Engine at import time and calls
# load_dotenv(), and backend/.env points DATABASE_URL at the shared production
# database — so a test file that forgot to set it, collected first, would bind
# the whole run to the real ledger. load_dotenv() never overwrites a variable
# that is already set, so these win over .env.
#
# The rest are import-time requirements (security.py, crypto.py and the Shoonya
# adapter each refuse to load without theirs), set so the suite runs in a clean
# checkout with no .env — which is what the pre-push hook tests. The broker
# hosts use the reserved .invalid TLD, so a test that forgets to stub the
# broker fails to resolve instead of reaching the real Shoonya API.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="gateway-test-"), "test.db")
os.environ["JWT_SECRET"] = "test-secret-not-a-real-key-padded-to-32b"
os.environ["SHOONYA_API_HOST"] = "https://shoonya.invalid/NorenWClientTP"
os.environ["SHOONYA_WS_URL"] = "wss://shoonya.invalid/NorenWSTP/"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

# These two files are `test_`-prefixed but are NOT pytest unit tests — they are
# manual broker integration scripts (real login against live .env credentials,
# __main__ runners, print/return instead of asserts). Run them directly with
# `python3 test_<broker>_adapter.py`. Exclude them from automated collection so
# the suite doesn't error on their missing `token` fixture / live network calls.
collect_ignore = [
    "test_shoonya_adapter.py",
]
