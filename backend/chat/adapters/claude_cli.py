"""Claude CLI harness adapter.

One `claude -p` process per turn, scoped to the chat workspace and bound to a
session id we mint ourselves — the same "the caller owns the conversation id"
rule the hive-edge-minds template follows, which is what lets a turn resume a
conversation the process that started it no longer exists to hold.

Model routing is by environment override (``ANTHROPIC_BASE_URL`` /
``ANTHROPIC_AUTH_TOKEN``), so the CLI talks to the UL AI proxy's
Anthropic-compatible surface rather than the public API.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid

logger = logging.getLogger(__name__)

NAME = "claude_cli"


def available() -> bool:
    return shutil.which("claude") is not None


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
    """Run one turn. Returns ``{"text": str, "harness_sid": str}``.

    Raises ``RuntimeError`` on any failure so ``session.py`` can degrade the
    turn to structured mode.
    """
    if not available():
        raise RuntimeError("the claude CLI is not installed in this container")

    session_id = harness_sid or str(uuid.uuid4())
    command = [
        "claude",
        "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--add-dir", workspace,
    ]
    if model:
        command.extend(["--model", model])
    # Resume once the conversation has a transcript; declare the id before it
    # does. Either way the id is ours, so the next turn can find this one.
    command.extend(["--resume", session_id] if harness_sid else ["--session-id", session_id])
    if brief and not harness_sid:
        command.extend(["--append-system-prompt", brief])

    process_env = dict(os.environ)
    process_env.update(env or {})

    logger.info("claude harness turn (session=%s, model=%s)", session_id, model or "default")
    try:
        completed = subprocess.run(
            command,
            input=question,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace,
            env=process_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI turn timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start the claude CLI: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise RuntimeError(f"claude CLI exited {completed.returncode}: {detail}")

    text, reported_sid = _parse(completed.stdout)
    if not text:
        raise RuntimeError("claude CLI returned no text")
    return {"text": text, "harness_sid": reported_sid or session_id}


def _parse(stdout: str) -> tuple[str, str]:
    """Pull the reply text and session id out of ``--output-format json``."""
    stdout = (stdout or "").strip()
    if not stdout:
        return "", ""
    try:
        payload = json.loads(stdout)
    except ValueError:
        # A CLI that fell back to plain text is still a usable answer.
        return stdout, ""

    if isinstance(payload, list):
        payload = next(
            (item for item in reversed(payload) if isinstance(item, dict) and item.get("result")),
            {},
        )
    if not isinstance(payload, dict):
        return stdout, ""
    if payload.get("is_error"):
        raise RuntimeError(str(payload.get("result") or "claude CLI reported an error"))
    return str(payload.get("result") or "").strip(), str(payload.get("session_id") or "")
