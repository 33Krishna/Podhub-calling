# PodHub Calling Dashboard — v1 MVP Plan

**How to read this file:** This is the single source of truth. The "Ground Rules" and "Amendments Summary" below are binding. If anything elsewhere in the file seems to conflict, Ground Rules win.

**Goal:** A local dashboard on top of the existing Python voice agent (`main.py`, `dial.py`, `prompt.md`, VideoSDK). Click "Start Call", watch status and live transcript, hang up, see the outcome.

---

## Ground Rules (non-negotiable)

- **Platform: Windows** (`D:\Coding\PodHub-calling`). No Mac or bash-only commands. Launcher is `start.bat`.
- **Local-first.** Runs on the PM's own PC. No hosted server. No tunnel needed in v1.
- **Single user.** No login, no analytics page, no settings page, no agent versioning UI.
- **Stack:** FastAPI (a Python library for HTTP endpoints), SQLite (one database file), HTML + vanilla JavaScript. No React, Redis, Postgres, queues, or new dependencies without asking.
- **Queue = a `status` column** in the `calls` table. One call at a time.
- **Status updates = polling** the VideoSDK API. No webhooks in v1.
- **No recordings in v1.** "Watch live" means live status + live transcript, not browser audio.
- **VideoSDK dial path is not redesigned.** `dial.py` keeps its URL, auth, routing rule, and request fields.
- **`main.py` and `dial.py`: surgical edits only.** No refactors, no renames.
- **Tests never place real calls** except the explicit real-call checks marked in this plan. Use mocks/fixtures otherwise.
- **v1 = a demoable single-call product.** Handling 200 calls/day is NOT a v1 acceptance criterion (see Section G).
- **Anything beyond scope** must be labelled "OUT OF SCOPE — defer to v2+".

## Amendments Summary (already merged into the sections below)

1. Windows, `start.bat`; Day 1 tests that shutdown/cleanup works on Windows.
2. Seven endpoints (cap relaxed from five). No `action` field. No `Accept`-header switching.
3. Phase 3 split into 3a (transcript) and 3b (Hang Up).
4. Day 1 saves raw JSON fixtures of one dial response and one poll response.
5. `calls.provider_raw_json` column added.
6. Live transcript must be proven in a real call on Day 1, with a named fallback. If it cannot be proven, STOP and tell the PM before building UI.
7. Two explicit tests: invalid phone number must not kill the backend; dial response missing `callId` must become `unknown` with no redial.
8. Skipped on purpose: custom SSE backoff, transcript pagination, worker-kill step in the live demo.

---

# SECTION A — Phase Breakdown

Delivery target: working demo by Day 5, verification Day 6, stakeholder demo Day 7.

## Before Day 1 (checklist, not a phase)

- Git baseline commit of the working code; `.env` / API keys in `.gitignore`.
- Run the existing worker once, outside the dashboard, and confirm a real call still works. This is the baseline.
- Confirm PC access, credentials, and a test phone are available every day. If any slips, the timeline slips 1:1.
- Send the VideoSDK support question (Section G) and note the date.

## Phase 1 — Prove the connection and create storage [DONE]

**Calendar days:** Day 1 — Status: DONE

**What gets built**
- `backend.py`: local FastAPI backend. Disable automatic docs routes (`docs_url=None`, `redoc_url=None`, `openapi_url=None`).
- `db.py` and `schema.sql`: create `calls.sqlite` (schema in Section B).
- `worker_bridge.py`: small helper that lives inside the worker process. It owns only sending events (transcript, heartbeat, errors) to the backend over local HTTP. `main.py` calls it; it does not own call logic or the database.
- `start.bat`: starts the backend and the existing worker on Windows.
- Endpoints: `POST /calls`, `GET /calls`, `GET /calls/{id}`.
- A dispatcher (background loop) that takes the oldest `queued` row and calls the existing `dial()`.
- `fixtures/day1/`: raw JSON of one real dial response and one real provider poll response.
- A temporary command-line test script (written by the coding assistant) to trigger a call.

