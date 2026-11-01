import test_support

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

import backend
import db

TEST_DB_PATH = test_support.TEST_DB_PATH
FIXTURES_TEST_DIR = Path(__file__).parent / "fixtures" / "day2_test"


class TestPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Disable background tasks during testing
        backend.ENABLE_DISPATCHER = False
        backend.ENABLE_POLLER = False

        if FIXTURES_TEST_DIR.exists():
            shutil.rmtree(FIXTURES_TEST_DIR)
        FIXTURES_TEST_DIR.mkdir(parents=True, exist_ok=True)
        cls.fixtures_patch = patch.object(backend, "FIXTURES_DAY2_DIR", FIXTURES_TEST_DIR)
        cls.fixtures_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.fixtures_patch.stop()
        if FIXTURES_TEST_DIR.exists():
            try:
                shutil.rmtree(FIXTURES_TEST_DIR)
            except Exception:
                pass

    def setUp(self):
        with db.get_connection(TEST_DB_PATH) as conn:
            conn.execute("DELETE FROM call_events")
            conn.execute("DELETE FROM calls")
        backend.dispatch_paused = False
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"

    def tearDown(self):
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"

    def test_dashboard_page(self):
        """Verify GET / returns 200 with dashboard HTML linking to static assets."""
        client = TestClient(backend.app)
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        html = res.text
        self.assertIn("PodHub Calling", html)
        self.assertIn("phone-input", html)
        self.assertIn("start-btn", html)
        self.assertIn("calls-list", html)
        self.assertIn("Last Provider Update", html)
        self.assertIn("/static/style.css", html)
        self.assertIn("/static/app.js", html)
        # Verify status timeline was removed
        self.assertNotIn("status timeline", html.lower())

        # Verify static CSS asset
        res_css = client.get("/static/style.css")
        self.assertEqual(res_css.status_code, 200)
        self.assertIn("text/css", res_css.headers["content-type"])
        self.assertIn("--bg: #0f172a;", res_css.text)
        self.assertIn(".detail-alert-warning", res_css.text)

        # Verify static JS asset
        res_js = client.get("/static/app.js")
        self.assertEqual(res_js.status_code, 200)
        self.assertTrue(
            "javascript" in res_js.headers["content-type"]
            or "text/plain" in res_js.headers["content-type"]
        )
        self.assertIn("initSSE()", res_js.text)
        self.assertIn("Reason: ", res_js.text)

    def test_route_precedence_stream_not_treated_as_call_id(self):
        """Verify GET /calls/stream is not routed to GET /calls/{call_id}."""
        client = TestClient(backend.app)
        with client.stream("GET", "/calls/stream") as res:
            self.assertEqual(res.status_code, 200)
            self.assertIn("text/event-stream", res.headers["content-type"])
            for line in res.iter_lines():
                if line:
                    self.assertIn("connected", line)
                    backend.get_sse_notify_event().set()
                    break

    def test_status_normalization(self):
        """Verify known status mappings and that unknown status returns None."""
        self.assertEqual(backend.normalize_provider_status("initiated"), "dialing")
        self.assertEqual(backend.normalize_provider_status("ringing"), "ringing")
        self.assertEqual(backend.normalize_provider_status("in-progress"), "connected")
        self.assertEqual(backend.normalize_provider_status("connected"), "connected")
        self.assertEqual(backend.normalize_provider_status("answered"), "connected")
        self.assertEqual(backend.normalize_provider_status("completed"), "completed")
        self.assertEqual(backend.normalize_provider_status("ended"), "completed")
        self.assertEqual(backend.normalize_provider_status("terminated"), "completed")
        self.assertEqual(backend.normalize_provider_status("busy"), "busy")
        self.assertEqual(backend.normalize_provider_status("no-answer"), "no_answer")
        self.assertEqual(backend.normalize_provider_status("missed"), "no_answer")
        self.assertEqual(backend.normalize_provider_status("failed"), "failed")
        self.assertEqual(backend.normalize_provider_status("canceled"), "failed")

        # Normalization with timelog
        self.assertEqual(
            backend.normalize_provider_status("ended", timelog=[{"status": "answered"}]),
            "completed",
        )
        self.assertEqual(
            backend.normalize_provider_status("ended", timelog=[{"status": "missed"}]),
            "no_answer",
        )
        self.assertEqual(
            backend.normalize_provider_status("ended", timelog=[{"status": "rejected"}]),
            "failed",
        )
        # Empty timelog defaults to completed
        self.assertEqual(
            backend.normalize_provider_status("ended", timelog=[]),
            "completed",
        )

        # Unrecognized statuses must return None
        self.assertIsNone(backend.normalize_provider_status("weird_status_123"))
        self.assertIsNone(backend.normalize_provider_status(None))
        self.assertIsNone(backend.normalize_provider_status(""))

    def test_unrecognized_status_keeps_call_active_and_saves_fixture(self):
        """
        Instruction 1: Unrecognized provider status pe calls.status mat badal.
        Sirf warning log kar aur raw response fixtures/day2/ mein save kar.
        """
        # Create an active dialing call with a provider_call_id
        call = db.create_call("c_test_unrec", "key-unrec", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_unrec",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_unrec_1",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_unrec_1",
                "status": "custom_provider_state_xyz",
            },
        }

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        updated_call = db.get_call_by_id("c_test_unrec", db_path=TEST_DB_PATH)
        # Status MUST NOT be changed to unknown or anything else; remains 'dialing' (active)
        self.assertEqual(updated_call["status"], "dialing")
        self.assertIsNotNone(updated_call["last_provider_poll_at"])
        self.assertIn("custom_provider_state_xyz", updated_call["provider_raw_json"])

        # Raw fixture file must be written
        fixture_file = FIXTURES_TEST_DIR / "poll_custom_provider_state_xyz.json"
        self.assertTrue(fixture_file.exists())

    def test_poll_failure_keeps_status_and_poll_time_unchanged(self):
        """
        Instruction 2: Poll fail (timeout, 401, 429, network, non-200) pe status
        unchanged rakh, last_provider_poll_at update mat kar, error log kar. Poller crash na ho.
        """
        call = db.create_call("c_test_fail", "key-fail", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_fail",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_fail_1",
            last_provider_poll_at=None,
        )

        # 1. Test 500 error
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.text = "Internal Server Error"
        with patch("requests.get", return_value=mock_500):
            asyncio.run(backend.poll_active_calls())

        call1 = db.get_call_by_id("c_test_fail", db_path=TEST_DB_PATH)
        self.assertEqual(call1["status"], "dialing")
        self.assertIsNone(call1["last_provider_poll_at"])

        # 2. Test timeout exception
        with patch("requests.get", side_effect=requests.Timeout("Connection timed out")):
            # Poller must NOT crash
            asyncio.run(backend.poll_active_calls())

        call2 = db.get_call_by_id("c_test_fail", db_path=TEST_DB_PATH)
        self.assertEqual(call2["status"], "dialing")
        self.assertIsNone(call2["last_provider_poll_at"])

        # 3. Test 401 Unauthorized
        mock_401 = MagicMock()
        mock_401.status_code = 401
        mock_401.text = "Unauthorized token"
        with patch("requests.get", return_value=mock_401):
            asyncio.run(backend.poll_active_calls())

        call3 = db.get_call_by_id("c_test_fail", db_path=TEST_DB_PATH)
        self.assertEqual(call3["status"], "dialing")
        self.assertIsNone(call3["last_provider_poll_at"])

    def test_fixture_written_only_once_per_raw_status(self):
        """Instruction 4: fixtures/day2/poll_<raw_status>.json sirf pehli baar naya raw status dikhne pe likh."""
        call = db.create_call("c_test_once", "key-once", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_once",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_once_1",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {"id": "prov_once_1", "status": "ringing", "timestamp": "first_poll"},
        }

        fixture_file = FIXTURES_TEST_DIR / "poll_ringing.json"
        if fixture_file.exists():
            fixture_file.unlink()

        # First poll: creates fixture
        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        self.assertTrue(fixture_file.exists())
        content_first = fixture_file.read_text(encoding="utf-8")
        self.assertIn("first_poll", content_first)

        # Modify mock to return different data for same status
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {"id": "prov_once_1", "status": "ringing", "timestamp": "second_poll"},
        }

        # Second poll: should NOT overwrite fixture
        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        content_second = fixture_file.read_text(encoding="utf-8")
        self.assertEqual(content_first, content_second)
        self.assertIn("first_poll", content_second)
        self.assertNotIn("second_poll", content_second)

    def test_sse_stream_filters_only_call_updated(self):
        """
        Instruction 6: SSE sirf call.updated events browser ko bheje.
        Heartbeat aur worker events browser tak mat push kar.
        """
        db.create_call("c_test_sse", "key-sse", "+919876543210", db_path=TEST_DB_PATH)
        # Save a call.updated event and a worker.heartbeat event
        db.save_call_event("c_test_sse", "heartbeat-1", "worker.heartbeat", "{}", db_path=TEST_DB_PATH)
        db.save_call_event(
            "c_test_sse",
            "updated-1",
            "call.updated",
            json.dumps({"call_id": "c_test_sse", "status": "connected"}),
            db_path=TEST_DB_PATH,
        )

        # Check get_events_after with filter
        events = db.get_events_after(0, event_types=["call.updated"], db_path=TEST_DB_PATH)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "call.updated")
        self.assertIn("connected", events[0]["payload_json"])

    def test_poller_status_transition_and_sse_trigger(self):
        """Verify poller transitions status from dialing -> ringing -> connected -> completed."""
        call = db.create_call("c_trans", "key-trans", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_trans",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_trans_1",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {"id": "prov_trans_1", "status": "in-progress"},
        }

        with patch("requests.get", return_value=mock_resp):
            backend.get_sse_notify_event().clear()
            asyncio.run(backend.poll_active_calls())

        # Check call status updated to connected
        updated = db.get_call_by_id("c_trans", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "connected")
        # Check SSE wake-up event was signaled
        self.assertTrue(backend.get_sse_notify_event().is_set())

        # Check call.updated event recorded in DB
        events = db.get_events_after(0, event_types=["call.updated"], db_path=TEST_DB_PATH)
        statuses = [json.loads(e["payload_json"])["status"] for e in events]
        self.assertIn("connected", statuses)

    def test_poll_ended_answered_becomes_completed(self):
        """Test answered + ended = completed using real fixture data."""
        call = db.create_call("c_test_ans", "key-ans", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_ans",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_ans_1",
        )

        real_fixture = Path(__file__).parent / "fixtures" / "day2" / "poll_ended.json"
        with open(real_fixture, "r", encoding="utf-8") as f:
            fixture_data = json.load(f)
        fixture_data["data"]["id"] = "prov_ans_1"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fixture_data

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        updated = db.get_call_by_id("c_test_ans", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "completed")
        self.assertIsNotNone(updated["ended_at"])
        self.assertIsNone(updated["error_message"])

    def test_poll_ended_missed_becomes_no_answer(self):
        """Test missed + ended = no_answer and error_message saved using real fixture data."""
        call = db.create_call("c_test_missed", "key-missed", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_missed",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_missed_1",
        )

        real_fixture = Path(__file__).parent / "fixtures" / "day2" / "poll_ended_missed.json"
        with open(real_fixture, "r", encoding="utf-8") as f:
            fixture_data = json.load(f)
        fixture_data["data"]["id"] = "prov_missed_1"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fixture_data

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        updated = db.get_call_by_id("c_test_missed", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "no_answer")
        self.assertIsNotNone(updated["ended_at"])
        self.assertEqual(updated["error_message"], "status 480 Callee currently unavailable")

        # Verify fixture was written for ended_missed
        ended_missed_file = FIXTURES_TEST_DIR / "poll_ended_missed.json"
        self.assertTrue(ended_missed_file.exists())

    def test_poll_ended_without_answered_or_missed_becomes_failed(self):
        """Test koi answered/missed nahi = failed, error_message saved, and warning logged."""
        call = db.create_call("c_test_reject", "key-reject", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_test_reject",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_reject_1",
        )

        real_fixture = Path(__file__).parent / "fixtures" / "day2" / "poll_ended_missed.json"
        with open(real_fixture, "r", encoding="utf-8") as f:
            fixture_data = json.load(f)
        fixture_data["data"]["id"] = "prov_reject_1"
        fixture_data["data"]["timelog"] = [
            {"timestamp": "2026-10-31T16:24:22.964Z", "status": "initiated"},
            {"timestamp": "2026-10-31T16:24:31.173Z", "status": "ringing"},
            {
                "timestamp": "2026-10-31T16:25:24.761Z",
                "status": "rejected",
                "message": "Call rejected by recipient",
            },
            {
                "timestamp": "2026-10-31T16:25:25.198Z",
                "status": "ended",
                "message": "Call ended by server",
            },
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fixture_data

        with self.assertLogs(backend.logger, level="WARNING") as log_ctx:
            with patch("requests.get", return_value=mock_resp):
                asyncio.run(backend.poll_active_calls())

        updated = db.get_call_by_id("c_test_reject", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "failed")
        self.assertIsNotNone(updated["ended_at"])
        # Last message in timelog
        self.assertEqual(updated["error_message"], "Call ended by server")

        # Warning log checks
        log_text = " ".join(log_ctx.output)
        self.assertIn("rejected", log_text)
        self.assertIn("c_test_reject", log_text)

        # Fixture saved on new timelog status name
        ended_rejected_file = FIXTURES_TEST_DIR / "poll_ended_rejected.json"
        self.assertTrue(ended_rejected_file.exists())

    def test_poll_top_level_missed_becomes_no_answer(self):
        """Top-level provider status missed maps to no_answer."""
        call = db.create_call("c_top_missed", "key-top-missed", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_top_missed",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_top_missed_1",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_top_missed_1",
                "status": "missed",
                "timelog": [
                    {
                        "status": "missed",
                        "message": "status 480 Callee currently unavailable",
                    }
                ],
            },
        }

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        updated = db.get_call_by_id("c_top_missed", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "no_answer")
        self.assertEqual(updated["error_message"], "status 480 Callee currently unavailable")

    def test_poll_ended_empty_timelog_defaults_to_completed_with_warning(self):
        """If provider status ended but timelog is empty/missing, default to completed and log warning."""
        call = db.create_call("c_empty_time", "key-empty-time", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(
            "c_empty_time",
            db_path=TEST_DB_PATH,
            status="dialing",
            provider_call_id="prov_empty_1",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_empty_1",
                "status": "ended",
                "timelog": [],
            },
        }

        with self.assertLogs(backend.logger, level="WARNING") as log_ctx:
            with patch("requests.get", return_value=mock_resp):
                asyncio.run(backend.poll_active_calls())

        updated = db.get_call_by_id("c_empty_time", db_path=TEST_DB_PATH)
        self.assertEqual(updated["status"], "completed")

        log_text = " ".join(log_ctx.output)
        self.assertIn("c_empty_time", log_text)
        self.assertIn("ended", log_text)

    def test_worker_stopped_emits_call_updated_and_heartbeat_does_not(self):
        """Verify worker.stopped emits a call.updated event with worker_state='stopped', while heartbeat does not."""
        call = db.create_call("c_wstop", "key-wstop", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call("c_wstop", db_path=TEST_DB_PATH, status="connected", worker_state="active")

        client = TestClient(backend.app)

        # 1. Post worker.stopped event
        res_stop = client.post(
            "/internal/calls/c_wstop/events",
            json={
                "event_key": "evt-wstop-1",
                "type": "worker.stopped",
                "payload": {},
            },
        )
        self.assertEqual(res_stop.status_code, 201)

        # Verify calls table updated to stopped
        updated_call = db.get_call_by_id("c_wstop", db_path=TEST_DB_PATH)
        self.assertEqual(updated_call["worker_state"], "stopped")

        # Verify call.updated event was saved
        events = db.get_events_after(0, event_types=["call.updated"], db_path=TEST_DB_PATH)
        self.assertTrue(len(events) > 0)
        latest_event = events[-1]
        payload = json.loads(latest_event["payload_json"])
        self.assertEqual(payload["call_id"], "c_wstop")
        self.assertEqual(payload["worker_state"], "stopped")
        self.assertEqual(payload["status"], "connected")

        events_count_before = len(events)

        # 2. Post worker.heartbeat event
        res_hb = client.post(
            "/internal/calls/c_wstop/events",
            json={
                "event_key": "evt-hb-1",
                "type": "worker.heartbeat",
                "payload": {},
            },
        )
        self.assertEqual(res_hb.status_code, 201)

        # Verify NO new call.updated event was saved for heartbeat
        events_after = db.get_events_after(0, event_types=["call.updated"], db_path=TEST_DB_PATH)
        self.assertEqual(len(events_after), events_count_before)


if __name__ == "__main__":
    unittest.main()
