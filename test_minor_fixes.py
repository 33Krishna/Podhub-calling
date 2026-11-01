import test_support

import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import backend
from worker_bridge import WorkerBridge, normalize_transcript


class TestMinorFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ENABLE_DISPATCHER = False
        backend.ENABLE_POLLER = False

    def test_static_cache_control_header(self):
        """Verify responses under /static include Cache-Control: no-cache header."""
        client = TestClient(backend.app)

        res_css = client.get("/static/style.css")
        self.assertEqual(res_css.status_code, 200)
        self.assertIn("cache-control", res_css.headers)
        self.assertEqual(res_css.headers["cache-control"], "no-cache")

        res_js = client.get("/static/app.js")
        self.assertEqual(res_js.status_code, 200)
        self.assertIn("cache-control", res_js.headers)
        self.assertEqual(res_js.headers["cache-control"], "no-cache")

    def test_favicon_in_head(self):
        """Verify static/index.html includes inline data URI favicon link."""
        client = TestClient(backend.app)
        res_html = client.get("/")
        self.assertEqual(res_html.status_code, 200)
        self.assertIn('<link rel="icon" href="data:,">', res_html.text)

    def test_normalize_transcript_helper(self):
        """Verify normalize_transcript converts to lowercase and strips punctuation and extra whitespace."""
        raw = "  Hi, Good Morning!   Main Ananya  bol rahi hoon...  "
        norm = normalize_transcript(raw)
        self.assertEqual(norm, "hi good morning main ananya bol rahi hoon")

    def test_duplicate_greeting_suppression_exact_and_prefix(self):
        """
        Verify worker_bridge remembers explicit greeting and suppresses model transcription
        within 60s if normalized text matches or is a prefix.
        """
        bridge = WorkerBridge(backend_url="http://127.0.0.1:8000")
        dispatched_events = []
        bridge._dispatch_event = lambda event: dispatched_events.append(event)

        greeting_text = (
            "Hi, good evening! Main Ananya bol rahi hoon PodHub Mumbai se. "
            "Main ek AI assistant hoon. Main sirf do minute lungi. "
            "Abhi baat kar sakte hain?"
        )

        # 1. Explicit greeting sent by worker on_enter
        bridge.send_transcript(speaker="agent", text=greeting_text)
        self.assertEqual(len(dispatched_events), 1)
        self.assertEqual(dispatched_events[0]["payload"]["text"], greeting_text)
        self.assertIsNotNone(bridge.last_greeting_norm)
        self.assertIsNotNone(bridge.last_greeting_time)

        # 2. Duplicate exact greeting from realtime model transcription -> skipped!
        exact_echo = "Hi, good evening! Main Ananya bol rahi hoon PodHub Mumbai se. Main ek AI assistant hoon. Main sirf do minute lungi. Abhi baat kar sakte hain?"
        bridge.on_transcription({"role": "model", "is_final": True, "text": exact_echo})
        self.assertEqual(len(dispatched_events), 1, "Exact echo must be skipped")

        # 3. Duplicate partial prefix from model transcription -> skipped!
        prefix_echo = "Hi, good evening! Main Ananya bol rahi hoon PodHub Mumbai se..."
        bridge.on_transcription({"role": "model", "is_final": True, "text": prefix_echo})
        self.assertEqual(len(dispatched_events), 1, "Prefix echo must be skipped")

        # 4. Model transcription where greeting is prefix of model speech -> skipped!
        superset_echo = greeting_text + " Haan ji."
        bridge.on_transcription({"role": "model", "is_final": True, "text": superset_echo})
        self.assertEqual(len(dispatched_events), 1, "Superset echo must be skipped")

        # 5. Distinct subsequent agent response -> NOT skipped!
        agent_reply = "Aapka package pricing details main WhatsApp pe bhej rahi hoon."
        bridge.on_transcription({"role": "model", "is_final": True, "text": agent_reply})
        self.assertEqual(len(dispatched_events), 2, "Distinct agent speech must be dispatched")
        self.assertEqual(dispatched_events[1]["payload"]["text"], agent_reply)

        # 6. Prospect speech -> NOT skipped!
        prospect_speech = "Haan bataiye, kya pricing hai?"
        bridge.on_transcription({"role": "user", "is_final": True, "text": prospect_speech})
        self.assertEqual(len(dispatched_events), 3, "Prospect speech must be dispatched")
        self.assertEqual(dispatched_events[2]["payload"]["speaker"], "prospect")

    def test_duplicate_greeting_expired_after_60_seconds(self):
        """Verify duplicate greeting check expires after 60 seconds."""
        bridge = WorkerBridge(backend_url="http://127.0.0.1:8000")
        dispatched_events = []
        bridge._dispatch_event = lambda event: dispatched_events.append(event)

        greeting = "Hi, good evening!"
        bridge.send_transcript(speaker="agent", text=greeting)
        self.assertEqual(len(dispatched_events), 1)

        # Fake the greeting timestamp to 65 seconds in the past
        bridge.last_greeting_time = time.time() - 65.0

        # Now model transcription with the same text is NOT skipped
        bridge.on_transcription({"role": "model", "is_final": True, "text": greeting})
        self.assertEqual(len(dispatched_events), 2, "Speech after 60s must not be suppressed")


if __name__ == "__main__":
    unittest.main()

