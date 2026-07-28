"""SQLite persistence for the staged screening app.

Human-gated stages and per-run model tagging mean state has to survive a
restart, so unlike the reference pipeline (in-memory jobs) everything lands in
SQLite. Raw ``sqlite3`` with a thin helper layer — no ORM at this scale.

Connections are created per call rather than shared: FastAPI dispatches sync
handlers onto a thread pool, and a single connection would need a lock around
every statement for no gain at this volume.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.getenv("SCREENING_DB", Path(__file__).resolve().parent.parent / "data" / "screening.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS screening (
    id           TEXT PRIMARY KEY,
    job_title    TEXT NOT NULL DEFAULT '',
    jd_text      TEXT NOT NULL DEFAULT '',
    jd_filename  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft',
    quals_confirmed INTEGER NOT NULL DEFAULT 0,
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'screening',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qualification (
    id           TEXT PRIMARY KEY,
    screening_id TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('required', 'preferred')),
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qual_screening ON qualification(screening_id);

CREATE TABLE IF NOT EXISTS candidate (
    id           TEXT PRIMARY KEY,
    screening_id TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    source_files TEXT NOT NULL DEFAULT '[]',
    resume_text  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_screening ON candidate(screening_id);

CREATE TABLE IF NOT EXISTS evaluation (
    id             TEXT PRIMARY KEY,
    screening_id   TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    candidate_id   TEXT NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    verdicts       TEXT NOT NULL DEFAULT '{}',
    required_met   INTEGER NOT NULL DEFAULT 0,
    required_total INTEGER NOT NULL DEFAULT 0,
    preferred_met  INTEGER NOT NULL DEFAULT 0,
    preferred_total INTEGER NOT NULL DEFAULT 0,
    ai_pass        INTEGER NOT NULL DEFAULT 0,
    summary        TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    provider       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_screening ON evaluation(screening_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_candidate ON evaluation(candidate_id);

CREATE TABLE IF NOT EXISTS stage_state (
    candidate_id TEXT PRIMARY KEY REFERENCES candidate(id) ON DELETE CASCADE,
    screening_id TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL DEFAULT '1',
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stage_screening ON stage_state(screening_id);

CREATE TABLE IF NOT EXISTS stage_action (
    id           TEXT PRIMARY KEY,
    screening_id TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES candidate(id) ON DELETE CASCADE,
    from_stage   TEXT NOT NULL,
    to_stage     TEXT NOT NULL,
    action       TEXT NOT NULL,
    override     INTEGER NOT NULL DEFAULT 0,
    actor        TEXT NOT NULL DEFAULT 'user',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_screening ON stage_action(screening_id);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_session (
    id           TEXT PRIMARY KEY,
    screening_id TEXT NOT NULL REFERENCES screening(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'structured',
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    harness_sid  TEXT NOT NULL DEFAULT '',
    workspace    TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_screening_stage
    ON chat_session(screening_id, stage);

CREATE TABLE IF NOT EXISTS chat_message (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chatmsg_session ON chat_message(session_id);

CREATE TABLE IF NOT EXISTS chat_action (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chataction_session ON chat_action(session_id);

CREATE TABLE IF NOT EXISTS audit_run (
    id           TEXT PRIMARY KEY,
    screening_id TEXT REFERENCES screening(id) ON DELETE SET NULL,
    corpus       TEXT NOT NULL DEFAULT '',
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'running',
    passed       INTEGER,
    pairs        TEXT NOT NULL DEFAULT '[]',
    summary      TEXT NOT NULL DEFAULT '{}',
    thresholds   TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
"""


def now() -> str:
    """UTC timestamp, ISO-8601 with a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    """A connection scoped to one unit of work; commits on clean exit."""
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with cursor() as cur:
        cur.execute(sql, params)


def execute_many(sql: str, rows: list[tuple]) -> None:
    with cursor() as cur:
        cur.executemany(sql, rows)


# ---------------------------------------------------------------------------
# Config: a key/value table holding JSON. Secrets never land here — they stay
# in the environment; config only records which non-secret selection is active.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ulproxy",
    "model": "",
    "chat_modes": {},
    # "Negligible bias", as agreed: no variant may flip a stage-1 outcome, and
    # the average absolute coverage change stays under half a qualification.
    # Verdict-flip rate is reported but is not a pass/fail criterion — a single
    # Meets/Partial wobble is model nondeterminism, not a hiring outcome.
    "audit_thresholds": {
        "max_stage_flips": 0,
        "max_mean_coverage_delta": 0.5,
    },
}


def get_config() -> dict[str, Any]:
    stored = {row["key"]: json.loads(row["value"]) for row in query("SELECT key, value FROM config")}
    merged = dict(DEFAULT_CONFIG)
    merged.update(stored)
    return merged


def set_config(values: dict[str, Any]) -> dict[str, Any]:
    with cursor() as cur:
        for key, value in values.items():
            cur.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
    return get_config()
