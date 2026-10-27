"""Shared plumbing for the stateless analysis tools.

Every tool is a script: argparse in, one JSON object on stdout, no state of its
own beyond the workspace snapshot it reads. Both chat modes run these same
files — the harness invokes them as shell commands, the structured loop runs
them as subprocesses on the model's behalf — so an answer can never differ
between modes because of how the question was routed.

The ranking and rollup math is imported from ``backend.pipeline`` rather than
reimplemented here. That is the whole basis of the what-if guarantee: the
numbers a chat answer quotes are produced by the same code that produced the
grid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(
    os.environ.get("SCREENING_WORKSPACE") or Path(__file__).resolve().parent.parent
)


def _backend_root() -> str:
    try:
        from _bootstrap import BACKEND_ROOT  # type: ignore[import-not-found]

        return BACKEND_ROOT
    except ImportError:
        # Running from the repo itself rather than a copied workspace.
        return os.environ.get(
            "SCREENING_BACKEND_ROOT", str(Path(__file__).resolve().parents[3])
        )


_root = _backend_root()
if _root and _root not in sys.path:
    sys.path.insert(0, _root)

from backend.pipeline import compute_rollups, rank_candidates  # noqa: E402

__all__ = [
    "argument_parser",
    "compute_rollups",
    "emit",
    "fail",
    "find_candidate",
    "load_snapshot",
    "rank_candidates",
    "resolve_qual",
    "stage_label",
    "workspace_dir",
]

STAGE_LABELS = {
    "1": "Stage 1 — Minimum Requirements",
    "2": "Stage 2 — Preferred Qualifications",
    "3": "Stage 3 — Interview Candidates",
    "rejected": "Rejected",
}


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(str(stage), f"Stage {stage}")


def argument_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--workspace",
        default=str(WORKSPACE),
        help="Chat workspace directory holding snapshot.json (defaults to this tool's workspace).",
    )
    return parser


def workspace_dir(args: argparse.Namespace) -> Path:
    return Path(args.workspace)


def load_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    path = workspace_dir(args) / "snapshot.json"
    if not path.exists():
        fail(f"no snapshot at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def fail(message: str, **extra: Any) -> None:
    """Errors are JSON too — a tool's caller is a model, not a terminal."""
    emit({"error": message, **extra})
    sys.exit(1)


def find_candidate(snapshot: dict[str, Any], needle: str) -> dict[str, Any] | None:
    """Match a candidate by id, rank, exact name, then unique substring.

    Rank matters because that is how the grid labels people: a user looking at
    the screen asks "why did 3, 4 and 6 fail?", meaning the rank column, and a
    model that can only search names will look for a candidate called "3".
    """
    needle = (needle or "").strip()
    if not needle:
        return None
    candidates = snapshot["candidates"]
    for candidate in candidates:
        if candidate["id"] == needle:
            return candidate

    rank_token = needle.lstrip("#").strip()
    if rank_token.isdigit():
        ranked = [c for c in candidates if str(c.get("rank")) == rank_token]
        # Ties share a rank, so only answer when it identifies one person.
        if len(ranked) == 1:
            return ranked[0]
    lowered = needle.lower()
    exact = [c for c in candidates if c["name"].lower() == lowered]
    if exact:
        return exact[0]
    partial = [c for c in candidates if lowered in c["name"].lower()]
    return partial[0] if len(partial) == 1 else None


def resolve_qual(snapshot: dict[str, Any], token: str) -> dict[str, Any] | None:
    """Match a qualification by short label (``R1``), id, or text substring."""
    token = (token or "").strip()
    if not token:
        return None
    quals = snapshot["qualifications"]
    lowered = token.lower()
    for qual in quals:
        if qual["id"] == token or qual["label"].lower() == lowered:
            return qual
    partial = [q for q in quals if lowered in q["text"].lower()]
    return partial[0] if len(partial) == 1 else None