**What is VERIFIABLY WORKING at end of phase (PM test)**
1. Double-click `start.bat`. Backend and worker start with no errors.
2. Run the temporary test script to call your own phone. Ananya speaks as before.
3. The saved call record contains the provider call ID and room ID.
4. Open `fixtures/day1/`: the dial file contains `callId` and `roomId`; the poll file contains `status`.
5. **Transcript proof:** during that real call, saved events show both your words (prospect) and Ananya's words (agent). If this does not work, apply the fallback in Section D.4 and test it. If neither works, **STOP and tell the PM before building any UI.**
6. **Windows shutdown proof:** stop the worker with Ctrl+C; the log shows the cleanup callback ran.
7. **Test A (invalid number):** dial with an invalid phone number. The backend logs the error and keeps serving requests. If the backend process exits, the `sys.exit()` fix was not applied.
8. **Test B (missing callId):** simulate a dial response with no `callId`. The call becomes `unknown`, no second dial happens.

**NOT in this phase:** dashboard styling, SSE, Hang Up, outcome extraction.

## Phase 2 — Start calls and show real status [DONE]

**Calendar days:** Day 2 — Status: DONE

**What gets built**
- Dashboard UI split into `static/index.html`, `static/style.css`, and `static/app.js`, served at `GET /`: phone input, Start Call button, call list, selected-call panel.
- `GET /calls/stream` (SSE — Server-Sent Events): pushes strictly `call.updated` events to the browser.
- Worker lifecycle events (`worker.started`, `worker.stopped`, `worker.error`) emit `call.updated` with `worker_state` to the browser; `worker.heartbeat` does NOT emit `call.updated`.
- When `call.updated` arrives in the dashboard, it re-fetches the latest call via `GET /calls/{id}` to update both the call list and detail panel with live `worker_state`.
- Polling VideoSDK about every 3 seconds while a call is active; each poll saves the raw response to `calls.provider_raw_json`.
- Save real VideoSDK polling responses during active calls to `fixtures/day2/` as raw JSON fixtures (saved on both new top-level statuses and new timelog status names).
- Real VideoSDK provider statuses: top-level statuses observed are `initiated`, `ringing`, `answered`, `missed`, and `ended`.
- Final status resolution: VideoSDK returns `status: "ended"` for all terminated calls. The backend inspects `data.timelog`:
  - `answered` present $\rightarrow$ dashboard status `completed`.
  - `missed` present $\rightarrow$ dashboard status `no_answer`, provider message saved to `calls.error_message`.
  - neither present (e.g. rejected or busy) $\rightarrow$ dashboard status `failed`, last timelog message saved to `calls.error_message`.
  - missing/empty timelog $\rightarrow$ defaults to `completed` with a warning log.
- Context-aware alert box in details panel: `no_answer` and `busy` display with an amber/neutral box labelled `"Reason:"`, while `failed` and `unknown` display with a red box labelled `"Error:"`.
- Display "last provider update" time.
- Register `GET /calls/stream` before `GET /calls/{id}` so the router doesn't treat "stream" as an ID.

**PM test:** Enter your number, click Start Call. One row goes queued → dialing → the statuses VideoSDK actually returns. End the call on your phone; the dashboard updates without refresh.

**NOT in this phase:** transcript rendering, browser audio, outcome extraction, extra pages.

## Phase 3a — Live transcript

**Calendar days:** Day 3 to Day 4 morning

**What gets built**
- `POST /internal/calls/{id}/events`.
- Surgical additions to `main.py` for transcript capture and heartbeat (Section D).
- Speaker-labelled transcript in the selected-call panel.
- Transcript saved to the database before the browser is notified.

**PM test:** Speak two distinct sentences on a real call. Your words and Ananya's replies appear under the correct speakers, live.

**NOT in this phase:** Hang Up, recordings, message sending, booking actions.

## Phase 3b — Hang Up

**Calendar days:** Day 4 afternoon

**What gets built**
- `POST /calls/{id}/hangup`.
- Hang Up button on the dashboard.
- Backend calls VideoSDK's existing call-end API using the saved provider call ID.
- Dashboard shows "Hangup requested" until polling confirms the call ended.

**PM test:** Click Hang Up during a real call. The phone disconnects, then the dashboard shows the confirmed final status.

