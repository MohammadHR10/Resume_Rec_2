"""Per-candidate evaluation: qualification verdicts, rollups, ranking, batching.

Division of labour, which the requirements are explicit about: the model
produces **only** per-qualification verdicts with evidence. Every number a
human acts on — coverage rollups, the stage-1 pass recommendation, the rank
order — is computed here in code, so it is reproducible, explainable and
identical across models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from . import db
from .extraction import extract_text_from_pdf
from .llm.base import LLMError, LLMProvider

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=8)

# Process this many candidates, then pause so a rate-limit window can refill.
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "30"))
BATCH_DELAY_SECONDS = float(os.getenv("BATCH_DELAY_SECONDS", "30"))

VERDICTS = ("Meets", "Partial", "No")

# In-memory progress for running jobs. Results live in SQLite; this is only the
# event log a polling client reads, and it is expendable across a restart.
JOBS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Candidate grouping
# ---------------------------------------------------------------------------

def candidate_key(filename: str) -> str:
    """Group key for the files belonging to one candidate.

    Two naming conventions show up in practice. Applicant-tracking exports use
    ``LastName, FirstName <doc type>.pdf`` and put a resume and a cover letter
    in separate files, so the first two whitespace tokens identify the person.
    Everything else — including the audit corpus's ``Resume_31_(BG1_R1)Name`` —
    is one file per candidate, where the whole stem is the identity and
    truncating it would collide distinct people.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    tokens = stem.split()
    if len(tokens) >= 2 and tokens[0].endswith(","):
        return " ".join(tokens[:2])
    return stem


def display_name(filename: str) -> str:
    """A human name derived from a filename, for when the model gives none."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    # Drop leading "Resume_12_", parenthesized audit codes, then tidy separators.
    stem = re.sub(r"^resume[_\- ]*\d*[_\- ]*", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\([^)]*\)+", " ", stem)
    stem = stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip() or os.path.basename(filename)


def group_files(paths: Iterable[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        groups[candidate_key(os.path.basename(path))].append(path)
    return dict(groups)


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

EVAL_PROMPT = """\
You are screening one candidate's resume against a fixed checklist of \
qualifications for a single position.

For every qualification in the checklist, return exactly one verdict object:
- qual_id: the id shown in the checklist, copied exactly.
- verdict: one of "Meets", "Partial", "No".
    - "Meets": the resume shows the qualification is fully satisfied, including \
any stated threshold (years, degree level, certification, specific technology).
    - "Partial": the resume shows related but insufficient experience, or the \
qualification is only partly satisfied (e.g. 2 years where 3 are required, a \
related degree where a specific one is required).
    - "No": the resume shows nothing that satisfies the qualification.
- evidence: a short verbatim quote from the resume supporting the verdict. Use \
an empty string when the verdict is "No".

Rules:
- Return a verdict for every qualification, in the order given. Never omit one, \
never invent an id.
- Judge only on evidence present in the resume. Do not assume unstated \
experience, and do not credit a qualification because the candidate seems \
strong overall.
- Base every verdict solely on qualifications, skills and experience. The \
candidate's name, gender, race, age, national origin, and any other personal \
characteristic are irrelevant and must not influence any verdict.
- Do not compute scores, totals or a recommendation — verdicts and evidence only.

