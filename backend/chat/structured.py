"""Mode B — a mini agentic loop for models that can only emit JSON.

Small open-weight models cannot drive a CLI harness, but they can reliably
produce ``{"tool_calls": [...], "answer": "..."}``. So the loop lives in our
code instead of theirs: the model names a tool, the backend runs it — the same
script the harness would have run — feeds the JSON back, and iterates until the
model has an answer or the round cap is reached.

Tool arguments arrive as a JSON string rather than a nested object because
strict structured-output schemas cannot express free-form objects, and the
models this mode exists for are exactly the ones that need the schema.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..llm.base import LLMError, LLMProvider

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5
TOOL_TIMEOUT_SECONDS = 60
MAX_TOOL_OUTPUT_CHARS = 12_000

# name -> how to turn the model's args into a command line
TOOLS: dict[str, dict[str, Any]] = {
    "query_candidates": {
        "script": "query_candidates.py",
        "description": (
            "List candidates with verdicts and coverage. Args: stage (1|2|3|rejected), "
            "name (substring), rank (grid rank, comma-separated for several: \"3,4,6\"), "
            "qual (label like R1), verdict (Meets|Partial|No, used with qual), "
            "min_required (int), ai_pass (yes|no), evidence (true to include quoted evidence), "
            "limit (int). Prefer ONE call with several ranks over one call per candidate."
        ),
        "params": {
            "stage": {"flag": "--stage"},
            "name": {"flag": "--name"},
            "rank": {"flag": "--rank"},
            "qual": {"flag": "--qual"},
            "verdict": {"flag": "--verdict"},
            "min_required": {"flag": "--min-required"},
            "ai_pass": {"flag": "--ai-pass"},
            "evidence": {"flag": "--evidence", "kind": "flag"},
            "limit": {"flag": "--limit"},
        },
    },
    "explain_rank": {
        "script": "explain_rank.py",
        "description": (
            "Compare two candidates verdict by verdict and explain which ranks higher and why. "
            "Args: a (candidate name), b (candidate name). Both required."
        ),
        "params": {
            "a": {"kind": "positional", "order": 0},
            "b": {"kind": "positional", "order": 1},
        },
    },
    "stage_stats": {
        "script": "stage_stats.py",
        "description": (
            "Aggregates for a stage: pass counts, per-qualification failure rates, coverage "
            "distributions. Use this for 'why did so many pass/fail'. Args: stage (1|2|3|rejected|all)."
        ),
        "params": {"stage": {"flag": "--stage"}},
    },
    "whatif": {
        "script": "whatif.py",
        "description": (
            "Recompute coverage, pass/fail and ranking with qualifications removed, and return the "
            "diff. A real recompute, not an estimate. Args: remove_qual (list of labels like "
            "[\"R3\"]), stage (optional)."
        ),
        "params": {
            "remove_qual": {"flag": "--remove-qual", "kind": "list"},
            "stage": {"flag": "--stage"},
        },
    },
    "grid_action": {
        "script": "grid_action.py",
        "description": (
            "Change the grid the user is looking at. Args: sort (\"required:desc\"), filter "
            "(list like [\"R1=Meets\"]), clear (true). Does not modify any candidate."
        ),
        "params": {
            "sort": {"flag": "--sort"},
            "filter": {"flag": "--filter", "kind": "repeat"},
            "clear": {"flag": "--clear", "kind": "flag"},
        },
    },
}

TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "One sentence on what you need next. Not shown to the user.",
        },
        "tool_calls": {
            "type": "array",
            "description": "Tools to run before answering. Empty when you are ready to answer.",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": sorted(TOOLS)},
                    "args_json": {
                        "type": "string",
                        "description": 'Arguments as a JSON object string, e.g. {"stage":"1"}. Use {} for none.',
                    },
                },
            },
        },
        "answer": {
            "type": "string",
            "description": "The final answer for the hiring reviewer. Empty while still calling tools.",
        },
    },
}


def system_prompt(brief: str) -> str:
    tool_lines = "\n".join(f"- `{name}`: {spec['description']}" for name, spec in sorted(TOOLS.items()))
    return f"""{brief}

---

# How to reply

You cannot run commands yourself. Every reply is one JSON object with these fields:

- `reasoning`: one sentence on what you need next.
- `tool_calls`: a list of tools for the backend to run for you. Each entry is
  `{{"tool": "<name>", "args_json": "<JSON object as a string>"}}`. Use `[]` when
  you are ready to answer.
- `answer`: your final answer in prose. Leave it empty (`""`) while you still
  need tool results.

Available tools:
{tool_lines}

Reply with **exactly one** JSON object and nothing after it — no second object,
no trailing commentary. The backend runs the tools you name and sends their JSON
output back to you. Then you reply again — more tool calls, or a final `answer`.
You have {MAX_ROUNDS} rounds; use them.

