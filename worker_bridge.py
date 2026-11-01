import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx

logger = logging.getLogger("worker_bridge")
RAW_LOG_PATH = Path(__file__).parent / "transcript_raw.log"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_transcript(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class WorkerBridge:
    def __init__(self, backend_url: str = "http://127.0.0.1:8000"):
        self.backend_url = backend_url.rstrip("/")
        self.call_id: Optional[str] = None
        self.room_id: Optional[str] = None
        self.seq: int = 0
        self.client: Optional[httpx.AsyncClient] = None
        self.pending_tasks: Set[asyncio.Task] = set()
        self.buffered_events: List[Dict[str, Any]] = []
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running: bool = True
        self._lock = asyncio.Lock()
        self.last_greeting_norm: Optional[str] = None
        self.last_greeting_time: Optional[float] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(timeout=5.0)
        return self.client

    def log_raw_transcript(self, role: Any, is_final: Any, text: Any) -> None:
        try:
            line = f"[{utc_now_iso()}] role={role!r} is_final={is_final!r} text={text!r}\n"
            with open(RAW_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            logger.error("Failed to write to raw transcript log: %s", exc)

    def is_duplicate_greeting(self, text: str) -> bool:
        if not self.last_greeting_norm or self.last_greeting_time is None:
            return False
        if time.time() - self.last_greeting_time > 60.0:
            return False
        incoming_norm = normalize_transcript(text)
        if not incoming_norm:
            return False
        if incoming_norm == self.last_greeting_norm:
            return True
        if incoming_norm.startswith(self.last_greeting_norm) or self.last_greeting_norm.startswith(incoming_norm):
            return True
        return False

    def on_transcription(self, data: Any) -> None:
        """
        Synchronous callback for realtime.on('realtime_model_transcription', ...)
        Must never block audio or raise unhandled exceptions.
        """
        try:
            if not isinstance(data, dict):
                return

            role = data.get("role", "")
            is_final = data.get("is_final", False)
            text = (data.get("text") or "").strip()

            # Requirement: log EVERY event before is_final filter
            self.log_raw_transcript(role, is_final, text)

            if not is_final or not text:
                return

            speaker = "prospect" if role == "user" else "agent"
            if speaker == "agent" and self.is_duplicate_greeting(text):
                logger.info("Skipping duplicate greeting transcription: %r", text)
                return

            self.send_transcript(speaker=speaker, text=text, from_realtime=True)
        except Exception as exc:
            logger.error("Error in on_transcription callback: %s", exc)

    def send_transcript(self, speaker: str, text: str, from_realtime: bool = False) -> None:
        """
        Explicitly dispatch a final transcript segment (e.g. initial greeting).
        """
        try:
            cleaned = (text or "").strip()
            if not cleaned:
                return

            # Track explicit agent greeting for 60s duplicate suppression
            if not from_realtime and speaker == "agent":
                self.last_greeting_norm = normalize_transcript(cleaned)
                self.last_greeting_time = time.time()

            self.seq += 1
            event_data = {
                "type": "transcript.final",
                "seq_num": self.seq,
                "payload": {"speaker": speaker, "text": cleaned},
            }
            self._dispatch_event(event_data)
        except Exception as exc:
            logger.error("Error in send_transcript: %s", exc)

    def on_realtime_error(self, err: Any) -> None:
        """
        Synchronous callback for realtime.on('error', ...)
        """
        try:
            msg = str(err)[:200]
            if "Receive loop cancelled" in msg:
                logger.info("Realtime receive loop cancelled (normal session shutdown)")
                return

            logger.error("Realtime error caught: %s", msg)
            self._dispatch_event({
                "type": "worker.error",
                "seq_num": self.seq,
                "payload": {"message": msg},
            })
        except Exception as exc:
            logger.error("Error in on_realtime_error callback: %s", exc)

    def _dispatch_event(self, event_data: Dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running asyncio event loop; buffering event")
            self.buffered_events.append(event_data)
            return

        task = loop.create_task(self._async_send_event(event_data))
        self.pending_tasks.add(task)
        task.add_done_callback(self.pending_tasks.discard)

    async def _async_send_event(self, event_data: Dict[str, Any]) -> None:
        try:
            # If call_id not resolved yet, buffer it
            if not self.call_id:
                async with self._lock:
                    if not self.call_id:
                        self.buffered_events.append(event_data)
                        return

            event_type = event_data["type"]
            seq_num = event_data.get("seq_num", 0)
            event_key = f"{self.call_id}-{event_type}-{seq_num}"

            body = {
                "event_key": event_key,
                "type": event_type,
                "payload": event_data["payload"],
            }
            url = f"{self.backend_url}/internal/calls/{self.call_id}/events"
            client = self._get_client()
            resp = await client.post(url, json=body)
            if resp.status_code not in (200, 201):
                logger.warning(
                    "Backend returned %s for event %s: %s",
                    resp.status_code,
                    event_key,
                    resp.text,
                )
        except Exception as exc:
            logger.error("Failed to deliver event over HTTP: %s", exc)

    async def start(self, room_id: Optional[str]) -> None:
        """
        Resolves internal call_id from backend and starts the heartbeat loop.
        """
        try:
            if not room_id:
                logger.error(
                    "CRITICAL: ctx.room_options.room_id is None! Cannot map worker session to dashboard call record."
                )
                return

            self.room_id = room_id
            logger.info("Resolving internal call_id for room_id: %s", room_id)

            resolved = await self._resolve_call_id(room_id)
            if not resolved:
                logger.error(
                    "CRITICAL: Failed to resolve call_id for room_id %s after all retries! Bridge events cannot be delivered.",
                    room_id,
                )
                return

            self.call_id = resolved
            logger.info("Successfully bound worker bridge to call_id: %s", self.call_id)

            # Flush any early buffered events
            async with self._lock:
                to_flush = list(self.buffered_events)
                self.buffered_events.clear()

            for evt in to_flush:
                task = asyncio.create_task(self._async_send_event(evt))
                self.pending_tasks.add(task)
                task.add_done_callback(self.pending_tasks.discard)

            # Send worker.started
            await self._send_simple_event("worker.started", {})

            # Start heartbeat loop
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        except Exception as exc:
            logger.error("Error in WorkerBridge.start(): %s", exc)

    async def _resolve_call_id(self, room_id: str, max_retries: int = 10, delay: float = 1.0) -> Optional[str]:
        client = self._get_client()
        url = f"{self.backend_url}/calls"
        params = {"room_id": room_id, "in_flight": "true"}

        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    calls = data.get("calls", [])
                    if len(calls) == 1:
                        return calls[0]["id"]
                    elif len(calls) > 1:
                        logger.warning(
                            "Expected exactly 1 call for room %s, but found %d. Choosing latest.",
                            room_id,
                            len(calls),
                        )
                        return calls[0]["id"]
                logger.info("Waiting for room %s call mapping (attempt %d/%d)...", room_id, attempt, max_retries)
            except Exception as exc:
                logger.error("HTTP error while resolving call_id: %s", exc)

            await asyncio.sleep(delay)
        return None

    async def _send_simple_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not self.call_id:
            return
        self.seq += 1
        event_key = f"{self.call_id}-{event_type}-{self.seq}"
        url = f"{self.backend_url}/internal/calls/{self.call_id}/events"
        body = {"event_key": event_key, "type": event_type, "payload": payload}
        client = self._get_client()
        await client.post(url, json=body)

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(5.0)
                if not self._running:
                    break
                try:
                    await self._send_simple_event("worker.heartbeat", {})
                except Exception as exc:
                    logger.error("Error sending heartbeat: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Heartbeat loop crashed: %s", exc)

    async def cleanup_bridge(self) -> None:
        """
        Orderly shutdown callback.
        Stops heartbeat, waits up to 3s for pending HTTP tasks, sends worker.stopped.
        """
        try:
            logger.info("Cleaning up WorkerBridge...")
            self._running = False
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()

            # Wait up to 3 seconds for pending HTTP tasks
            if self.pending_tasks:
                logger.info("Waiting up to 3s for %d pending bridge tasks...", len(self.pending_tasks))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*list(self.pending_tasks), return_exceptions=True),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Timed out waiting for pending bridge event deliveries.")

            # Send worker.stopped
            if self.call_id:
                try:
                    await self._send_simple_event("worker.stopped", {})
                    logger.info("Sent worker.stopped for call %s", self.call_id)
                except Exception as exc:
                    logger.error("Error sending worker.stopped: %s", exc)

            if self.client and not self.client.is_closed:
                await self.client.aclose()
        except Exception as exc:
            logger.error("Error in WorkerBridge.cleanup_bridge(): %s", exc)

