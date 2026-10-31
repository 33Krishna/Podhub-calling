import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

import backend
import db

TEST_DB_PATH = Path(__file__).parent / "test_resilience.sqlite"


class TestResilience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ENABLE_DISPATCHER = False
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
        cls.db_patch = patch.object(db, "DEFAULT_DB_PATH", TEST_DB_PATH)
        cls.db_patch.start()
        db.init_db(TEST_DB_PATH)

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        if TEST_DB_PATH.exists():
            try:
                TEST_DB_PATH.unlink()
            except Exception:
                pass

    def setUp(self):
        with db.get_connection(TEST_DB_PATH) as conn:
            conn.execute("DELETE FROM call_events")
            conn.execute("DELETE FROM calls")
        backend.dispatch_paused = False
        # Enable allow_real_dial for mock testing
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "1"

    def tearDown(self):
        os.environ.pop("PODHUB_ALLOW_REAL_DIAL", None)

    def test_idempotency_rules(self):
        """Verify POST /calls creates or replays call, and rejects conflict."""
        client = TestClient(backend.app)
        # 1. Create call
        payload1 = {"phone_number": "+919876543210", "idempotency_key": "idem-key-1"}
        res1 = client.post("/calls", json=payload1)
        self.assertEqual(res1.status_code, 202)
        data1 = res1.json()
        self.assertEqual(data1["replayed"], False)
        call_id = data1["id"]

        # 2. Replay same key and same number -> 200
        res2 = client.post("/calls", json=payload1)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2["replayed"], True)
        self.assertEqual(data2["id"], call_id)

        # 3. Conflict: same key with different number -> 409
        payload3 = {"phone_number": "+919999999999", "idempotency_key": "idem-key-1"}
        res3 = client.post("/calls", json=payload3)
        self.assertEqual(res3.status_code, 409)

        # 4. Invalid phone number format -> 422
        payload4 = {"phone_number": "12345", "idempotency_key": "idem-key-invalid"}
        res4 = client.post("/calls", json=payload4)
        self.assertEqual(res4.status_code, 422)

    def test_test_a_explicit_provider_reject_marks_failed(self):
        """
        Test A: Dial raises an explicit 400 HTTPError from provider.
        Backend marks status 'failed', records error message, and continues serving requests.
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error": "Invalid destination phone number"}'
        mock_error = requests.HTTPError(response=mock_resp)

        client = TestClient(backend.app)
        with patch("backend.dial", side_effect=mock_error) as mock_dial:
            res = client.post(
                "/calls",
                json={"phone_number": "+910000000000", "idempotency_key": "key-test-a"},
            )
            self.assertEqual(res.status_code, 202)
            call_id = res.json()["id"]

            # Dispatch one call
            dispatched = asyncio.run(backend.dispatch_one_call())
            self.assertTrue(dispatched)

            call = db.get_call_by_id(call_id, TEST_DB_PATH)
            self.assertEqual(call["status"], "failed")
            self.assertIn("Provider rejected (400)", call["error_message"])
            self.assertFalse(backend.dispatch_paused)

            # Backend is still running and serving requests
            health = client.get(f"/calls/{call_id}")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "failed")

    def test_test_b_missing_call_id_becomes_unknown_and_pauses_dispatch(self):
        """
        Test B: Dial returns 200 OK but response is missing callId/roomId.
        Call becomes 'unknown', dispatch pauses, room_id is saved if present, and NO second dial happens.
        """
        mock_response = {
            "provider_call_id": None,
            "room_id": "partial-room-123",
            "status": "initiated",
            "raw": {"status": "ok"},
        }

        client = TestClient(backend.app)
        with patch("backend.dial", return_value=mock_response) as mock_dial:
            res1 = client.post(
                "/calls",
                json={"phone_number": "+919876543210", "idempotency_key": "key-test-b-1"},
            )
            self.assertEqual(res1.status_code, 202)
            call_id1 = res1.json()["id"]

            # Queue a second call
            res2 = client.post(
                "/calls",
                json={"phone_number": "+919876543211", "idempotency_key": "key-test-b-2"},
            )
            self.assertEqual(res2.status_code, 202)
            call_id2 = res2.json()["id"]

            # Dispatch first call
            dispatched1 = asyncio.run(backend.dispatch_one_call())
            self.assertTrue(dispatched1)

            call1 = db.get_call_by_id(call_id1, TEST_DB_PATH)
            self.assertEqual(call1["status"], "unknown")
            self.assertEqual(call1["room_id"], "partial-room-123")  # Room ID saved!
            self.assertIn("Dial response missing callId or roomId", call1["error_message"])
            self.assertTrue(backend.dispatch_paused)

            # Try dispatching again: must NOT dispatch second call because dispatch is paused!
            dispatched2 = asyncio.run(backend.dispatch_one_call())
            self.assertFalse(dispatched2)

            # Verify dial was called exactly once (for call 1), never for call 2
            self.assertEqual(mock_dial.call_count, 1)

            # Call 2 remains queued
            call2 = db.get_call_by_id(call_id2, TEST_DB_PATH)
            self.assertEqual(call2["status"], "queued")

    def test_test_c_timeout_becomes_unknown_and_pauses_dispatch(self):
        """
        Test C: Dial encounters requests.Timeout (ambiguous network error).
        Status becomes 'unknown', dispatch pauses.
        """
        mock_timeout = requests.Timeout("Connection timed out after 20 seconds")

        client = TestClient(backend.app)
        with patch("backend.dial", side_effect=mock_timeout) as mock_dial:
            res = client.post(
                "/calls",
                json={"phone_number": "+919876543210", "idempotency_key": "key-test-c"},
            )
            call_id = res.json()["id"]

            dispatched = asyncio.run(backend.dispatch_one_call())
            self.assertTrue(dispatched)

            call = db.get_call_by_id(call_id, TEST_DB_PATH)
            self.assertEqual(call["status"], "unknown")
            self.assertIn("Network error/timeout", call["error_message"])
            self.assertTrue(backend.dispatch_paused)

    def test_real_dial_response_fixture_shape(self):
        """
        Test real VideoSDK fixture response shape:
        IDs are in data.id and data.roomId, status is 'initiated'.
        Assert provider_call_id and room_id are non-null and status maps to 'dialing'.
        """
        raw_body = {
            "message": "Call initiated successfully",
            "data": {
                "id": "7e29776b-bf1f-4425-93f0-9f32c950f932",
                "type": "outbound",
                "transport": "sip",
                "gatewayId": "221fd865-aec6-4c2b-8103-3f2b53df2752",
                "ruleId": "rr_q8y23w",
                "roomId": "p8j8-ly25-12p9",
                "to": "+919999999999",
                "from": "+918065354620",
                "status": "initiated",
            },
        }

        # Simulated dial() output with real fixture shape
        data = raw_body["data"]
        mock_dial_result = {
            "provider_call_id": data.get("id") or data.get("callId") or data.get("sipCallId"),
            "room_id": data.get("roomId"),
            "status": data.get("status"),
            "raw": raw_body,
        }

        client = TestClient(backend.app)
        with patch("backend.dial", return_value=mock_dial_result) as mock_dial:
            res = client.post(
                "/calls",
                json={"phone_number": "+919999999999", "idempotency_key": "key-real-shape"},
            )
            self.assertEqual(res.status_code, 202)
            call_id = res.json()["id"]

            dispatched = asyncio.run(backend.dispatch_one_call())
            self.assertTrue(dispatched)

            call = db.get_call_by_id(call_id, TEST_DB_PATH)
            self.assertEqual(call["provider_call_id"], "7e29776b-bf1f-4425-93f0-9f32c950f932")
            self.assertEqual(call["room_id"], "p8j8-ly25-12p9")
            # Must be cleanly mapped to 'dialing' so SQLite constraint does not fail
            self.assertEqual(call["status"], "dialing")
            self.assertFalse(backend.dispatch_paused)

    def test_refuses_dial_without_allow_real_dial_env(self):
        """
        Verify safety guard: without PODHUB_ALLOW_REAL_DIAL=1,
        the backend refuses to dial and marks call 'failed'.
        """
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"
        client = TestClient(backend.app)
        with patch("backend.dial") as mock_dial:
            res = client.post(
                "/calls",
                json={"phone_number": "+919999999999", "idempotency_key": "key-guard-test"},
            )
            call_id = res.json()["id"]

            dispatched = asyncio.run(backend.dispatch_one_call())
            self.assertTrue(dispatched)

            # Assert dial was NOT invoked
            mock_dial.assert_not_called()

            call = db.get_call_by_id(call_id, TEST_DB_PATH)
            self.assertEqual(call["status"], "failed")
            self.assertIn("PODHUB_ALLOW_REAL_DIAL", call["error_message"])


if __name__ == "__main__":
    unittest.main()
