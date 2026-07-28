"""Per-chat-session scratch directory: snapshot, tools, brief, action queue.

Both modes read the same workspace. A harness is scoped to it as its working
directory and allowed directory; the structured loop runs the same tools
against it as subprocesses. Everything in it is derived — deleting a workspace
loses nothing but a cache.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .. import db
from ..pipeline import load_qualifications, load_rows

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(
    os.getenv("CHAT_WORKSPACE_ROOT", Path(__file__).resolve().parent.parent.parent / "data" / "chat")
)
TOOLS_SOURCE = Path(__file__).resolve().parent / "tools"
BACKEND_ROOT = str(Path(__file__).resolve().parents[2])

STAGE_LABELS = {
    "1": "Stage 1 — Minimum Requirements",
    "2": "Stage 2 — Preferred Qualifications",
    "3": "Stage 3 — Interview Candidates",
    "rejected": "Rejected",
}


def qual_labels(quals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the short R1/P2 labels the tools and prompts refer to."""
    counters = {"required": 0, "preferred": 0}
    labelled = []
    for qual in quals:
        counters[qual["kind"]] += 1
        prefix = "R" if qual["kind"] == "required" else "P"
        labelled.append({**qual, "label": f"{prefix}{counters[qual['kind']]}"})
    return labelled


def build_snapshot(screening_id: str, stage: str) -> dict[str, Any]:
    screening = db.query_one("SELECT * FROM screening WHERE id=?", (screening_id,)) or {}
    quals = qual_labels(load_qualifications(screening_id))
    rows = load_rows(screening_id)

    stages: dict[str, int] = {}
    for row in rows:
        stages[str(row["stage"])] = stages.get(str(row["stage"]), 0) + 1

    return {
        "screening": {
            "id": screening.get("id", screening_id),
            "job_title": screening.get("job_title", ""),
            "provider": screening.get("provider", ""),
            "model": screening.get("model", ""),
            "created_at": screening.get("created_at", ""),
        },
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, f"Stage {stage}"),
        "qualifications": [
            {
                "id": q["id"],
                "label": q["label"],
                "text": q["text"],
                "kind": q["kind"],
                "position": q["position"],
            }
            for q in quals
        ],
        "candidates": [
            {
                "id": row["id"],
                "name": row["name"],
                "stage": str(row["stage"]),
                "rank": row["rank"],
                "required_met": row["required_met"],
                "required_total": row["required_total"],
                "preferred_met": row["preferred_met"],
                "preferred_total": row["preferred_total"],
                "ai_pass": row["ai_pass"],
                "summary": row.get("summary") or "",
                "error": row.get("error") or "",
                "files": row.get("source_files") or [],
                "verdicts": row["verdicts"],
            }
            for row in rows
        ],
        "stages": stages,
    }


def workspace_path(session_id: str) -> Path:
    return WORKSPACE_ROOT / session_id


