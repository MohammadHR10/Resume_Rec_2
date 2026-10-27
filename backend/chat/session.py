"""Chat sessions use only allowlisted tools, regardless of legacy configuration."""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any

from .. import db
from ..llm import registry
from ..llm.base import LLMError
from . import structured, workspace
from .app_tools import AppTools, proposals

# Serialize turns per session so snapshots, transcript and queued actions cannot interleave.
_TURN_LOCKS: dict[str, Lock] = {}
_LOCK_GUARD = Lock()

logger = logging.getLogger(__name__)

VALID_STAGES = {"1", "2", "3", "rejected"}
HISTORY_TURNS = 8

def get_or_create_session(screening_id: str, stage: str) -> dict[str, Any]:
    stage = str(stage)
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown stage: {stage}")

    row = db.query_one(
        "SELECT * FROM chat_session WHERE screening_id=? AND stage=?", (screening_id, stage)
    )
    if row:
        return row

    session_id = db.new_id()
    db.execute(
        "INSERT OR IGNORE INTO chat_session (id, screening_id, stage, mode, provider, model, harness_sid, "
        "workspace, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, screening_id, stage, "", "", "", "", "", db.now()),
    )
    return db.query_one("SELECT * FROM chat_session WHERE screening_id=? AND stage=?", (screening_id, stage))  # type: ignore[return-value]


def transcript(session_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, role, content, mode, model, created_at FROM chat_message "
        "WHERE session_id=? ORDER BY rowid",
        (session_id,),
    )


def _history(session_id: str) -> list[dict[str, str]]:
    rows = db.query(
        "SELECT role, content FROM chat_message WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
        (session_id, HISTORY_TURNS * 2),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _record(session_id: str, role: str, content: str, mode: str = "", model: str = "") -> None:
    db.execute(
        "INSERT INTO chat_message (id, session_id, role, content, mode, model, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (db.new_id(), session_id, role, content, mode, model, db.now()),
    )


def _queue_actions(session_id: str, actions: list[dict[str, Any]]) -> None:
    for action in actions:
        db.execute(
            "INSERT INTO chat_action (session_id, payload, created_at) VALUES (?,?,?)",
            (session_id, json.dumps(action), db.now()),
        )


def pending_actions(session_id: str, since: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT id, payload FROM chat_action WHERE session_id=? AND id > ? ORDER BY id",
        (session_id, since),
    )
    return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]


def ask(session_id: str, question: str, view: dict | None = None) -> dict[str, Any]:
    with _LOCK_GUARD:
        lock = _TURN_LOCKS.setdefault(session_id, Lock())
    if not lock.acquire(blocking=False):
        raise ValueError("A question is already running in this chat. Wait for it to finish.")
    try:
        return _ask(session_id, question, view)
    finally:
        lock.release()


def _ask(session_id: str, question: str, view: dict | None = None) -> dict[str, Any]:
    """Run one turn and return the answer plus any grid actions it queued."""
    session = db.query_one("SELECT * FROM chat_session WHERE id=?", (session_id,))
    if not session:
        raise ValueError("unknown chat session")

    question = (question or "").strip()
    if not question:
        raise ValueError("no question given")

    provider, provider_name, model = registry.active()
    mode = "structured"

    path = workspace.build_workspace(session_id, session["screening_id"], session["stage"])
    brief = (path / "CHAT.md").read_text(encoding="utf-8")

    _record(session_id, "user", question)

    app_tools = AppTools(session, view)
    # Clear legacy/failed turn actions before executing the current request.
    workspace.drain_actions(session_id)

    def execute_tool(tool: str, args: dict) -> str:
        if tool in structured.ANALYSIS_TOOLS:
            output = structured.run_tool(path, tool, args)
            # Preserve interleaved navigation/grid operations in model call order.
            app_tools.actions.extend(workspace.drain_actions(session_id))
            return output
        return app_tools.run(tool, args)

    try:
        outcome = structured.run_turn(
            provider, model, path, brief, _history(session_id)[:-1], question,
            execute_app_tool=execute_tool,
        )
    except LLMError as exc:
        workspace.drain_actions(session_id)
        message = f"I couldn't complete that request — {exc}"
        _record(session_id, "assistant", message, "error", model)
        raise LLMError(message) from exc
    answer, trace = outcome["answer"], outcome["trace"]
    actions = app_tools.actions
    before = pending_actions(session_id)
    last_id = before[-1]["id"] if before else 0
    _queue_actions(session_id, actions)
    actions = pending_actions(session_id, last_id)

    _record(session_id, "assistant", answer, mode, model)
    db.execute(
        "UPDATE chat_session SET mode=?, provider=?, model=?, harness_sid=?, workspace=? WHERE id=?",
        (mode, provider_name, model, "", str(path), session_id),
    )

    return {
        "answer": answer,
        "mode": mode,
        "degradedFrom": "",
        "actions": actions,
        "trace": trace,
        "proposals": proposals(session_id),
        "model": model,
        "provider": provider_name,
    }