Ground every number in tool output or the snapshot. If you have not looked
something up, look it up rather than guessing.
"""


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def build_command(workspace: Path, tool: str, args: dict[str, Any]) -> list[str]:
    spec = TOOLS[tool]
    script = workspace / "tools" / spec["script"]
    command = [sys.executable, str(script), "--workspace", str(workspace)]
    positionals: list[tuple[int, str]] = []

    for key, value in (args or {}).items():
        param = spec["params"].get(key)
        if param is None or value is None or value == "":
            continue
        kind = param.get("kind", "value")
        if kind == "positional":
            positionals.append((param["order"], str(value)))
        elif kind == "flag":
            if value is True or str(value).lower() in ("true", "yes", "1"):
                command.append(param["flag"])
        elif kind == "list":
            values = value if isinstance(value, list) else [value]
            if values:
                command.append(param["flag"])
                command.extend(str(v) for v in values)
        elif kind == "repeat":
            for item in value if isinstance(value, list) else [value]:
                command.extend([param["flag"], str(item)])
        else:
            command.extend([param["flag"], str(value)])

    for _, value in sorted(positionals):
        command.append(value)
    return command


def run_tool(workspace: Path, tool: str, args: dict[str, Any]) -> str:
    if tool not in TOOLS:
        return json.dumps({"error": f"unknown tool {tool!r}", "known": sorted(TOOLS)})
    try:
        command = build_command(workspace, tool, args)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"could not build command: {exc}"})

    logger.info("structured chat running: %s", " ".join(command[1:]))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"{tool} timed out after {TOOL_TIMEOUT_SECONDS}s"})
    except OSError as exc:
        return json.dumps({"error": f"could not run {tool}: {exc}"})

    output = completed.stdout.strip()
    if not output:
        return json.dumps(
            {"error": f"{tool} produced no output", "stderr": completed.stderr[-500:]}
        )
    if len(output) > MAX_TOOL_OUTPUT_CHARS:
        output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n…[truncated]"
    return output


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_turn(
    provider: LLMProvider,
    model: str,
    workspace: Path,
    brief: str,
    history: list[dict[str, str]],
    question: str,
) -> dict[str, Any]:
    """One user question through the loop. Returns ``{answer, trace}``."""
    system = system_prompt(brief)
    messages = list(history) + [{"role": "user", "content": question}]
    trace: list[dict[str, Any]] = []

    for round_index in range(MAX_ROUNDS):
        try:
            reply = provider.chat(
                messages, schema=TURN_SCHEMA, model=model, system=system, schema_name="ChatTurn"
            )
        except LLMError as exc:
            # A malformed turn is recoverable: tell the model what went wrong
            # and spend another round, rather than losing the user's question.
            logger.warning("Chat turn %s was unusable: %s", round_index + 1, exc)
            if round_index >= MAX_ROUNDS - 1:
                raise
            messages.append(
                {
                    "role": "user",
                    "content": f"Your last reply could not be read ({exc}). Reply with exactly "
                    "one JSON object and nothing after it.",
                }
            )
            continue
        calls = [c for c in (reply.get("tool_calls") or []) if isinstance(c, dict) and c.get("tool")]
        answer = str(reply.get("answer") or "").strip()

        if not calls:
            if answer:
                return {"answer": answer, "trace": trace}
            # No tools, no answer — nudge once rather than looping on silence.
            messages.append({"role": "assistant", "content": json.dumps(reply)})
            messages.append(
                {
                    "role": "user",
                    "content": "You returned neither a tool call nor an answer. "
                    "Either call a tool or answer the question now.",
                }
            )
            continue

        messages.append({"role": "assistant", "content": json.dumps(reply)})
        results = []
        for call in calls[:4]:
            tool = str(call.get("tool"))
            args = _parse_args(call.get("args_json") or call.get("args"))
            output = run_tool(workspace, tool, args)
            trace.append({"round": round_index + 1, "tool": tool, "args": args})
            results.append(f"### {tool}\n{output}")
        messages.append({"role": "user", "content": "Tool results:\n\n" + "\n\n".join(results)})

    # Round cap reached: ask for the answer it has, with tools off.
    final = provider.chat(
        messages
        + [
            {
                "role": "user",
                "content": "No more tool calls are available. Answer the question now "
                "using what you have, and say plainly if something is unresolved.",
            }
        ],
        schema=TURN_SCHEMA,
        model=model,
        system=system,
        schema_name="ChatTurn",
    )
    answer = str(final.get("answer") or "").strip()
    if not answer:
        raise LLMError("the model finished its rounds without producing an answer")
    return {"answer": answer, "trace": trace}
