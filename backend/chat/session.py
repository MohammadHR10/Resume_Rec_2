"""Chat session lifecycle: mode selection, turn execution, degradation, actions.

One session per screening+stage. Which path a turn takes is config, not code:
a model marked ``harness`` drives a real CLI over the workspace; a model marked
``structured`` runs the in-code loop. Both call the same tools, so an answer
does not change shape when the mode does — and a harness turn that fails
degrades to structured *for that turn* rather than losing the question.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import db
from ..llm import registry
from ..llm.base import LLMError
from . import structured, workspace
from .adapters import get_adapter

logger = logging.getLogger(__name__)

VALID_STAGES = {"1", "2", "3", "rejected"}
HISTORY_TURNS = 8

#: Which CLI a provider's harness mode drives when config does not say.
#: The claude CLI needs an Anthropic-compatible endpoint, which only the UL
#: proxy exposes; the SIS gateway is assumed OpenAI-only, so codex it is.
DEFAULT_HARNESS = {"ulproxy": "claude_cli", "fastllm": "codex_cli"}


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
        "INSERT INTO chat_session (id, screening_id, stage, mode, provider, model, harness_sid, "
        "workspace, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, screening_id, stage, "", "", "", "", "", db.now()),
    )
    return db.query_one("SELECT * FROM chat_session WHERE id=?", (session_id,))  # type: ignore[return-value]


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


def harness_name(provider_name: str) -> str:
    configured = (db.get_config().get("harness_cli") or "").strip()
    return configured or DEFAULT_HARNESS.get(provider_name, "claude_cli")


def ask(session_id: str, question: str) -> dict[str, Any]:
    """Run one turn and return the answer plus any grid actions it queued."""
    session = db.query_one("SELECT * FROM chat_session WHERE id=?", (session_id,))
    if not session:
        raise ValueError("unknown chat session")

    question = (question or "").strip()
    if not question:
        raise ValueError("no question given")

    provider, provider_name, model = registry.active()
    mode = registry.chat_mode(provider_name, model)

    path = workspace.build_workspace(session_id, session["screening_id"], session["stage"])
    brief = (path / "CHAT.md").read_text(encoding="utf-8")

    _record(session_id, "user", question)

    degraded_from = ""
    trace: list[dict[str, Any]] = []
    harness_sid = session["harness_sid"]

    if mode == "harness":
        try:
            adapter = get_adapter(harness_name(provider_name))
            env = getattr(provider, "harness_env", dict)() or {}
            result = adapter.run_turn(
                workspace=str(path),
                question=question,
                model=model,
                env=env,
                harness_sid=harness_sid,
                brief=brief,
            )
            answer = result["text"]
            harness_sid = result.get("harness_sid") or harness_sid
        except Exception as exc:  # noqa: BLE001 — any harness failure degrades
            logger.warning("Harness turn failed, degrading to structured: %s", exc)
            degraded_from = f"{harness_name(provider_name)}: {exc}"
            mode = "structured"
            # A failed harness turn leaves its conversation in an unknown
            # state; drop the id so the next turn starts a clean one.
            harness_sid = ""

    if mode == "structured":
        try:
            outcome = structured.run_turn(
                provider, model, path, brief, _history(session_id)[:-1], question
            )
        except LLMError as exc:
            _record(session_id, "assistant", f"The model could not answer: {exc}", "error", model)
            raise
        answer = outcome["answer"]
        trace = outcome["trace"]

    actions = workspace.drain_actions(session_id)
    _queue_actions(session_id, actions)

    _record(session_id, "assistant", answer, mode, model)
    db.execute(
        "UPDATE chat_session SET mode=?, provider=?, model=?, harness_sid=?, workspace=? WHERE id=?",
        (mode, provider_name, model, harness_sid, str(path), session_id),
    )

    return {
        "answer": answer,
        "mode": mode,
        "degradedFrom": degraded_from,
        "actions": actions,
        "trace": trace,
        "model": model,
        "provider": provider_name,
    }
