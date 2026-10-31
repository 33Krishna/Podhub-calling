PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS calls (
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

CREATE TABLE IF NOT EXISTS call_events (
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

CREATE INDEX IF NOT EXISTS call_events_by_call
    ON call_events(call_id, id);

