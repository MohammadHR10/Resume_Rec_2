"""Codex CLI harness adapter.

``codex exec --json`` is per-turn by design, and codex mints its own thread id
rather than accepting one — so, exactly as in the hive-edge-minds template, the
thread id it reports on the first turn is persisted as ``harness_sid`` and
passed to ``codex resume`` on every later turn. A turn that fails clears the
stored id, because resuming a thread with an unanswered turn returns the
*previous* turn's reply on the next question.

Routed by ``OPENAI_BASE_URL``/``OPENAI_API_KEY`` to whichever gateway the active
provider exposes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

NAME = "codex_cli"


def available() -> bool:
    return shutil.which("codex") is not None


def run_turn(
    *,
    workspace: str,
    question: str,
    model: str = "",
    env: dict[str, str] | None = None,
    harness_sid: str = "",
    brief: str = "",
    timeout: float = 300.0,
) -> dict:
    """Run one turn. Returns ``{"text": str, "harness_sid": str}``."""
    if not available():
        raise RuntimeError("the codex CLI is not installed in this container")

    command = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox",
               "-C", workspace]
    if model:
        command.extend(["-m", model])
    if harness_sid:
        command.extend(["resume", harness_sid, "-"])
        stdin_text = question
    else:
        command.append("-")
        # Codex has no --append-system-prompt; the brief rides in as the
        # opening turn, which is the only channel this harness has for it.
        stdin_text = f"{brief}\n\n---\n\n{question}" if brief else question

    process_env = dict(os.environ)
    process_env.update(env or {})

    logger.info("codex harness turn (thread=%s, model=%s)", harness_sid or "new", model or "default")
    try:
        completed = subprocess.run(
            command,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"codex CLI turn timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start the codex CLI: {exc}") from exc

    texts, thread_id, failure = _parse(completed.stdout)

    if failure:
        raise RuntimeError(f"codex turn failed: {failure}")
    if completed.returncode != 0 and not texts:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise RuntimeError(f"codex CLI exited {completed.returncode}: {detail}")
    if not texts:
        raise RuntimeError("codex CLI returned no assistant message")

    return {"text": "\n\n".join(texts).strip(), "harness_sid": thread_id or harness_sid}


def _parse(stdout: str) -> tuple[list[str], str, str]:
    """Read agent messages, the thread id, and any failure from the event stream."""
    texts: list[str] = []
    thread_id = ""
    failure = ""

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        if kind == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                texts.append(str(item["text"]))
        elif kind == "turn.failed":
            failure = str((event.get("error") or {}).get("message") or "unknown error")

    return texts, thread_id, failure