**NOT in this phase:** anything else. If 3a slips, 3b still gets its own time on Day 4 afternoon.

## Phase 4 — Outcome and ordinary-failure handling

**Calendar days:** Day 5

**What gets built**
- `outcome.py`: one post-call request using the existing Google integration to classify the saved transcript. Output: an outcome label and a short summary only.
- Idempotency (a repeated submission creates only one call).
- Worker heartbeat-loss detection.
- Explicit handling of uncertain dial timeouts.
- Dashboard restores state after refresh.
- Prompt filename and shutdown fixes from Section D (if not already done).

**PM test:** Finish a call and see its outcome. Double-click Start: exactly one attempt exists. Refresh during a call: transcript stays. Stop the worker during a test call: a worker-loss warning appears.

**NOT in this phase:** extra business actions, automatic redial.

## Phase 5 — Rehearse, fix, demo

**Calendar days:** Days 6–7

**What gets built**
- Day 6: fixes only, based on the checks above.
- Ten consecutive real calls without restarting the application.
- A clearly labelled simulated 200-attempt test through the same database and dispatcher. This checks application bookkeeping, not telephone capacity.
- Verify the actual provider polling allowance.
- Day 7: run the five-minute demo (Section F).

**PM test:** Run the demo twice before stakeholders arrive. Both runs show one real call, live transcript, refresh recovery, Hang Up, and a saved outcome.

**NOT in this phase:** new features. If checks fail, fix them; do not add screens.

---

# SECTION B — Data Model

Two tables. All timestamps are UTC ISO 8601 text, e.g. `2026-10-31T09:30:00Z`.

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE calls (
    id                      TEXT PRIMARY KEY,
    idempotency_key         TEXT NOT NULL UNIQUE,
    phone_number            TEXT NOT NULL,

    status                  TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued', 'dialing', 'ringing', 'connected',
            'hangup_requested', 'completed', 'busy',
            'no_answer', 'failed', 'unknown'
        )),

    provider_call_id        TEXT,
    room_id                 TEXT,

    worker_state            TEXT NOT NULL DEFAULT 'waiting'
        CHECK (worker_state IN (
            'waiting', 'active', 'stale', 'stopped'
        )),

    last_heartbeat_at       TEXT,
    last_provider_poll_at   TEXT,
    provider_raw_json       TEXT,
    error_message           TEXT,

    outcome_state           TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome_state IN (
            'pending', 'processing', 'ready', 'unavailable'
        )),

    outcome                 TEXT
        CHECK (outcome IS NULL OR outcome IN (
            'interested', 'callback_requested',
            'not_interested', 'unclear', 'no_conversation'
        )),

    summary                 TEXT,
    created_at              TEXT NOT NULL,
    ended_at                TEXT
);

CREATE TABLE call_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         TEXT NOT NULL
        REFERENCES calls(id),
    event_key       TEXT NOT NULL UNIQUE,

    type            TEXT NOT NULL
        CHECK (type IN (
            'call.updated',
            'transcript.final',
            'worker.started',
            'worker.heartbeat',
            'worker.stopped',
            'worker.error'
        )),

    payload_json    TEXT NOT NULL,
    received_at     TEXT NOT NULL
);

CREATE INDEX call_events_by_call
    ON call_events(call_id, id);
