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
FIXTURES_TEST_DIR = Path(__file__).parent / "fixtures" / "day3_test"


class TestPhase3b(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Disable background tasks during testing
        backend.ENABLE_DISPATCHER = False
        backend.ENABLE_POLLER = False

        if FIXTURES_TEST_DIR.exists():
            shutil.rmtree(FIXTURES_TEST_DIR)
        FIXTURES_TEST_DIR.mkdir(parents=True, exist_ok=True)
        cls.fixtures_patch = patch.object(backend, "FIXTURES_DAY3_DIR", FIXTURES_TEST_DIR)
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
        backend.get_sse_notify_event().clear()

    def tearDown(self):
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"

    def test_hangup_success_dialing_ringing_connected(self):
        """Active calls (dialing, ringing, connected) successfully transition to hangup_requested on provider 200."""
        statuses = ["dialing", "ringing", "connected"]
        for idx, st in enumerate(statuses):
            cid = f"c_active_{idx}"
            db.create_call(cid, f"key_active_{idx}", "+919876543210", db_path=TEST_DB_PATH)
            db.update_call(cid, status=st, provider_call_id=f"prov_{cid}", db_path=TEST_DB_PATH)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "message": "Call end initiated",
                "data": {"id": f"prov_{cid}", "status": "ended"}
            }

            with patch("requests.post", return_value=mock_resp) as mock_post:
                client = TestClient(backend.app)
                res = client.post(f"/calls/{cid}/hangup")
                self.assertEqual(res.status_code, 202)
                data = res.json()
                self.assertEqual(data["id"], cid)
                self.assertEqual(data["status"], "hangup_requested")

                # Verify payload sent to provider
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                self.assertEqual(call_args[0][0], "https://api.videosdk.live/v2/sip/call/end")
                self.assertEqual(call_args[1]["json"], {"callId": f"prov_{cid}"})

                # Verify DB status updated
                updated = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
                self.assertEqual(updated["status"], "hangup_requested")

                # Verify call.updated event recorded
                events = db.get_events_after(0, event_types=["call.updated"], db_path=TEST_DB_PATH)
                self.assertTrue(any(
                    json.loads(e["payload_json"]).get("status") == "hangup_requested" and e["call_id"] == cid
                    for e in events
                ))

        # Verify fixture file was created
        fixture_file = FIXTURES_TEST_DIR / "hangup_response.json"
        self.assertTrue(fixture_file.exists())
        with open(fixture_file, "r", encoding="utf-8") as f:
            saved_fixture = json.load(f)
            self.assertEqual(saved_fixture.get("message"), "Call end initiated")

    def test_hangup_already_hangup_requested_returns_200_no_provider_hit(self):
        """User amendment 3: If call is already hangup_requested, do NOT hit provider again; return 200 with current status."""
        cid = "c_already_req"
        db.create_call(cid, "key_already_req", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="hangup_requested", provider_call_id="prov_already_req", db_path=TEST_DB_PATH)

        with patch("requests.post") as mock_post:
            client = TestClient(backend.app)
            res = client.post(f"/calls/{cid}/hangup")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["id"], cid)
            self.assertEqual(data["status"], "hangup_requested")

            # Must NOT hit provider again
            mock_post.assert_not_called()

    def test_hangup_already_finished_call_returns_200_current_status(self):
        """Hang Up on an already-finished call returns 200 with its current status and does NOT hit provider."""
        terminal_statuses = ["completed", "busy", "no_answer", "failed", "unknown"]
        for idx, term_st in enumerate(terminal_statuses):
            cid = f"c_term_{idx}"
            db.create_call(cid, f"key_term_{idx}", "+919876543210", db_path=TEST_DB_PATH)
            db.update_call(cid, status=term_st, provider_call_id=f"prov_{cid}", db_path=TEST_DB_PATH)

            with patch("requests.post") as mock_post:
                client = TestClient(backend.app)
                res = client.post(f"/calls/{cid}/hangup")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["id"], cid)
                self.assertEqual(data["status"], term_st)
                mock_post.assert_not_called()

    def test_hangup_queued_or_missing_provider_id_returns_409(self):
        """Call that is queued or lacks provider_call_id cannot be hung up (returns 409 Conflict)."""
        # 1. Queued call
        db.create_call("c_queued", "key_q", "+919876543210", db_path=TEST_DB_PATH)
        client = TestClient(backend.app)
        res_q = client.post("/calls/c_queued/hangup")
        self.assertEqual(res_q.status_code, 409)

        # 2. Dialing call without provider_call_id
        db.create_call("c_no_prov", "key_np", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call("c_no_prov", status="dialing", provider_call_id=None, db_path=TEST_DB_PATH)
        res_np = client.post("/calls/c_no_prov/hangup")
        self.assertEqual(res_np.status_code, 409)

    def test_hangup_unknown_call_returns_404(self):
        """Hang up on non-existent call returns 404 Not Found."""
        client = TestClient(backend.app)
        res = client.post("/calls/c_does_not_exist/hangup")
        self.assertEqual(res.status_code, 404)

    def test_hangup_provider_rejection_returns_502(self):
        """Provider rejecting call end (HTTP 4xx/5xx) returns 502 Bad Gateway; DB status remains unchanged."""
        cid = "c_reject"
        db.create_call(cid, "key_rej", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="connected", provider_call_id="prov_rej", db_path=TEST_DB_PATH)

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error": "Call is not in active state"}'

        with patch("requests.post", return_value=mock_resp):
            client = TestClient(backend.app)
            res = client.post(f"/calls/{cid}/hangup")
            self.assertEqual(res.status_code, 502)
            self.assertIn("Provider rejected hangup request", res.json()["detail"]["error"])

            # Call remains connected in DB
            call = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
            self.assertEqual(call["status"], "connected")

    def test_hangup_provider_timeout_returns_504(self):
        """Provider network timeout during hangup returns 504 Gateway Timeout; DB status remains unchanged."""
        cid = "c_timeout"
        db.create_call(cid, "key_to", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="ringing", provider_call_id="prov_to", db_path=TEST_DB_PATH)

        with patch("requests.post", side_effect=requests.Timeout("Connection timed out")):
            client = TestClient(backend.app)
            res = client.post(f"/calls/{cid}/hangup")
            self.assertEqual(res.status_code, 504)
            self.assertIn("timed out", res.json()["detail"]["error"])

            # Call remains ringing in DB for poller to resolve
            call = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
            self.assertEqual(call["status"], "ringing")

    def test_poller_race_fix_hangup_requested_not_overwritten_by_non_terminal(self):
        """
        User amendment 1 (Race fix):
        Poller hangup_requested status ko kisi bhi non-terminal provider status (initiated, ringing, answered) se overwrite na kare.
        Status hangup_requested, provider ringing bheje -> status hangup_requested hi rahe.
        """
        cid = "c_race_fix"
        db.create_call(cid, "key_race_fix", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="hangup_requested", provider_call_id="prov_race_fix", db_path=TEST_DB_PATH)

        # Provider returns 'ringing'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_race_fix",
                "status": "ringing",
                "timelog": [{"status": "initiated"}, {"status": "ringing"}]
            }
        }

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        call = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
        self.assertEqual(call["status"], "hangup_requested")

        # Provider returns 'answered' / 'in-progress'
        mock_resp.json.return_value["data"]["status"] = "in-progress"
        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        call_after = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
        self.assertEqual(call_after["status"], "hangup_requested")

    def test_poller_hangup_requested_ringing_ended_maps_to_completed(self):
        """
        User rule:
        When status is hangup_requested and provider returns 'ended' without 'answered' in timelog (e.g. hung up while ringing),
        final status maps to 'completed' with reason 'Hung up from dashboard'.
        """
        cid = "c_ringing_hangup"
        db.create_call(cid, "key_ring_hangup", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="hangup_requested", provider_call_id="prov_ring_hangup", db_path=TEST_DB_PATH)

        # Provider returns ended with only initiated and ringing (no answered)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_ring_hangup",
                "status": "ended",
                "timelog": [
                    {"status": "initiated"},
                    {"status": "ringing"}
                ]
            }
        }

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        call = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["error_message"], "Hung up from dashboard")
        self.assertIsNotNone(call["ended_at"])

    def test_poller_hangup_requested_top_level_missed_maps_to_completed(self):
        """
        User amendment 2:
        hangup_requested mapping mein top-level missed ko bhi completed ('Hung up from dashboard') mein shamil kar.
        """
        cid = "c_missed_hangup"
        db.create_call(cid, "key_missed_hangup", "+919876543210", db_path=TEST_DB_PATH)
        db.update_call(cid, status="hangup_requested", provider_call_id="prov_missed_hangup", db_path=TEST_DB_PATH)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "statusCode": 200,
            "data": {
                "id": "prov_missed_hangup",
                "status": "missed",
                "timelog": [{"status": "initiated"}]
            }
        }

        with patch("requests.get", return_value=mock_resp):
            asyncio.run(backend.poll_active_calls())

        call = db.get_call_by_id(cid, db_path=TEST_DB_PATH)
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["error_message"], "Hung up from dashboard")
        self.assertIsNotNone(call["ended_at"])

    def test_ui_assets_contain_hangup_elements_and_handlers(self):
        """Verify HTML, CSS, and JS include Hang Up button, styling, double-click protection, and 504 retry logic."""
        client = TestClient(backend.app)

        # 1. HTML
        res_html = client.get("/")
        self.assertEqual(res_html.status_code, 200)
        self.assertIn('id="hangup-btn"', res_html.text)
        self.assertIn('id="hangup-warning"', res_html.text)
        self.assertIn("Phase 3b", res_html.text)

        # 2. CSS
        res_css = client.get("/static/style.css")
        self.assertEqual(res_css.status_code, 200)
        self.assertIn("button.btn-danger", res_css.text)
        self.assertIn(".card-title-actions", res_css.text)

        # 3. JS
        res_js = client.get("/static/app.js")
        self.assertEqual(res_js.status_code, 200)
        js = res_js.text
        self.assertIn('hangupBtn', js)
        self.assertIn('/hangup', js)
        self.assertIn('hangupBtn.disabled = true', js)
        self.assertIn('Hangup unconfirmed', js)


if __name__ == "__main__":
    unittest.main()

