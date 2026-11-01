import atexit
import os
import tempfile
from pathlib import Path

# 1. Paths and Environment Setup
PROJECT_DIR = Path(__file__).parent.resolve()
REAL_CALLS_DB_PATH = (PROJECT_DIR / "calls.sqlite").resolve()

# Create single process-wide temp DB
_temp_file = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
_temp_file.close()
TEST_DB_PATH = Path(_temp_file.name).resolve()

os.environ["PODHUB_DB_PATH"] = str(TEST_DB_PATH)
os.environ["PODHUB_TESTING"] = "1"
os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"


def _cleanup_temp_db():
    try:
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
    except Exception:
        pass


atexit.register(_cleanup_temp_db)

# 2. Network Tripwire
# Any unmocked external request in tests raises an immediate RuntimeError.
import requests

_real_session_request = requests.Session.request
_real_requests_get = requests.get
_real_requests_post = requests.post


def _network_tripwire(self, method, url, *args, **kwargs):
    raise RuntimeError(
        f"Unmocked network call attempted in tests: {method} {url}. All external calls must be explicitly mocked."
    )


def _network_tripwire_get(url, *args, **kwargs):
    raise RuntimeError(
        f"Unmocked network call attempted in tests: GET {url}. All external calls must be explicitly mocked."
    )


def _network_tripwire_post(url, *args, **kwargs):
    raise RuntimeError(
        f"Unmocked network call attempted in tests: POST {url}. All external calls must be explicitly mocked."
    )


requests.Session.request = _network_tripwire
requests.get = _network_tripwire_get
requests.post = _network_tripwire_post

# 3. Initialize test DB schema
import db
db.DEFAULT_DB_PATH = TEST_DB_PATH
db.init_db(TEST_DB_PATH)

