import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BACKEND_URL = "http://127.0.0.1:8000"
VIDEOSDK_TOKEN = os.getenv("VIDEOSDK_TOKEN", "").strip()
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "day1"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


def poll_videosdk_fixture(provider_call_id: str) -> None:
    if not VIDEOSDK_TOKEN:
        print("[FIXTURE] VIDEOSDK_TOKEN missing in environment; cannot fetch poll fixture.")
        return
    poll_file = FIXTURES_DIR / "poll_response.json"
    if poll_file.exists():
        return

    # VideoSDK SIP call details endpoint
    url = f"https://api.videosdk.live/v2/sip/call/{provider_call_id}"
    headers = {"Authorization": VIDEOSDK_TOKEN}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            raw_body = resp.json()
            # Save only the response body (no headers, no auth tokens)
            with open(poll_file, "w", encoding="utf-8") as f:
                json.dump(raw_body, f, indent=2)
            print(f"[FIXTURE] Saved real provider poll response to {poll_file}")
        else:
            # Fallback query parameter variant
            alt_url = f"https://api.videosdk.live/v2/sip/call?callId={provider_call_id}"
            alt_resp = requests.get(alt_url, headers=headers, timeout=10)
            if alt_resp.status_code == 200:
                raw_body = alt_resp.json()
                with open(poll_file, "w", encoding="utf-8") as f:
                    json.dump(raw_body, f, indent=2)
                print(f"[FIXTURE] Saved real provider poll response to {poll_file}")
            else:
                print(f"[FIXTURE] Provider poll returned {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"[FIXTURE] Could not fetch provider poll fixture: {exc}")


def main():
    if len(sys.argv) > 1:
        phone_number = sys.argv[1].strip()
    else:
        phone_number = input("Enter target phone number (e.g. +91XXXXXXXXXX): ").strip()

    if not phone_number.startswith("+"):
        print("ERROR: Phone number must start with '+' and international country code (e.g. +91XXXXXXXXXX).")
        sys.exit(1)

    idempotency_key = f"cli-test-{uuid.uuid4().hex[:10]}"
    print(f"\n==================================================")
    print(f"Triggering Phase 1 test call to {phone_number}")
    print(f"Idempotency Key: {idempotency_key}")
    print(f"==================================================\n")

    try:
        resp = requests.post(
            f"{BACKEND_URL}/calls",
            json={"phone_number": phone_number, "idempotency_key": idempotency_key},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"ERROR: Could not connect to backend at {BACKEND_URL}. Is backend running?")
        print(f"Detail: {exc}")
        sys.exit(1)

    if resp.status_code not in (200, 202):
        print(f"ERROR: Backend returned status {resp.status_code}: {resp.text}")
        sys.exit(1)

    call_data = resp.json()
    call_id = call_data["id"]
    print(f"Call Created! Internal ID: {call_id} (Status: {call_data['status']})")
    print("Monitoring live call events... Press Ctrl+C to stop monitoring.\n")

    seen_event_ids = set()
    poll_attempted = False
    last_status = None
    last_worker_state = None

    try:
        while True:
            time.sleep(2.0)
            try:
                detail_resp = requests.get(f"{BACKEND_URL}/calls/{call_id}", timeout=5)
            except requests.RequestException as exc:
                print(f"[WARN] Failed to poll backend: {exc}")
                continue

            if detail_resp.status_code != 200:
                print(f"[WARN] Backend returned {detail_resp.status_code}")
                continue

            detail = detail_resp.json()
            cur_status = detail.get("status")
            cur_worker = detail.get("worker_state")
            provider_call_id = detail.get("provider_call_id")
            room_id = detail.get("room_id")

            # Status / State change logging
            if cur_status != last_status or cur_worker != last_worker_state:
                print(f"[STATUS UPDATE] Status: {cur_status.upper()} | Worker: {cur_worker.upper()} | Room: {room_id} | Provider Call: {provider_call_id}")
                last_status = cur_status
                last_worker_state = cur_worker

            # Targeted poll fixture
            if provider_call_id and not poll_attempted:
                poll_attempted = True
                poll_videosdk_fixture(provider_call_id)

            # Print new transcript events
            transcript = detail.get("transcript", [])
            for seg in transcript:
                eid = seg.get("event_id")
                if eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    speaker = seg.get("speaker", "UNKNOWN").upper()
                    text = seg.get("text", "")
                    print(f"  [{speaker}]: {text}")

            if cur_status in ("completed", "failed", "busy", "no_answer", "unknown"):
                print(f"\nCall ended with terminal status: {cur_status}")
                if detail.get("error_message"):
                    print(f"Error Message: {detail['error_message']}")
                break

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")


if __name__ == "__main__":
    main()

