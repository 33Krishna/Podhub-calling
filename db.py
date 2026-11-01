import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REAL_CALLS_DB_PATH = (Path(__file__).parent / "calls.sqlite").resolve()


def get_default_db_path() -> Path:
    env_path = os.getenv("PODHUB_DB_PATH")
    if env_path:
        return Path(env_path)
    return REAL_CALLS_DB_PATH


DEFAULT_DB_PATH = get_default_db_path()
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

ACTIVE_STATUSES = ("dialing", "ringing", "connected", "hangup_requested")
IN_FLIGHT_STATUSES = ("queued", "dialing", "ringing", "connected", "hangup_requested")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_resolved_path(db_path: Optional[Path | str] = None) -> Path:
    if db_path:
        return Path(db_path).resolve()
    return get_default_db_path().resolve()


@contextmanager
def get_connection(db_path: Optional[Path | str] = None):
    resolved_path = get_resolved_path(db_path)

    # Hard guard: If testing is active, never allow touching the real calls.sqlite
    if os.getenv("PODHUB_TESTING") == "1" and resolved_path == REAL_CALLS_DB_PATH:
        raise RuntimeError("Tests must never use the real calls.sqlite")

    path_str = str(resolved_path)
    conn = sqlite3.connect(path_str, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Path | str] = None) -> None:
    path = get_resolved_path(db_path)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(path) as conn:
        conn.executescript(schema_sql)


def create_call(
    call_id: str,
    idempotency_key: str,
    phone_number: str,
    db_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    now = utc_now_iso()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO calls (id, idempotency_key, phone_number, status, worker_state, outcome_state, created_at)
            VALUES (?, ?, ?, 'queued', 'waiting', 'pending', ?)
            """,
            (call_id, idempotency_key, phone_number, now),
        )
    return get_call_by_id(call_id, db_path=db_path)  # type: ignore


def get_call_by_id(call_id: str, db_path: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None


def get_call_by_idempotency_key(
    key: str, db_path: Optional[Path | str] = None
) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM calls WHERE idempotency_key = ?", (key,)).fetchone()
        return dict(row) if row else None


def get_calls(
    limit: int = 50,
    before: Optional[str] = None,
    room_id: Optional[str] = None,
    in_flight: Optional[bool] = None,
    db_path: Optional[Path | str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    query = "SELECT * FROM calls WHERE 1=1"
    params: List[Any] = []

    if before:
        # Cursor by created_at or id
        call_before = get_call_by_id(before, db_path=db_path)
        if call_before:
            query += " AND created_at < ?"
            params.append(call_before["created_at"])

    if room_id:
        query += " AND room_id = ?"
        params.append(room_id)

    if in_flight:
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
        query += f" AND status IN ({placeholders})"
        params.extend(IN_FLIGHT_STATUSES)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        calls = [dict(r) for r in rows]

        # Determine next_before
        next_before = calls[-1]["id"] if len(calls) == limit else None

        # Determine highest event_id cursor
        cursor_row = conn.execute("SELECT MAX(id) as max_id FROM call_events").fetchone()
        event_cursor = (cursor_row["max_id"] if cursor_row and cursor_row["max_id"] else 0)

        return calls, next_before, event_cursor


def get_oldest_queued_call(db_path: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_all_queued_calls(db_path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE status = 'queued' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def is_any_call_active(db_path: Optional[Path | str] = None) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT 1 FROM calls WHERE status IN ({placeholders}) LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
        return row is not None


def is_any_call_in_flight(db_path: Optional[Path | str] = None) -> bool:
    return is_any_call_active(db_path=db_path)


def get_active_calls(db_path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM calls WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]


def update_call(call_id: str, db_path: Optional[Path | str] = None, **fields) -> None:
    if not fields:
        return
    set_clauses = [f"{k} = ?" for k in fields.keys()]
    values = list(fields.values())
    values.append(call_id)
    with get_connection(db_path) as conn:
        conn.execute(
            f"UPDATE calls SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )


def save_call_event(
    call_id: str,
    event_key: str,
    event_type: str,
    payload_json: str,
    db_path: Optional[Path | str] = None,
) -> Tuple[int, bool, bool]:
    """
    Saves event into call_events.
    Returns (event_id, is_duplicate, is_conflict).
    If duplicate with identical payload: returns (existing_id, True, False).
    If duplicate with different payload: returns (-1, True, True).
    If new: returns (new_id, False, False).
    """
    now = utc_now_iso()
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id, payload_json FROM call_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()

        if existing:
            if existing["payload_json"] == payload_json:
                return existing["id"], True, False
            else:
                return -1, True, True

        cur = conn.execute(
            """
            INSERT INTO call_events (call_id, event_key, type, payload_json, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (call_id, event_key, event_type, payload_json, now),
        )
        return cur.lastrowid, False, False


def get_call_transcript(call_id: str, db_path: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json, received_at
            FROM call_events
            WHERE call_id = ? AND type = 'transcript.final'
            ORDER BY id ASC
            """,
            (call_id,),
        ).fetchall()

        result = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
                result.append({
                    "event_id": r["id"],
                    "speaker": payload.get("speaker", "unknown"),
                    "text": payload.get("text", ""),
                })
            except Exception:
                continue
        return result


def get_events_after(
    after_id: int,
    event_types: Optional[List[str]] = None,
    limit: int = 100,
    db_path: Optional[Path | str] = None,
) -> List[Dict[str, Any]]:
    query = "SELECT id, call_id, event_key, type, payload_json, received_at FROM call_events WHERE id > ?"
    params: List[Any] = [after_id]
    if event_types:
        placeholders = ",".join("?" for _ in event_types)
        query += f" AND type IN ({placeholders})"
        params.extend(event_types)
    query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def record_call_updated_event(
    call_id: str,
    new_status: Optional[str] = None,
    worker_state: Optional[str] = None,
    db_path: Optional[Path | str] = None,
) -> int:
    tag_status = new_status or ""
    tag_worker = worker_state or ""
    event_key = f"call-updated-{call_id}-{tag_status}-{tag_worker}-{utc_now_iso()}"
    payload: Dict[str, Any] = {"call_id": call_id}
    if new_status is not None:
        payload["status"] = new_status
    if worker_state is not None:
        payload["worker_state"] = worker_state
    payload_json = json.dumps(payload)
    event_id, _, _ = save_call_event(
        call_id=call_id,
        event_key=event_key,
        event_type="call.updated",
        payload_json=payload_json,
        db_path=db_path,
    )
    return event_id
