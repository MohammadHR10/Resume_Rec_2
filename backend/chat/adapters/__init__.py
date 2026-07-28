"""Harness adapters — one thin spawn/parse seam per CLI.

Following the ``mind_templates/`` pattern from hive-edge-minds: each adapter
knows only how to start its CLI, hand it a turn, and read the reply back out.
Session identity, workspace construction and mode selection live in
``session.py``, so adding a third harness is one file.

One deliberate departure from that reference: a turn here is a fresh process
resumed by id, not a long-lived stream-json process held open per session. A
screening chat is request/response over HTTP with minutes of human thinking
between turns, so a resident process would spend its life idle and need a
reaper; ``--resume``/``codex resume`` gives the same continuity from the
``harness_sid`` already in the data model, without the lifecycle.
"""

from __future__ import annotations

from typing import Protocol

from . import claude_cli, codex_cli


class HarnessAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def run_turn(
        self,
        *,
        workspace: str,
        question: str,
        model: str,
        env: dict[str, str],
        harness_sid: str = "",
        brief: str = "",
        timeout: float = 300.0,
    ) -> dict: ...


ADAPTERS = {
    claude_cli.NAME: claude_cli,
    codex_cli.NAME: codex_cli,
}


def get_adapter(name: str):
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise ValueError(f"unknown harness adapter: {name}")
    return adapter


def available_adapters() -> list[dict]:
    return [
        {"name": name, "available": module.available()} for name, module in ADAPTERS.items()
    ]