def build_workspace(session_id: str, screening_id: str, stage: str) -> Path:
    """Create (or refresh) the workspace and return its path.

    Refreshing on every turn rather than only at session creation is deliberate:
    a human promotes candidates between questions, and a chat answering from a
    stale snapshot is worse than no chat at all.
    """
    path = workspace_path(session_id)
    tools_dir = path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot(screening_id, stage)
    (path / "snapshot.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    for source in TOOLS_SOURCE.glob("*.py"):
        if source.name == "__init__.py":
            continue
        shutil.copy2(source, tools_dir / source.name)
    # The copied tools import the real rollup/ranking code rather than carrying
    # their own — this file is the only thing telling them where it lives.
    (tools_dir / "_bootstrap.py").write_text(
        f'"""Generated: where the backend package lives for the copied tools."""\n'
        f"BACKEND_ROOT = r\"{BACKEND_ROOT}\"\n",
        encoding="utf-8",
    )

    (path / "actions.jsonl").touch(exist_ok=True)
    (path / "CHAT.md").write_text(brief(snapshot, path), encoding="utf-8")

    # A read-only copy of the database, for questions the JSON snapshot cannot
    # answer (audit trail of promotions, prior evaluation runs).
    try:
        shutil.copy2(db.DB_PATH, path / "screening.db")
    except OSError as exc:
        logger.warning("Could not copy the database into the chat workspace: %s", exc)

    return path


def brief(snapshot: dict[str, Any], path: Path) -> str:
    """The CHAT.md a harness reads before its first turn."""
    quals = snapshot["qualifications"]
    required = [q for q in quals if q["kind"] == "required"]
    preferred = [q for q in quals if q["kind"] == "preferred"]
    tools = path / "tools"

    qual_lines = "\n".join(f"- `{q['label']}` ({q['kind']}) — {q['text']}" for q in quals)
    stage_lines = "\n".join(
        f"- {STAGE_LABELS.get(k, k)}: {v} candidate(s)" for k, v in sorted(snapshot["stages"].items())
    )

    return f"""# Screening chat brief

You are answering questions about one resume screening, for a hiring reviewer
looking at **{snapshot['stage_label']}**.

## The screening

- Position: {snapshot['screening']['job_title'] or '(untitled)'}
- Evaluated with: {snapshot['screening']['provider'] or '—'} / {snapshot['screening']['model'] or '—'}
- Candidates: {len(snapshot['candidates'])} ({len(required)} required, {len(preferred)} preferred qualifications)

{stage_lines}

## Qualifications

{qual_lines}

## How the numbers work

- Every candidate has a per-qualification verdict: **Meets**, **Partial** or **No**, with
  evidence quoted from their resume. Those verdicts are the only thing a model produced.
- Coverage rollups (required met N/M, preferred met N/M), the stage-1 pass
  recommendation and the rank order are computed **in code**, never by a model.
- Stage-1 pass recommendation = *every* required qualification is `Meets`.
  `Partial` does not pass.
- Rank order = required met (desc), then preferred met (desc), then name (asc).
  Ties share a rank.
- Nothing advances automatically. A human promotes or rejects at every stage and
  may override the AI in either direction.

## Data

`snapshot.json` in this directory holds the whole screening: qualifications,
candidates, verdicts, evidence, stage placement and rank. `screening.db` is a
read-only SQLite copy if you need the promotion audit trail.

## Tools

Run these with the workspace's python. Each prints one JSON object on stdout.

```
python {tools / 'query_candidates.py'} [--stage 1] [--name X] [--qual R1] [--verdict Meets] [--evidence]
python {tools / 'explain_rank.py'} "Candidate A" "Candidate B"
python {tools / 'stage_stats.py'} --stage 1
python {tools / 'whatif.py'} --remove-qual R3 P1 [--stage 1]
python {tools / 'grid_action.py'} [--sort required:desc] [--filter R1=Meets] [--clear]
```

- `whatif.py` is a real recompute using the same ranking code as the grid — quote
  its numbers directly, never estimate them.
- `grid_action.py` changes what the user sees in their grid. Use it when they ask
  you to sort, filter or reset the view; it does not modify any candidate.

## Answering

Answer in prose for a hiring reviewer, not in JSON. Cite specific candidates,
verdicts and evidence. If a tool contradicts your expectation, the tool is right.
Never invent a verdict, a count or a rank — read them from the snapshot or a tool.
"""


def drain_actions(session_id: str) -> list[dict[str, Any]]:
    """Read and clear the grid actions a turn queued."""
    path = workspace_path(session_id) / "actions.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    path.write_text("", encoding="utf-8")

    actions = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            actions.append(json.loads(line))
        except ValueError:
            logger.warning("Discarding malformed grid action: %s", line[:200])
    return actions


def destroy_workspace(session_id: str) -> None:
    shutil.rmtree(workspace_path(session_id), ignore_errors=True)
