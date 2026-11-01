import test_support

import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend
import db

REAL_DB_PATH = (Path(__file__).parent / "calls.sqlite").resolve()


class TestIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ENABLE_DISPATCHER = False
        backend.ENABLE_POLLER = False

    def test_real_calls_sqlite_untouched_read_only(self):
        """
        Verify that real calls.sqlite is never modified by test operations.
        Opens real calls.sqlite strictly in read-only mode (mode=ro, uri=True) so no new file is ever created.
        """
        if not REAL_DB_PATH.exists():
            self.skipTest("Real calls.sqlite does not exist; skipping read-only comparison.")

        # Read state before test operations strictly in read-only mode
        uri = f"file:{REAL_DB_PATH}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            count_before = conn.execute("SELECT count(*) as cnt FROM calls").fetchone()["cnt"]
            rows_before = [
                dict(r) for r in conn.execute("SELECT id, status, created_at FROM calls ORDER BY id").fetchall()
            ]

        # Run test operations on backend using TestClient
        client = TestClient(backend.app)
        res = client.get("/calls?limit=10")
        self.assertEqual(res.status_code, 200)

        res2 = client.post(
            "/calls",
            json={"phone_number": "+919999988888", "idempotency_key": "isolation-test-key-1"},
        )
        self.assertEqual(res2.status_code, 202)

        # Read state after test operations strictly in read-only mode
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            count_after = conn.execute("SELECT count(*) as cnt FROM calls").fetchone()["cnt"]
            rows_after = [
                dict(r) for r in conn.execute("SELECT id, status, created_at FROM calls ORDER BY id").fetchall()
            ]

        # Real DB content and row count must remain 100% unchanged
        self.assertEqual(count_after, count_before)
        self.assertEqual(rows_after, rows_before)

    def test_db_hard_guard_blocks_real_db_access_in_tests(self):
        """
        Verify that if PODHUB_TESTING=1 and any code attempts to open the real calls.sqlite,
        db.get_connection() immediately raises RuntimeError.
        """
        self.assertEqual(os.getenv("PODHUB_TESTING"), "1")
        with self.assertRaises(RuntimeError) as ctx:
            with db.get_connection(REAL_DB_PATH):
                pass
        self.assertIn("Tests must never use the real calls.sqlite", str(ctx.exception))

    def test_network_tripwire_catches_unmocked_calls(self):
        """
        Verify that test_support network tripwire catches any unmocked requests.get or requests.post.
        """
        import requests
        with self.assertRaises(RuntimeError) as ctx:
            requests.get("https://api.videosdk.live/v2/sip/call")
        self.assertIn("Unmocked network call attempted", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx2:
            requests.post("https://api.videosdk.live/v2/sip/call/end")
        self.assertIn("Unmocked network call attempted", str(ctx2.exception))

    def test_stale_queued_calls_skipped_on_startup(self):
        """
        Verify that backend startup marks queued calls older than 2 minutes as failed
        with error_message = 'Stale queued call skipped on startup', while fresh queued calls are preserved.
        """
        now = datetime.now(timezone.utc)
        stale_time = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_time = (now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Insert directly into test DB
        with db.get_connection(test_support.TEST_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO calls (id, idempotency_key, phone_number, status, worker_state, outcome_state, created_at)
                VALUES ('c_stale_1', 'k_stale_1', '+919999911111', 'queued', 'waiting', 'pending', ?),
                       ('c_fresh_1', 'k_fresh_1', '+919999922222', 'queued', 'waiting', 'pending', ?)
                """,
                (stale_time, fresh_time),
            )

        # Run startup stale call cleanup
        cleaned = backend.cleanup_stale_queued_calls(max_age_seconds=120.0)
        self.assertGreaterEqual(cleaned, 1)

        stale_call = db.get_call_by_id("c_stale_1", db_path=test_support.TEST_DB_PATH)
        self.assertEqual(stale_call["status"], "failed")
        self.assertEqual(stale_call["error_message"], "Stale queued call skipped on startup")
        self.assertIsNotNone(stale_call["ended_at"])

        fresh_call = db.get_call_by_id("c_fresh_1", db_path=test_support.TEST_DB_PATH)
        self.assertEqual(fresh_call["status"], "queued")
        self.assertIsNone(fresh_call["error_message"])


if __name__ == "__main__":
    unittest.main()