```

Rules: keep database transactions (groups of changes saved together) short. Only the backend writes to SQLite. Network calls happen outside transactions.

**Why each table**

| Table | Why |
|---|---|
| `calls` | One row per attempt, holding the latest state needed to rebuild the dashboard. |
| `call_events` | Saved transcript segments and changes, so refreshes and SSE reconnects lose nothing. |

**Why each column**

| Column | Why |
|---|---|
| `calls.id` | Internal ID for one attempt, independent of provider IDs. |
| `idempotency_key` | Prevents duplicate calls from repeated Start submissions. |
| `phone_number` | The destination actually requested. |
| `status` | Drives the database queue and shows telephone progress. |
| `provider_call_id` | Identifies the provider call for polling and Hang Up. |
| `room_id` | Connects the worker's room to the correct internal attempt. |
| `worker_state` | Separates "phone call status" from "is Ananya's worker alive". |
| `last_heartbeat_at` | Detects when worker updates stop mid-call. |
| `last_provider_poll_at` | Prevents stale provider info from looking current. |
| `provider_raw_json` | Last raw provider response, so the PM can paste it to a developer for debugging. No UI. |
| `error_message` | Explains failures and uncertain states. |
| `outcome_state` | Distinguishes pending analysis from ready or unavailable. |
| `outcome` | The business outcome label. |
| `summary` | One or two sentences explaining the outcome. |
| `created_at` | Orders calls and selects the oldest queued attempt. |
| `ended_at` | Records confirmed termination; marks calls ready for outcome processing. |
| `call_events.id` | Ordered cursor for SSE replay. |
| `call_events.call_id` | Links each event to its attempt. |
| `event_key` | Prevents a repeated worker event from duplicating a transcript segment. |
| `type` | Tells backend and browser how to interpret the event. |
| `payload_json` | Event-specific content without extra tables. |
| `received_at` | When the backend accepted the event. |

No contacts table, recordings table, or separate queue table.

---

# SECTION C — Backend Contract

**Seven endpoints only:**

1. `GET /` — dashboard page
2. `POST /calls`
3. `POST /calls/{id}/hangup`
4. `GET /calls`
5. `GET /calls/{id}`
6. `GET /calls/stream`
7. `POST /internal/calls/{id}/events`

**Common rules**
- Validation errors: 422. Missing call: 404. Database unavailable: 503.
- Error body: `{"error": "Readable explanation"}`
- Phone numbers in international format, e.g. `+91…`.
- Backend runs as one process (so only one dispatcher exists).
- Example numbers and IDs are placeholders.
- Internal endpoint is reachable locally only.

### 1. GET /
Returns `static/index.html` (HTML served with `/static/style.css` and `/static/app.js`). No content negotiation.

### 2. POST /calls  (create only)
Request:
```json
{
  "phone_number": "+919000000000",
  "idempotency_key": "b80d63c0-unique-start-action"
}
```
Response `202 Accepted`:
```json
{ "id": "c_123", "status": "queued", "replayed": false }
```
Repeating the same key and number returns the existing call with `200` and `"replayed": true`. Reusing the key with a different number returns `409`.
Errors: 422 invalid number, 409 key conflict, 503.

### 3. POST /calls/{id}/hangup
No request body. Response `202` after the provider accepts the termination:
```json
{ "id": "c_123", "status": "hangup_requested" }
```
Errors:
- 404 unknown call
- 409 call is still queued or has no provider ID
- 502 provider explicitly rejects termination
- 504 termination request timed out — show "Hangup unconfirmed" and keep polling

Hang Up on an already-finished call returns `200` with its current status.

### 4. GET /calls  (JSON only)
`GET /calls?limit=50&before=c_123`

Response `200`:
```json
{
  "calls": [
    {
      "id": "c_122",
      "phone_number": "+919000000000",
      "status": "connected",
      "worker_state": "active",
      "outcome": null,
      "created_at": "2026-10-31T09:30:00Z"
    }
  ],
  "next_before": null,
  "event_cursor": 28
}
```
Also supports `room_id` and `in_flight=true` filters (used by the worker to find its call).
Errors: 422 invalid filter; 503.

### 5. GET /calls/{id}
Response `200`:
```json
{
  "id": "c_123",
  "phone_number": "+919000000000",
  "status": "completed",
  "worker_state": "stopped",
  "provider_call_id": "provider_456",
  "room_id": "room_789",
  "last_provider_poll_at": "2026-10-31T09:32:00Z",
  "last_heartbeat_at": "2026-10-31T09:31:58Z",
  "error_message": null,
  "outcome_state": "ready",
  "outcome": "callback_requested",
  "summary": "The prospect requested a callback.",
  "transcript": [
    { "event_id": 25, "speaker": "prospect", "text": "Please call me tomorrow." }
  ],
  "event_cursor": 28
}
```
Returns the full transcript (no cap in v1). Errors: 404, 503.

### 6. GET /calls/stream
`GET /calls/stream?after=28` with `Accept: text/event-stream`. On automatic reconnect, accept `Last-Event-ID` as the cursor. Browser uses the built-in `EventSource` reconnect; the page shows "Reconnecting" while disconnected.

Response `200` as SSE frames (pushes strictly `call.updated` events to browser):
```
id: 30
event: call.updated
data: {"call_id":"c_123","status":"completed","worker_state":"stopped"}
```
Replay saved `call.updated` events after the cursor, then stream new ones. Emitted on call status changes and on worker lifecycle events (`worker.started`, `worker.stopped`, `worker.error`), but NOT on `worker.heartbeat`. Dashboard on receiving `call.updated` re-fetches `GET /calls/{id}` to refresh both call list and detail panel with fresh server state. Send a keepalive comment every 15 seconds.
Errors: 422 invalid cursor; 503 before opening if storage is down.

### 7. POST /internal/calls/{id}/events
Request:
```json
{
  "event_key": "session-abc-segment-7",
  "type": "transcript.final",
  "payload": { "speaker": "prospect", "text": "Please send the details." }
}
```
Response `201`:
```json
{ "event_id": 29, "accepted": true }
```
A repeated `event_key` with the same content returns `200` with the existing event ID.

Allowed worker payloads:

| Type | Payload |
|---|---|
| `worker.started` | `{}` |
| `worker.heartbeat` | `{}` |
| `worker.stopped` | `{}` |
| `worker.error` | `{"message":"Connection lost"}` |
| `transcript.final` | `{"speaker":"prospect"|"agent","text":"..."}` |

`call.updated` is generated by the backend, never accepted from the worker.
Errors: 404 unknown call; 409 same `event_key` with different content; 422 invalid type/payload; 503.

---

# SECTION D — Worker Integration Points

Change `main.py` and `dial.py` **minimally**. Do not redesign the worker.

### D.1 Keep the existing dialing path
The backend imports `dial()` and calls it once per selected queued row. Preserve its VideoSDK URL, authorization, routing rule, and request fields.

Necessary change: when imported, `dial()` must **raise an exception instead of calling `sys.exit()`**, otherwise one bad dial kills the backend. The command-line `main()` in `dial.py` may still convert exceptions to an exit code.

### D.2 Save the nested response correctly
Immediately after `resp.json()` in `dial.py`, normalise the response (IDs are nested under `data`):
```python
raw = resp.json()
data = raw.get("data", raw)

