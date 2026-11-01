import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
from dial import VIDEOSDK_TOKEN, dial

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "day1"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

FIXTURES_DAY2_DIR = Path(__file__).parent / "fixtures" / "day2"
FIXTURES_DAY2_DIR.mkdir(parents=True, exist_ok=True)

FIXTURES_DAY3_DIR = Path(__file__).parent / "fixtures" / "day3"
FIXTURES_DAY3_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_PATH = STATIC_DIR / "index.html"

# Dispatcher & Poller state
dispatch_paused: bool = False
dispatcher_task: Optional[asyncio.Task] = None
poller_task: Optional[asyncio.Task] = None

ENABLE_DISPATCHER: bool = True
ENABLE_POLLER: bool = True
POLL_INTERVAL_SECONDS: float = 3.0

# SSE wake-up event
_sse_notify_event: Optional[asyncio.Event] = None


def get_sse_notify_event() -> asyncio.Event:
    global _sse_notify_event
    if _sse_notify_event is None:
        _sse_notify_event = asyncio.Event()
    return _sse_notify_event


def notify_call_updated(
    call_id: str,
    new_status: Optional[str] = None,
    worker_state: Optional[str] = None,
) -> None:
    db.record_call_updated_event(
        call_id,
        new_status=new_status,
        worker_state=worker_state,
    )
    if _sse_notify_event is not None:
        _sse_notify_event.set()


def normalize_provider_status(
    raw_status: str,
    timelog: Optional[List[Dict[str, Any]]] = None,
    call_id: Optional[str] = None,
) -> Optional[str]:
    if not raw_status:
        return None
    s = str(raw_status).strip().lower()
    if s == "initiated":
        return "dialing"
    elif s == "ringing":
        return "ringing"
    elif s in ("in-progress", "in_progress", "ongoing", "connected", "answered"):
        return "connected"
    elif s in ("completed", "ended", "terminated", "hangup"):
        if timelog is not None:
            if not timelog:
                logger.warning(
                    "Call %s: provider status is '%s' but data.timelog is missing/empty; defaulting to completed",
                    call_id or "unknown",
                    raw_status,
                )
                return "completed"
            timelog_statuses = [
                str(entry.get("status", "")).strip().lower()
                for entry in timelog
                if isinstance(entry, dict)
            ]
            if "answered" in timelog_statuses:
                return "completed"
            elif "missed" in timelog_statuses:
                return "no_answer"
            else:
                return "failed"
        return "completed"
    elif s == "busy":
        return "busy"
    elif s in ("no-answer", "no_answer", "unanswered", "missed"):
        return "no_answer"
    elif s in ("failed", "canceled", "cancelled", "rejected"):
        return "failed"
    return None