Also return candidate_name (the applicant's name as written on the resume, or an \
empty string) and a one-sentence factual summary of the candidate's background.

CHECKLIST
{checklist}

RESUME
{resume}
"""


def build_checklist(quals: list[dict[str, Any]]) -> tuple[str, dict[str, str], list[str]]:
    """Render the checklist and the label↔db-id mapping used to read it back.

    Short labels (``R1``, ``P2``) rather than database ids: they are cheap
    tokens, they are legible in a prompt, and a model is far less likely to
    mangle one than a 16-character hex string.
    """
    lines: list[str] = []
    label_to_id: dict[str, str] = {}
    labels: list[str] = []
    counters = {"required": 0, "preferred": 0}

    for kind, heading in (("required", "Required qualifications"), ("preferred", "Preferred qualifications")):
        subset = [q for q in quals if q["kind"] == kind]
        if not subset:
            continue
        lines.append(f"{heading}:")
        for qual in subset:
            counters[kind] += 1
            label = f"{'R' if kind == 'required' else 'P'}{counters[kind]}"
            label_to_id[label] = qual["id"]
            labels.append(label)
            lines.append(f"  [{label}] {qual['text']}")
        lines.append("")

    return "\n".join(lines).strip(), label_to_id, labels


def evaluation_schema(labels: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidate_name": {"type": "string"},
            "summary": {"type": "string"},
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "qual_id": {"type": "string", "enum": labels},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Rollups and ranking — code, never the model
# ---------------------------------------------------------------------------

def compute_rollups(
    verdicts: dict[str, dict[str, str]], quals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Coverage counts and the stage-1 pass recommendation.

    A qualification with no verdict counts as unmet rather than being dropped
    from the denominator: a model that skips an item must not make a candidate
    look better than one that answered it.
    """
    required = [q for q in quals if q["kind"] == "required"]
    preferred = [q for q in quals if q["kind"] == "preferred"]

    def met(subset: list[dict[str, Any]]) -> int:
        return sum(1 for q in subset if (verdicts.get(q["id"]) or {}).get("verdict") == "Meets")

    required_met = met(required)
    preferred_met = met(preferred)

    return {
        "required_met": required_met,
        "required_total": len(required),
        "preferred_met": preferred_met,
        "preferred_total": len(preferred),
        # Stage-1 recommendation: every required qualification is "Meets".
        # "Partial" does not pass — the human overrides at the gate if it should.
        "ai_pass": required_met == len(required),
    }


def rank_key(row: dict[str, Any]) -> tuple:
    """Required coverage first, preferred as tiebreak, then name for stability."""
    return (
        -int(row.get("required_met") or 0),
        -int(row.get("preferred_met") or 0),
        str(row.get("name") or "").lower(),
    )


def rank_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by the ranking rule and stamp a 1-based ``rank`` on each row.

    Ties share a rank (standard competition ranking) so two candidates with
    identical coverage are never presented as if one beat the other.
    """
    ordered = sorted(rows, key=rank_key)
    previous: tuple | None = None
    previous_rank = 0
    for index, row in enumerate(ordered, start=1):
        key = rank_key(row)[:2]
        if key == previous:
            row["rank"] = previous_rank
        else:
            row["rank"] = index
            previous_rank = index
            previous = key
    return ordered


# ---------------------------------------------------------------------------
# One candidate
# ---------------------------------------------------------------------------

def evaluate_resume(
    provider: LLMProvider,
    model: str,
    resume_text: str,
    quals: list[dict[str, Any]],
) -> dict[str, Any]:
    """One LLM call for one candidate. Returns verdicts keyed by qualification id.

    A single call per candidate rather than one per qualification: the model
    reads the resume once, which is both cheaper and more consistent than
    n independent readings that cannot see each other.
    """
    checklist, label_to_id, labels = build_checklist(quals)
    if not labels:
        raise LLMError("the screening has no qualifications to evaluate against")

    prompt = EVAL_PROMPT.format(checklist=checklist, resume=resume_text)
    raw = provider.structured_extract(
        prompt,
        evaluation_schema(labels),
        model=model,
        schema_name="CandidateEvaluation",
    )

    verdicts: dict[str, dict[str, str]] = {}
    for item in raw.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        qual_id = label_to_id.get(str(item.get("qual_id") or "").strip())
        verdict = str(item.get("verdict") or "").strip().title()
        if not qual_id or verdict not in VERDICTS:
            continue
        verdicts[qual_id] = {
            "verdict": verdict,
            "evidence": str(item.get("evidence") or "").strip(),
        }

    return {
        "candidate_name": str(raw.get("candidate_name") or "").strip(),
        "summary": str(raw.get("summary") or "").strip(),
        "verdicts": verdicts,
    }


def store_evaluation(
    screening_id: str,
    candidate_id: str,
    result: dict[str, Any],
    quals: list[dict[str, Any]],
    provider_name: str,
    model: str,
) -> dict[str, Any]:
    rollups = compute_rollups(result["verdicts"], quals)
    db.execute(
        "INSERT INTO evaluation (id, screening_id, candidate_id, verdicts, required_met, "
        "required_total, preferred_met, preferred_total, ai_pass, summary, error, provider, "
        "model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(candidate_id) DO UPDATE SET verdicts=excluded.verdicts, "
        "required_met=excluded.required_met, required_total=excluded.required_total, "
        "preferred_met=excluded.preferred_met, preferred_total=excluded.preferred_total, "
        "ai_pass=excluded.ai_pass, summary=excluded.summary, error=excluded.error, "
        "provider=excluded.provider, model=excluded.model, created_at=excluded.created_at",
        (
            db.new_id(),
            screening_id,
            candidate_id,
            json.dumps(result["verdicts"]),
            rollups["required_met"],
            rollups["required_total"],
            rollups["preferred_met"],
            rollups["preferred_total"],
            1 if rollups["ai_pass"] else 0,
            result.get("summary", ""),
            result.get("error", ""),
            provider_name,
            model,
            db.now(),
        ),
    )
    return rollups


def store_failure(
    screening_id: str, candidate_id: str, message: str, quals: list[dict[str, Any]],
    provider_name: str, model: str,
) -> None:
    """Record a candidate the model could not evaluate.

    A failed candidate is kept with an error and zero coverage rather than
    dropped: a person silently missing from a screening grid is the one
    outcome a hiring reviewer must never get.
    """
    store_evaluation(
        screening_id,
        candidate_id,
        {"candidate_name": "", "summary": "", "verdicts": {}, "error": message},
        quals,
        provider_name,
        model,
    )


# ---------------------------------------------------------------------------
# The batch job
# ---------------------------------------------------------------------------

def new_job(job_id: str, total: int = 0) -> dict[str, Any]:
    JOBS[job_id] = {
        "status": "running",
        "progress": [],
        "error": None,
        "total": total,
        "done": 0,
    }
    return JOBS[job_id]


async def run_evaluation(
    job_id: str,
    screening_id: str,
    candidates: list[dict[str, Any]],
    quals: list[dict[str, Any]],
    provider: LLMProvider,
    provider_name: str,
    model: str,
) -> None:
    """Evaluate every candidate, writing results to SQLite as they complete."""
    job = JOBS.setdefault(job_id, new_job(job_id))
    job["total"] = len(candidates)
    loop = asyncio.get_event_loop()

    def log(message: str) -> None:
        job["progress"].append(message)
        logger.info("[%s] %s", job_id, message)

    try:
        total = len(candidates)
        total_batches = max(1, (total + BATCH_SIZE - 1) // BATCH_SIZE)
        log(f"Evaluating {total} candidate(s) against {len(quals)} qualification(s) "
            f"using {provider_name}/{model}.")

        for batch_index in range(total_batches):
            batch = candidates[batch_index * BATCH_SIZE : (batch_index + 1) * BATCH_SIZE]
            if batch_index > 0:
                log(f"Pausing {BATCH_DELAY_SECONDS:.0f}s between batches to respect rate limits...")
                await asyncio.sleep(BATCH_DELAY_SECONDS)
            if total_batches > 1:
                log(f"--- Batch {batch_index + 1}/{total_batches} ({len(batch)} candidates) ---")

            for offset, candidate in enumerate(batch, batch_index * BATCH_SIZE + 1):
                name = candidate["name"]
                log(f"[{offset}/{total}] Evaluating {name}...")
                try:
                    result = await loop.run_in_executor(
                        _executor,
                        evaluate_resume,
                        provider,
                        model,
                        candidate["resume_text"],
                        quals,
                    )
                    rollups = store_evaluation(
                        screening_id, candidate["id"], result, quals, provider_name, model
                    )
                    if result["candidate_name"]:
                        db.execute(
                            "UPDATE candidate SET name=? WHERE id=? AND name=?",
                            (result["candidate_name"], candidate["id"], name),
                        )
                    log(
                        f"  {name}: required {rollups['required_met']}/{rollups['required_total']}, "
                        f"preferred {rollups['preferred_met']}/{rollups['preferred_total']} "
                        f"— AI recommends {'PASS' if rollups['ai_pass'] else 'FAIL'}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Evaluation failed for %s", name)
                    store_failure(
                        screening_id, candidate["id"], str(exc), quals, provider_name, model
                    )
                    log(f"  {name}: evaluation failed — {exc}")
                finally:
                    job["done"] += 1

        db.execute(
            "UPDATE screening SET status='evaluated', provider=?, model=? WHERE id=?",
            (provider_name, model, screening_id),
        )
        log(f"Evaluation complete. {total} candidate(s) processed.")
        job["status"] = "done"

    except Exception as exc:  # noqa: BLE001
        logger.exception("Evaluation job failed")
        job["status"] = "error"
        job["error"] = str(exc)
        job["progress"].append(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Reading a screening back out
# ---------------------------------------------------------------------------

def load_qualifications(screening_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, text, kind, position FROM qualification WHERE screening_id=? "
        "ORDER BY CASE kind WHEN 'required' THEN 0 ELSE 1 END, position, rowid",
        (screening_id,),
    )


def load_rows(screening_id: str) -> list[dict[str, Any]]:
    """Every candidate in a screening with verdicts, rollups, stage and rank."""
    rows = db.query(
        "SELECT c.id, c.name, c.source_files, e.verdicts, e.required_met, e.required_total, "
        "e.preferred_met, e.preferred_total, e.ai_pass, e.summary, e.error, e.provider, "
        "e.model, COALESCE(s.stage, '1') AS stage "
        "FROM candidate c "
        "LEFT JOIN evaluation e ON e.candidate_id = c.id "
        "LEFT JOIN stage_state s ON s.candidate_id = c.id "
        "WHERE c.screening_id=?",
        (screening_id,),
    )
    for row in rows:
        row["verdicts"] = json.loads(row.get("verdicts") or "{}")
        row["source_files"] = json.loads(row.get("source_files") or "[]")
        row["ai_pass"] = bool(row.get("ai_pass"))
        for key in ("required_met", "required_total", "preferred_met", "preferred_total"):
            row[key] = int(row.get(key) or 0)
        row["evaluated"] = row.get("provider") is not None
    return rank_candidates(rows)
