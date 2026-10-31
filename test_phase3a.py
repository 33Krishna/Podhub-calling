import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend
import db

TEST_DB_PATH = Path(__file__).parent / "test_phase3a.sqlite"


class TestPhase3a(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ENABLE_DISPATCHER = False
        backend.ENABLE_POLLER = False

        if TEST_DB_PATH.exists():
            try:
                TEST_DB_PATH.unlink()
            except Exception:
                pass
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
        os.environ["PODHUB_ALLOW_REAL_DIAL"] = "0"
        backend.get_sse_notify_event().clear()

    def tearDown(self):
        os.environ.pop("PODHUB_ALLOW_REAL_DIAL", None)

    def test_transcript_final_event_storage_and_sse_trigger(self):
        """Verify POST /internal/calls/{id}/events saves transcript.final and wakes up SSE."""
        call = db.create_call("c_t1", "key-t1", "+919876543210", db_path=TEST_DB_PATH)

        with TestClient(backend.app) as client:
            backend.get_sse_notify_event().clear()
            res = client.post(
                "/internal/calls/c_t1/events",
                json={
                    "event_key": "c_t1-transcript-1",
                    "type": "transcript.final",
                    "payload": {"speaker": "agent", "text": "Hi, good evening! Main Ananya bol rahi hoon."},
                },
            )
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertTrue(data.get("accepted"))
            self.assertIsInstance(data.get("event_id"), int)

            # SSE wake-up event must be set
            self.assertTrue(backend.get_sse_notify_event().is_set())

            # Verify saved in call_events table
            transcript = db.get_call_transcript("c_t1", db_path=TEST_DB_PATH)
            self.assertEqual(len(transcript), 1)
            self.assertEqual(transcript[0]["speaker"], "agent")
            self.assertEqual(transcript[0]["text"], "Hi, good evening! Main Ananya bol rahi hoon.")

    def test_transcript_final_event_deduplication(self):
        """Backend-side check: sending identical event_key twice returns 200 and does NOT duplicate row."""
        call = db.create_call("c_t2", "key-t2", "+919876543210", db_path=TEST_DB_PATH)

        with TestClient(backend.app) as client:
            # First submission: 201 Created
            res1 = client.post(
                "/internal/calls/c_t2/events",
                json={
                    "event_key": "c_t2-transcript-dup",
                    "type": "transcript.final",
                    "payload": {"speaker": "prospect", "text": "Haan bolie, kya baat hai?"},
                },
            )
            self.assertEqual(res1.status_code, 201)
            event_id_1 = res1.json()["event_id"]

            # Count rows in call_events
            with db.get_connection(TEST_DB_PATH) as conn:
                count_1 = conn.execute("SELECT count(*) as cnt FROM call_events WHERE call_id = 'c_t2'").fetchone()["cnt"]
            self.assertEqual(count_1, 1)

            # Second submission with identical event_key & identical payload: returns 200 OK
            res2 = client.post(
                "/internal/calls/c_t2/events",
                json={
                    "event_key": "c_t2-transcript-dup",
                    "type": "transcript.final",
                    "payload": {"speaker": "prospect", "text": "Haan bolie, kya baat hai?"},
                },
            )
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(res2.json()["event_id"], event_id_1)

            # Row count must still be 1 (strictly deduped)
            with db.get_connection(TEST_DB_PATH) as conn:
                count_2 = conn.execute("SELECT count(*) as cnt FROM call_events WHERE call_id = 'c_t2'").fetchone()["cnt"]
            self.assertEqual(count_2, 1)

    def test_transcript_final_event_conflict(self):
        """Submitting same event_key with different payload returns 409 Conflict."""
        call = db.create_call("c_t3", "key-t3", "+919876543210", db_path=TEST_DB_PATH)

        with TestClient(backend.app) as client:
            res1 = client.post(
                "/internal/calls/c_t3/events",
                json={
                    "event_key": "c_t3-evt-conflict",
                    "type": "transcript.final",
                    "payload": {"speaker": "agent", "text": "Original text"},
                },
            )
            self.assertEqual(res1.status_code, 201)

            res2 = client.post(
                "/internal/calls/c_t3/events",
                json={
                    "event_key": "c_t3-evt-conflict",
                    "type": "transcript.final",
                    "payload": {"speaker": "agent", "text": "Conflicting different text"},
                },
            )
            self.assertEqual(res2.status_code, 409)

    def test_get_call_returns_ordered_transcript(self):
        """Verify GET /calls/{id} returns chronological transcript items."""
        call = db.create_call("c_t4", "key-t4", "+919876543210", db_path=TEST_DB_PATH)

        with TestClient(backend.app) as client:
            client.post(
                "/internal/calls/c_t4/events",
                json={
                    "event_key": "c_t4-1",
                    "type": "transcript.final",
                    "payload": {"speaker": "agent", "text": "First greeting"},
                },
            )
            client.post(
                "/internal/calls/c_t4/events",
                json={
                    "event_key": "c_t4-2",
                    "type": "transcript.final",
                    "payload": {"speaker": "prospect", "text": "Prospect reply"},
                },
            )

            res = client.get("/calls/c_t4")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("transcript", data)
            transcript = data["transcript"]
            self.assertEqual(len(transcript), 2)
            self.assertEqual(transcript[0]["speaker"], "agent")
            self.assertEqual(transcript[0]["text"], "First greeting")
            self.assertEqual(transcript[1]["speaker"], "prospect")
            self.assertEqual(transcript[1]["text"], "Prospect reply")
            self.assertTrue(transcript[0]["event_id"] < transcript[1]["event_id"])

    def test_sse_stream_delivers_transcript_final_with_metadata(self):
        """Verify GET /calls/stream queries and delivers transcript.final with call_id and event_id."""
        call = db.create_call("c_t5", "key-t5", "+919876543210", db_path=TEST_DB_PATH)
        ev_id, _, _ = db.save_call_event(
            call_id="c_t5",
            event_key="c_t5-trans-sse",
            event_type="transcript.final",
            payload_json=json.dumps({"speaker": "prospect", "text": "I am interested"}),
            db_path=TEST_DB_PATH,
        )

        # Verify db.get_events_after queries transcript.final
        events = db.get_events_after(
            0, event_types=["call.updated", "transcript.final"], db_path=TEST_DB_PATH
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "transcript.final")
        self.assertEqual(events[0]["call_id"], "c_t5")

        with TestClient(backend.app) as client:
            with client.stream("GET", "/calls/stream?after=0") as res:
                self.assertEqual(res.status_code, 200)
                lines = []
                for line in res.iter_lines():
                    if line:
                        lines.append(line)
                        if "event: transcript.final" in line:
                            continue
                        if line.startswith("data: ") and "interested" in line:
                            data_str = line[len("data: "):]
                            data_json = json.loads(data_str)
                            self.assertEqual(data_json.get("call_id"), "c_t5")
                            self.assertEqual(data_json.get("speaker"), "prospect")
                            self.assertEqual(data_json.get("text"), "I am interested")
                            self.assertEqual(data_json.get("event_id"), ev_id)
                            break

    def test_ui_assets_contain_transcript_and_dedupe_logic(self):
        """Verify HTML, CSS, and JS include required transcript display and safe dedupe/textContent."""
        with TestClient(backend.app) as client:
            res_html = client.get("/")
            self.assertEqual(res_html.status_code, 200)
            self.assertIn("transcript-card", res_html.text)
            self.assertIn("transcript-container", res_html.text)
            self.assertIn("transcript-messages", res_html.text)

            res_css = client.get("/static/style.css")
            self.assertEqual(res_css.status_code, 200)
            self.assertIn(".transcript-card", res_css.text)
            self.assertIn(".transcript-msg-agent", res_css.text)
            self.assertIn(".transcript-msg-prospect", res_css.text)

            res_js = client.get("/static/app.js")
            self.assertEqual(res_js.status_code, 200)
            js_text = res_js.text
            self.assertIn("callTranscripts", js_text)
            self.assertIn("addTranscriptItems", js_text)
            self.assertIn("renderTranscript", js_text)
            self.assertIn("transcript.final", js_text)
            # Ensure textContent is used for transcript text, not innerHTML
            self.assertIn("textDiv.textContent = item.text", js_text)


if __name__ == "__main__":
    unittest.main()