def resolve_call_status_and_error(
    raw_status: str,
    timelog: Optional[List[Dict[str, Any]]] = None,
    call_id: Optional[str] = None,
    current_status: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if not raw_status:
        return None, None
    s = str(raw_status).strip().lower()

    # Phase 3b: Special handling when status is hangup_requested
    if current_status == "hangup_requested":
        # Any terminal/ended/missed status resolves to completed with dashboard reason
        if s in ("completed", "ended", "terminated", "hangup", "missed", "no-answer", "no_answer", "unanswered"):
            return "completed", "Hung up from dashboard"
        elif s in ("failed", "canceled", "cancelled", "rejected"):
            return "failed", "Call failed after hangup request"
        elif s == "busy":
            return "busy", "Busy"
        elif s in ("initiated", "ringing", "in-progress", "in_progress", "ongoing", "connected", "answered"):
            # Non-terminal: preserve hangup_requested
            return "hangup_requested", None

    if s in ("completed", "ended", "terminated", "hangup"):
        if not timelog:
            logger.warning(
                "Call %s: provider status is '%s' but data.timelog is missing/empty; defaulting to completed",
                call_id or "unknown",
                raw_status,
            )
            return "completed", None

        timelog_statuses = [
            str(entry.get("status", "")).strip().lower()
            for entry in timelog
            if isinstance(entry, dict)
        ]
        if "answered" in timelog_statuses:
            return "completed", None
        elif "missed" in timelog_statuses:
            missed_entry = next(
                (
                    e
                    for e in timelog
                    if isinstance(e, dict)
                    and str(e.get("status", "")).strip().lower() == "missed"
                ),
                None,
            )
            provider_msg = (
                missed_entry.get("message") if missed_entry else None
            ) or "status 480 Callee currently unavailable"
            return "no_answer", provider_msg
        else:
            last_msg = None
            for e in reversed(timelog):
                if isinstance(e, dict) and e.get("message"):
                    last_msg = e.get("message")
                    break
            logger.warning(
                "Call %s: ended with neither answered nor missed in timelog. Real timelog statuses: %s. Last message: %s",
                call_id or "unknown",
                timelog_statuses,
                last_msg,
            )
            return "failed", last_msg or "Call ended without answered or missed status"
    elif s in ("no-answer", "no_answer", "unanswered", "missed"):
        missed_entry = next(
            (
                e
                for e in (timelog or [])
                if isinstance(e, dict)
                and str(e.get("status", "")).strip().lower() == "missed"
            ),
            None,
        )
        provider_msg = missed_entry.get("message") if missed_entry else None
        return "no_answer", provider_msg
    else:
        return normalize_provider_status(raw_status, timelog=timelog, call_id=call_id), None


async def dispatch_one_call() -> bool:
    global dispatch_paused
    if dispatch_paused:
        return False

    if db.is_any_call_in_flight():
        return False

    queued_call = db.get_oldest_queued_call()
    if not queued_call:
        return False

    call_id = queued_call["id"]
    phone_number = queued_call["phone_number"]

    logger.info("Dispatching call %s to %s...", call_id, phone_number)
    db.update_call(call_id, status="dialing")
    notify_call_updated(call_id, "dialing")

    # Safety Guard: refuse real dialing unless explicitly enabled
    allow_real_dial = os.getenv("PODHUB_ALLOW_REAL_DIAL", "0").strip() == "1"
    if not allow_real_dial:
        logger.error(
            "PODHUB_ALLOW_REAL_DIAL != 1. Refusing to place real call for %s. Marking failed.",
            call_id,
        )
        db.update_call(
            call_id,
            status="failed",
            error_message="Real dialing disabled (PODHUB_ALLOW_REAL_DIAL != 1)",
        )
        notify_call_updated(call_id, "failed")
        return True

    try:
        # Run synchronous dial() in a threadpool outside of DB transactions
        result = await asyncio.to_thread(dial, phone_number)
    except requests.HTTPError as exc:
        resp = exc.response
        status_code = resp.status_code if resp is not None else 500
        resp_text = resp.text if resp is not None else str(exc)

        if 400 <= status_code < 500:
            # Explicit provider rejection (e.g. invalid number, routing rule misconfig)
            logger.error("Provider explicitly rejected dial for %s: %s %s", call_id, status_code, resp_text)
            db.update_call(call_id, status="failed", error_message=f"Provider rejected ({status_code}): {resp_text}")
            notify_call_updated(call_id, "failed")
        else:
            # 5xx ambiguous error
            logger.error("Provider returned server error %s for %s. Pausing dispatch.", status_code, call_id)
            db.update_call(call_id, status="unknown", error_message=f"Provider error ({status_code}): {resp_text}")
            notify_call_updated(call_id, "unknown")
            dispatch_paused = True
        return True
    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.error("Ambiguous network error/timeout during dial for %s: %s. Pausing dispatch.", call_id, exc)
        db.update_call(call_id, status="unknown", error_message=f"Network error/timeout: {exc}")
        notify_call_updated(call_id, "unknown")
        dispatch_paused = True
        return True
    except Exception as exc:
        logger.error("Unexpected error during dial for %s: %s. Pausing dispatch.", call_id, exc)
        db.update_call(call_id, status="unknown", error_message=f"Unexpected error: {exc}")
        notify_call_updated(call_id, "unknown")
        dispatch_paused = True
        return True

    # Dial succeeded with HTTP response
    raw_body = result.get("raw", {})
    provider_call_id = result.get("provider_call_id")
    room_id = result.get("room_id")

    # Save clean dial fixture body (strip headers/tokens)
    try:
        with open(FIXTURES_DIR / "dial_response.json", "w", encoding="utf-8") as f:
            json.dump(raw_body, f, indent=2)
    except Exception as exc:
        logger.error("Failed to write dial_response.json fixture: %s", exc)

    if not provider_call_id or not room_id:
        logger.error("Dial response missing callId or roomId for call %s: %s", call_id, raw_body)
        update_fields = {
            "status": "unknown",
            "error_message": "Dial response missing callId or roomId",
            "provider_raw_json": json.dumps(raw_body),
        }
        if room_id:
            update_fields["room_id"] = room_id
        if provider_call_id:
            update_fields["provider_call_id"] = provider_call_id
        db.update_call(call_id, **update_fields)
        notify_call_updated(call_id, "unknown")
        dispatch_paused = True
    else:
        logger.info("Dial succeeded for %s: provider_call_id=%s, room_id=%s", call_id, provider_call_id, room_id)
        db.update_call(
            call_id,
            provider_call_id=provider_call_id,
            room_id=room_id,
            status="dialing",
            provider_raw_json=json.dumps(raw_body),
        )
        notify_call_updated(call_id, "dialing")
    return True


async def dispatcher_loop():
    logger.info("Dispatcher loop started.")
    while True:
        try:
            await asyncio.sleep(1.0)
            await dispatch_one_call()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Unhandled error in dispatcher loop: %s", exc)


async def poll_active_calls() -> None:
    active_calls = db.get_active_calls()
    for call in active_calls:
        provider_call_id = call.get("provider_call_id")
        if not provider_call_id:
            continue

        call_id = call["id"]
        current_status = call["status"]
        url = f"https://api.videosdk.live/v2/sip/call/{provider_call_id}"
        headers = {"Authorization": VIDEOSDK_TOKEN}

        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        except Exception as exc:
            logger.error("Poll request failed for call %s (%s): %s", call_id, provider_call_id, exc)
            continue

        if resp.status_code != 200:
            logger.error(
                "Poll returned non-200 (%s) for call %s (%s): %s",
                resp.status_code,
                call_id,
                provider_call_id,
                resp.text,
            )
            continue

        try:
            raw_body = resp.json()
        except Exception as exc:
            logger.error("Failed to parse poll JSON for call %s: %s", call_id, exc)
            continue

        # 200 OK: Update last_provider_poll_at and provider_raw_json
        now_iso = db.utc_now_iso()
        raw_json_str = json.dumps(raw_body)
        db.update_call(call_id, last_provider_poll_at=now_iso, provider_raw_json=raw_json_str)

        data = raw_body.get("data", raw_body)
        raw_status = data.get("status")
        timelog = data.get("timelog")
        if not isinstance(timelog, list):
            timelog = None

        if raw_status:
            safe_status = re.sub(r"[^a-zA-Z0-9_\-]", "", str(raw_status))
            fixture_file = FIXTURES_DAY2_DIR / f"poll_{safe_status}.json"
            if not fixture_file.exists():
                try:
                    with open(fixture_file, "w", encoding="utf-8") as f:
                        json.dump(raw_body, f, indent=2)
                    logger.info("Saved fixture for new raw status '%s' to %s", safe_status, fixture_file)
                except Exception as exc:
                    logger.error("Failed to write fixture %s: %s", fixture_file, exc)

            # Fixtures for timelog statuses
            if timelog:
                for entry in timelog:
                    if not isinstance(entry, dict):
                        continue
                    t_status = entry.get("status")
                    if not t_status:
                        continue
                    safe_t = re.sub(r"[^a-zA-Z0-9_\-]", "", str(t_status))
                    if not safe_t:
                        continue

                    # For ended call with sub-statuses (e.g. missed, answered, busy, rejected)
                    if safe_status == "ended" and safe_t not in ("initiated", "ringing", "ended"):
                        ended_fixture_file = FIXTURES_DAY2_DIR / f"poll_ended_{safe_t}.json"
                        if not ended_fixture_file.exists():
                            try:
                                with open(ended_fixture_file, "w", encoding="utf-8") as f:
                                    json.dump(raw_body, f, indent=2)
                                logger.info(
                                    "Saved fixture for ended call with timelog status '%s' to %s",
                                    safe_t,
                                    ended_fixture_file,
                                )
                            except Exception as exc:
                                logger.error("Failed to write fixture %s: %s", ended_fixture_file, exc)

                    # Also save standalone fixture for new timelog status
                    t_fixture_file = FIXTURES_DAY2_DIR / f"poll_{safe_t}.json"
                    if not t_fixture_file.exists():
                        try:
                            with open(t_fixture_file, "w", encoding="utf-8") as f:
                                json.dump(raw_body, f, indent=2)
                            logger.info(
                                "Saved fixture for new timelog status '%s' to %s", safe_t, t_fixture_file
                            )
                        except Exception as exc:
                            logger.error("Failed to write fixture %s: %s", t_fixture_file, exc)

            # Race condition fix: re-read latest status from DB right before evaluating transition
            fresh_call = db.get_call_by_id(call_id)
            if not fresh_call:
                continue
            current_status = fresh_call["status"]

            normalized, err_msg = resolve_call_status_and_error(
                raw_status, timelog=timelog, call_id=call_id, current_status=current_status
            )

            # Poller must NEVER overwrite hangup_requested with a non-terminal status
            if current_status == "hangup_requested" and normalized in ("dialing", "ringing", "connected", "hangup_requested", None):
                logger.info(
                    "Call %s is in hangup_requested; ignoring non-terminal provider status '%s'",
                    call_id,
                    raw_status,
                )
                continue

            if normalized is None:
                # Rule 1: Do NOT change status. Log warning, raw response already saved.
                logger.warning(
                    "Unrecognized provider status '%s' for call %s; keeping status '%s' unchanged",
                    raw_status,
                    call_id,
                    current_status,
                )
            elif normalized != current_status:
                logger.info(
                    "Call %s status updated: %s -> %s (raw: %s)",
                    call_id,
                    current_status,
                    normalized,
                    raw_status,
                )
                update_kwargs: Dict[str, Any] = {"status": normalized}
                if normalized in ("completed", "busy", "no_answer", "failed"):
                    update_kwargs["ended_at"] = now_iso
                if err_msg:
                    update_kwargs["error_message"] = err_msg
                db.update_call(call_id, **update_kwargs)
                notify_call_updated(call_id, normalized)


async def poller_loop():
    logger.info("Poller loop started.")
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            await poll_active_calls()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Unhandled error in poller loop: %s", exc)


def cleanup_stale_queued_calls(max_age_seconds: float = 120.0) -> int:
    now = datetime.now(timezone.utc)
    now_iso = db.utc_now_iso()
    queued_calls = db.get_all_queued_calls()
    cleaned = 0

    for call in queued_calls:
        created_str = call.get("created_at")
        is_stale = False
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age = (now - created_dt).total_seconds()
                if age > max_age_seconds:
                    is_stale = True
            except Exception:
                is_stale = True
        else:
            is_stale = True

        if is_stale:
            logger.warning(
                "Skipping stale queued call %s on startup (created_at: %s, age > %ss)",
                call["id"],
                created_str,
                max_age_seconds,
            )
            db.update_call(
                call["id"],
                status="failed",
                ended_at=now_iso,
                error_message="Stale queued call skipped on startup",
            )
            notify_call_updated(call["id"], "failed")
            cleaned += 1

    return cleaned


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispatcher_task, poller_task, _sse_notify_event
    _sse_notify_event = asyncio.Event()
    db.init_db()
    cleanup_stale_queued_calls(max_age_seconds=120.0)
    if ENABLE_DISPATCHER:
        dispatcher_task = asyncio.create_task(dispatcher_loop())
    if ENABLE_POLLER:
        poller_task = asyncio.create_task(poller_loop())
    yield
    if dispatcher_task:
        dispatcher_task.cancel()
        try:
            await dispatcher_task
        except asyncio.CancelledError:
            pass
    if poller_task:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


class CreateCallRequest(BaseModel):
    phone_number: str
    idempotency_key: str


class EventRequest(BaseModel):
    event_key: str
    type: str
    payload: Dict[str, Any]


PHONE_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    if not INDEX_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Dashboard template not found"},
        )
    return HTMLResponse(content=INDEX_PATH.read_text(encoding="utf-8"))


