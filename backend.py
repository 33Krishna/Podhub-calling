import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

import db
from dial import dial

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "day1"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

# Dispatcher state
dispatch_paused: bool = False
dispatcher_task: Optional[asyncio.Task] = None


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
        else:
            # 5xx ambiguous error
            logger.error("Provider returned server error %s for %s. Pausing dispatch.", status_code, call_id)
            db.update_call(call_id, status="unknown", error_message=f"Provider error ({status_code}): {resp_text}")
            dispatch_paused = True
        return True
    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.error("Ambiguous network error/timeout during dial for %s: %s. Pausing dispatch.", call_id, exc)
        db.update_call(call_id, status="unknown", error_message=f"Network error/timeout: {exc}")
        dispatch_paused = True
        return True
    except Exception as exc:
        logger.error("Unexpected error during dial for %s: %s. Pausing dispatch.", call_id, exc)
        db.update_call(call_id, status="unknown", error_message=f"Unexpected error: {exc}")
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


ENABLE_DISPATCHER: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispatcher_task
    db.init_db()
    if ENABLE_DISPATCHER:
        dispatcher_task = asyncio.create_task(dispatcher_loop())
    yield
    if dispatcher_task:
        dispatcher_task.cancel()
        try:
            await dispatcher_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class CreateCallRequest(BaseModel):
    phone_number: str
    idempotency_key: str


class EventRequest(BaseModel):
    event_key: str
    type: str
    payload: Dict[str, Any]


PHONE_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")


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
    return {
        "id": created["id"],
        "status": created["status"],
        "replayed": False,
    }


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

    response.status_code = status.HTTP_201_CREATED
    return {"event_id": event_id, "accepted": True}