return {
    "provider_call_id": data.get("id") or data.get("callId") or data.get("sipCallId"),
    "room_id": data.get("roomId"),
    "status": data.get("status"),
}
```
The real VideoSDK SIP API response stores the provider call identifier in `data.id` (not `callId` or `sipCallId`). The backend caller saves these against the internal call ID immediately. Database writes stay out of `dial.py`.

The provider's raw status is `"initiated"`, which does not exist in `calls.status` CHECK constraints; the backend maps this to `'dialing'` on successful dial while preserving the raw status in `calls.provider_raw_json`.

If the HTTP response succeeds but identifiers are missing, mark the attempt `unknown` and preserve partial `room_id` if present. Do not declare a safe failure. Do not redial.

### D.3 How the worker discovers `call_id`
The backend generates `call_id` before dialing (VideoSDK does not). The worker finds it through the room:
1. In `entrypoint(ctx)`, after the room connection is established, read `ctx.room_options.room_id`.
2. Call local `GET /calls?room_id=…&in_flight=true`.
3. Require exactly one match.
4. Keep that internal ID for all event submissions.

The dial response can arrive after the worker joins. Retry only this local lookup, briefly, while holding early transcript events in memory. **Never repeat the telephone dial request.** One active call at a time makes room matching reliable. Do not run command-line calls while using the dashboard.

### D.4 Transcript callbacks (PROVEN on Day 1)
In `entrypoint(ctx)`, right after constructing `realtime = GeminiRealtime(...)` and before starting the session, register:
```python
realtime.on("realtime_model_transcription", on_transcription)
```
**Day 1 verification result: PROVEN.** The callback fires live in production configuration:
- Emits user turn end (`accumulated_input_text`, mapped `user` → `prospect`) and agent turn complete (`final_transcription`, mapped `agent` → `agent`).
- `session.say()` does NOT produce a callback event; `VoiceAssistant.on_enter()` explicitly sends the greeting text as `transcript.final` (speaker `agent`) via the bridge.
- Upon hangup/call termination, any incomplete final agent reply currently in flight is lost.

The callback:
- Maps `user` → `prospect`, `agent` → `agent`.
- Submits only non-empty final segments.
- Gives each segment a stable `event_key`.
- Schedules the local HTTP submission without blocking audio.

Important: the SDK's event emitter needs a **synchronous** callback. The callback schedules an async task; do not register an `async def` directly. Track pending HTTP tasks in a small set (not a call queue).

Also register `realtime.on("error", on_realtime_error)`. Suppress normal shutdown error `"Receive loop cancelled"` (log as info only) and do not emit a `worker.error` event for it.

### D.5 Heartbeat and outcome
- Send `worker.started` after the session starts.
- Send `worker.heartbeat` every 5 seconds while the session runs.
- Send `worker.stopped` during orderly cleanup.
- The backend (not the voice conversation) runs one outcome-extraction request after termination and final transcript flush, using the existing Google client and key with a text-capable model verified on Day 1.
- Output must be a label from the schema plus a short summary; unsupported conclusions become `unclear`.
- No prospect transcript → `no_conversation`. Extraction failure → `outcome_state = unavailable`; transcript stays visible.

### D.6 Fix the prompt filename
In `main.py` change to:
```python
PROMPT_PATH = Path(__file__).parent / "prompt.md"
```
Keep the existing lowercase file. Do not duplicate or rename it.

### D.7 Replace the fake shutdown handling
1. Delete the custom `shutdown_handler()`.
2. Delete the two custom `signal.signal(...)` registrations.
3. Replace the redundant worker construction and instance call with:
```python
Worker.run_worker(
    options=options,
    default_room_options=default_room_options,
)
```
The installed runner catches SIGINT/SIGTERM and invokes `worker.shutdown()`.

Keep `ctx.run_until_shutdown(session=session)`, which already registers session closing.

Add one more cleanup callback:
```python
ctx.add_shutdown_callback(cleanup_bridge)
```
`cleanup_bridge` stops the heartbeat, waits up to 3 seconds for pending event submissions, and sends `worker.stopped`. It must not close the session a second time.

**Windows note:** signal handling differs on Windows. Day 1 must prove that Ctrl+C and process stop actually trigger the cleanup callback. A forced process kill cannot run cleanup; heartbeat detection covers that.

---

# SECTION E — Reliability Floor (v1 only)

| Failure | Required v1 behaviour | PM verification |
|---|---|---|
| Double-click Start | Button disables immediately; one idempotency key is reused; SQLite's unique constraint guarantees one record. | Double-click quickly; only one phone call occurs. |
| Worker crash mid-call | After 20 seconds without heartbeat, show "Worker connection lost — transcript may be incomplete"; keep provider polling; keep Hang Up available. | Stop the worker while connected; warning appears and saved text stays. |
| Dial request timeout | Set `unknown`, show "Call may have started — do not redial", pause dispatch of later queued calls. | Assistant simulates a timeout after submission; no second dial occurs. |
| Dashboard refresh | Reload saved list and selected detail, then replay SSE from the snapshot cursor. | Refresh while talking; earlier text returns and new text continues. |
| Bad phone number | `dial()` raises an exception; backend logs it, marks the call `failed`, keeps running. | Dial an invalid number; the backend keeps serving requests. |
| Dial response missing `callId` | Call becomes `unknown`; no second dial; "do not redial" warning shown. | Simulate such a response; confirm no second dial. |

For an uncertain dial: poll recent provider records and adopt a match only if unambiguous. If unresolved, keep dispatch paused; the PM checks the VideoSDK console before restarting. Restarting must not silently resend an unknown attempt.

Do not turn a worker crash into a claim that the phone call ended.

### DEFERRED TO v2 — OUT OF SCOPE — defer to v2+
- Provider webhooks, signature verification, webhook dedupe, out-of-order handling
- Recordings and playback
- Hosted deployment
- Multiple concurrent calls; hitting 200 calls/day as a guaranteed target
- Multi-tenant support and templates for others (migration note only)
- Database or provider migration
- Login and public dashboard access
- Analytics, settings, agent versioning pages
- WhatsApp/email sending, callback scheduling, booking, transfers
- Automated redial and advanced recovery
- Custom SSE backoff logic, transcript pagination (add if transcripts exceed ~500 segments)

---

# SECTION F — Demo Script (Day 7)

**Before the five minutes:** run `start.bat`, keep the PC awake, confirm the worker is connected, use your own phone or a consenting colleague's. Keep a previously completed call visible in history as an honest fallback.

| Time | What I do | What stakeholders see |
|---|---|---|
| 0:00–0:30 | Open dashboard, enter test number, click Start Call. | One new row: queued, then dialing. |
| 0:30–1:00 | Answer the phone on speaker. | Connected status and Ananya's introduction. |
| 1:00–2:00 | Say: "We currently record in our office. Editing takes too long." | Prospect text and Ananya's reply under correct speakers. |
| 2:00–2:30 | Refresh the browser; select the active row if needed. | Transcript returns; same call still active. |
| 2:30–3:00 | Say: "I'm interested. Please call me tomorrow." | New transcript text appears without refresh. |
| 3:00–3:30 | Click Hang Up. | "Hangup requested", phone disconnects, then confirmed final status. |
| 3:30–4:30 | Keep the call selected. | Outcome moves from processing to `callback_requested` with a short summary. |
| 4:30–5:00 | Refresh once more; reopen that call. | Completed status, saved transcript, and outcome remain. |

**Success:** one real call, both speakers' transcript, working Hang Up, refresh recovery, and a saved outcome — all from one page.

**If something breaks live (honest fallback line):**
> "The live call integration has failed at this step. I'll show a previously completed call to demonstrate the saved results, and we'll record this failure as a demo blocker."

**If only outcome extraction fails:**
> "The call and transcript were saved. Automatic outcome classification failed, and the dashboard is showing that explicitly."

Never present a previous transcript as the current live call.

(The worker-crash behaviour is verified in rehearsal, not in the live demo.)

---

# SECTION G — Risks & Open Questions

**Assumptions that could invalidate the plan**
- **Throughput:** one call at a time supports 200 attempts in 8 hours only if dial + conversation + cleanup average about 2 min 24 s. Longer average calls mean v1 supports fewer calls/day. This is acceptable in v1 (demo scope); revisit before any production use.
- **Transcript callbacks:** the plugin source contains the event, but it must fire in the live configuration. Day 1 proves this (Section D.4). Largest technical risk.
- **Room matching:** the worker's room ID must match the room ID returned by the dial request. Test with two consecutive calls, including any room reuse.
- **Windows behaviour:** shutdown and signal handling must be proven on Windows (Section D.7).
- **Outcome model:** a text-capable Gemini model with the existing key must pass a small extraction test on Day 1; do not assume the live audio model supports it.
- **Local operation:** the PC stays awake and online throughout calling.
- **Seven-day assumption:** daily access to the PC, working credentials, and a test phone. Waiting days for account support can break the schedule.

**VideoSDK items to verify BEFORE Day 1**
- Polling `GET /v2/sip/call` every 3 seconds during active calls — is it allowed for the account? Which statuses and final causes are returned?
- Does the saved `data.callId` successfully end a real call through the existing call-end API?
- **Deprecation-safety question for VideoSDK support (ask for a yes/no):**
  > "Will our existing self-hosted Python `videosdk.agents` worker, registered through our current routing rule and dialing through `POST /v2/sip/call`, remain supported without a mandatory migration for the next 90 days — yes or no?"

  Their docs show an AI Voice Agents migration notice. That does not establish whether this account is affected; support must answer for this integration. No migration is included in this plan.

**Most likely reason this slips from 1 week to 2:** reliable two-speaker transcript capture, and matching those events to the right call attempt, will take more debugging than expected. That is why both are tested on Day 1, before any dashboard polish.