@app.post("/calls", status_code=status.HTTP_202_ACCEPTED)
async def post_calls(req: CreateCallRequest, response: Response):
    # Validation
    cleaned_phone = req.phone_number.strip()
    if not PHONE_REGEX.match(cleaned_phone):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"Invalid international phone number format: {req.phone_number}"},
        )

    existing = db.get_call_by_idempotency_key(req.idempotency_key)
    if existing:
        if existing["phone_number"] == cleaned_phone:
            response.status_code = status.HTTP_200_OK
            return {
                "id": existing["id"],
                "status": existing["status"],
                "replayed": True,
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "Idempotency key reused with different phone number"},
            )

    call_id = f"c_{uuid.uuid4().hex[:12]}"
    created = db.create_call(call_id, req.idempotency_key, cleaned_phone)
    notify_call_updated(call_id, "queued")
    return {
        "id": created["id"],
        "status": created["status"],
        "replayed": False,
    }


@app.post("/calls/{call_id}/hangup")
async def post_hangup(call_id: str, response: Response):
    call = db.get_call_by_id(call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Call not found"},
        )

    current_st = call["status"]

    # Rule: If call is already hangup_requested, do NOT hit provider again; return 200 with current status
    if current_st == "hangup_requested":
        response.status_code = status.HTTP_200_OK
        return {"id": call["id"], "status": current_st}

    # Rule: Hang Up on an already-finished call returns 200 with its current status
    if current_st in ("completed", "busy", "no_answer", "failed", "unknown"):
        response.status_code = status.HTTP_200_OK
        return {"id": call["id"], "status": current_st}

    provider_call_id = call.get("provider_call_id")
    # Rule: queued or missing provider ID returns 409
    if current_st == "queued" or not provider_call_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Call is still queued or has no provider ID"},
        )

    # Active call: dialing, ringing, connected
    url = "https://api.videosdk.live/v2/sip/call/end"
    headers = {
        "Authorization": VIDEOSDK_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"callId": provider_call_id}

    logger.info("Sending hangup request to VideoSDK for call %s (provider_call_id: %s)...", call_id, provider_call_id)
    try:
        resp = await asyncio.to_thread(
            requests.post, url, json=payload, headers=headers, timeout=15
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.error("Hangup request timed out or network error for call %s (%s): %s", call_id, provider_call_id, exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "Termination request timed out"},
        )
    except Exception as exc:
        logger.error("Unexpected error during hangup for call %s (%s): %s", call_id, provider_call_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": f"Provider error during hangup: {exc}"},
        )

    if resp.status_code >= 400:
        logger.error(
            "Provider rejected hangup for call %s (%s): %s %s",
            call_id,
            provider_call_id,
            resp.status_code,
            resp.text,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": f"Provider rejected hangup request ({resp.status_code}): {resp.text}"},
        )

    try:
        raw_body = resp.json()
    except Exception:
        raw_body = {"status": "ok", "statusCode": resp.status_code}

    # Save fixture of first real provider hangup response
    fixture_file = FIXTURES_DAY3_DIR / "hangup_response.json"
    if not fixture_file.exists():
        try:
            with open(fixture_file, "w", encoding="utf-8") as f:
                json.dump(raw_body, f, indent=2)
            logger.info("Saved hangup fixture to %s", fixture_file)
        except Exception as exc:
            logger.error("Failed to write hangup fixture %s: %s", fixture_file, exc)

    # Provider accepted termination
    db.update_call(call_id, status="hangup_requested")
    notify_call_updated(call_id, "hangup_requested")
    response.status_code = status.HTTP_202_ACCEPTED
    return {"id": call_id, "status": "hangup_requested"}


@app.get("/calls/stream")
async def get_calls_stream(
    request: Request,
    after: Optional[int] = Query(None),
):
    last_event_id_header = request.headers.get("Last-Event-ID")
    cursor = 0
    if after is not None:
        cursor = after
    elif last_event_id_header:
        try:
            cursor = int(last_event_id_header)
        except ValueError:
            cursor = 0

    async def event_generator():
        nonlocal cursor
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break

            events = db.get_events_after(
                cursor, event_types=["call.updated", "transcript.final"], limit=50
            )
            if events:
                for ev in events:
                    cursor = ev["id"]
                    if ev["type"] == "transcript.final":
                        try:
                            payload = json.loads(ev["payload_json"])
                        except Exception:
                            payload = {}
                        payload["call_id"] = ev["call_id"]
                        payload["event_id"] = ev["id"]
                        yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(payload)}\n\n"
                    else:
                        yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {ev['payload_json']}\n\n"
                continue

            try:
                evt = get_sse_notify_event()
                evt.clear()
                await asyncio.wait_for(evt.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/calls")
async def get_calls(
    limit: int = Query(50, ge=1, le=100),
    before: Optional[str] = None,
    room_id: Optional[str] = None,
    in_flight: Optional[bool] = None,
):
    calls, next_before, event_cursor = db.get_calls(
        limit=limit, before=before, room_id=room_id, in_flight=in_flight
    )
    return {
        "calls": calls,
        "next_before": next_before,
        "event_cursor": event_cursor,
    }


@app.get("/calls/{call_id}")
async def get_call(call_id: str):
    call = db.get_call_by_id(call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Call not found"},
        )

    transcript = db.get_call_transcript(call_id)
    _, _, event_cursor = db.get_calls(limit=1)
    call["transcript"] = transcript
    call["event_cursor"] = event_cursor
    return call


ALLOWED_WORKER_EVENT_TYPES = {
    "transcript.final",
    "worker.started",
    "worker.heartbeat",
    "worker.stopped",
    "worker.error",
}


@app.post("/internal/calls/{call_id}/events")
async def post_internal_event(call_id: str, req: EventRequest, response: Response):
    call = db.get_call_by_id(call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Unknown call ID"},
        )

    if req.type not in ALLOWED_WORKER_EVENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"Invalid event type: {req.type}"},
        )

    payload_json = json.dumps(req.payload, sort_keys=True)
    event_id, is_duplicate, is_conflict = db.save_call_event(
        call_id=call_id,
        event_key=req.event_key,
        event_type=req.type,
        payload_json=payload_json,
    )

    if is_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Same event_key submitted with conflicting payload"},
        )

    # Update call record based on event
    now = db.utc_now_iso()
    if req.type == "worker.started":
        db.update_call(call_id, worker_state="active", last_heartbeat_at=now)
    elif req.type == "worker.heartbeat":
        db.update_call(call_id, worker_state="active", last_heartbeat_at=now)
    elif req.type == "worker.stopped":
        db.update_call(call_id, worker_state="stopped")
    elif req.type == "worker.error":
        err_msg = str(req.payload.get("message", "Unknown error"))
        db.update_call(call_id, error_message=err_msg)

    if is_duplicate:
        response.status_code = status.HTTP_200_OK
        return {"event_id": event_id, "accepted": True}

    # Notify SSE listeners on newly saved transcript.final
    if req.type == "transcript.final":
        evt = get_sse_notify_event()
        evt.set()

    # Emit call.updated for worker lifecycle changes (not heartbeat)
    if req.type in ("worker.started", "worker.stopped", "worker.error"):
        latest_call = db.get_call_by_id(call_id)
        latest_status = latest_call["status"] if latest_call else call["status"]
        latest_worker_state = latest_call.get("worker_state") if latest_call else None
        notify_call_updated(
            call_id,
            new_status=latest_status,
            worker_state=latest_worker_state,
        )

    response.status_code = status.HTTP_201_CREATED
    return {"event_id": event_id, "accepted": True}
