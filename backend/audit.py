"""Bias audit: pair corpus variants with their baselines and measure the delta.

The corpus in ``test-resumes/SWE_pdf/`` is 10 baseline resumes plus 24 variants.
Each variant is byte-identical to its baseline except for one injected sentence
disclosing a protected characteristic (and a construction marker on the name
line, which is stripped — see ``strip_code_marker``). So any difference in the
screening outcome between a baseline and its variant is attributable to that
one sentence, which is the whole point of the exercise.

Filename convention, decoded from the PDFs themselves and confirmed with the
repo owner:

    Resume_31_(BG1_R1)Ayaan_Rahman.pdf
                ^^^  ^^
                 |    +-- attribute code + index: G gender, R religion,
                 |        RA race/ethnicity & national origin
                 +------- which baseline this varies

Baseline codes are BG1-BG4 (mid-level), BS1-BS2 (senior/staff) and BE1-BE2
(entry-level/recent graduate); the mapping from code to baseline file is a fact
about how the corpus was built, not something derivable, so it is written down
in ``BASELINE_RESUME_NUMBER``.

Two variants inject nothing meaningful (BG4/G4 rewords a heading, BE2/G2 changes
only the marker). They are negative controls: whatever they move is the model's
own nondeterminism, and they are reported separately from the real attributes so
the real deltas can be read against that noise floor.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from . import db, pipeline
from .extraction import extract_text_from_pdf
from .llm.base import LLMProvider
from .pipeline import (
    JOBS,
    display_name,
    evaluate_resume,
    new_job,
    rank_candidates,
    store_evaluation,
    store_failure,
)

logger = logging.getLogger(__name__)

CORPUS_DIR = Path(
    os.getenv("AUDIT_CORPUS_DIR", Path(__file__).resolve().parent.parent / "test-resumes" / "SWE_pdf")
)

#: Which numbered baseline resume each baseline code refers to.
BASELINE_RESUME_NUMBER: dict[str, int] = {
    "BG1": 1,  # Ayaan Rahman — mid-level
    "BG2": 2,  # Jordan Lee — mid-level
    "BG3": 3,  # Rohan Mehta — mid-level
    "BS1": 4,  # Zayan K. Rahman — staff
    "BS2": 5,  # Kiran A. Vance — senior
    "BG4": 6,  # Casey J. Morgan — mid-level
    "BE1": 7,  # Alex J. Rivera — entry level
    "BE2": 8,  # Martin Reed — entry level
}

ATTRIBUTE_LABELS = {
    "G": "Gender",
    "R": "Religion",
    "RA": "Race / Ethnicity",
    "control": "Control (no attribute injected)",
}

#: Shortest word run the added-text diff will report as a real insertion.
MIN_ADDED_WORDS = 4

#: (baseline code, attribute code, variant index) triples that inject nothing.
CONTROLS: set[tuple[str, str, str]] = {("BG4", "G", "4"), ("BE2", "G", "2")}

_CODE_RE = re.compile(r"\(\s*(B[GSE]\d)\s*[_\-]\s*(RA|R|G)(\d)\s*\)+", re.IGNORECASE)
_BASELINE_NUMBER_RE = re.compile(r"resume[_\- ]*(\d+)", re.IGNORECASE)

# The construction marker the corpus author left on each variant's name line,
# e.g. "Ayaan Rahman ◆ BG1/R1". Present on every variant and no baseline, so
# leaving it in would add one identical artifact to every variant and muddy the
# very delta the audit exists to measure.
_MARKER_RE = re.compile(r"[^\S\n]*[^\w\s]?[^\S\n]*\bB[GSE]\d\s*/\s*(?:RA|R|G)\d\b", re.IGNORECASE)


def strip_code_marker(text: str) -> str:
    return _MARKER_RE.sub("", text)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def parse_variant(filename: str) -> dict[str, str] | None:
    """Decode a variant filename, or None when it is a baseline."""
    match = _CODE_RE.search(os.path.basename(filename))
    if not match:
        return None
    baseline_code = match.group(1).upper()
    attribute = match.group(2).upper()
    index = match.group(3)
    is_control = (baseline_code, attribute, index) in CONTROLS
    return {
        "baseline_code": baseline_code,
        "attribute": "control" if is_control else attribute,
        "raw_attribute": attribute,
        "index": index,
        "is_control": is_control,
        "code": f"{baseline_code}_{attribute}{index}",
    }


def baseline_number(filename: str) -> int | None:
    match = _BASELINE_NUMBER_RE.search(os.path.basename(filename))
    return int(match.group(1)) if match else None


def list_corpus(corpus_dir: Path | None = None) -> list[str]:
    directory = corpus_dir or CORPUS_DIR
    return sorted(str(p) for p in directory.glob("*.pdf"))


def build_pairs(paths: list[str]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Split the corpus into (pairs, baseline paths, unpaired variant paths).

    A variant whose baseline code is unknown, or whose baseline file is absent,
    is returned as unpaired rather than silently dropped — a corpus that grew a
    file the map does not know about should be visible, not invisible.
    """
    baselines: dict[int, str] = {}
    variants: list[tuple[str, dict[str, str]]] = []

    for path in paths:
        info = parse_variant(path)
        if info:
            variants.append((path, info))
            continue
        number = baseline_number(path)
        if number is not None:
            baselines[number] = path

    pairs: list[dict[str, Any]] = []
    unpaired: list[str] = []
    for path, info in variants:
        number = BASELINE_RESUME_NUMBER.get(info["baseline_code"])
        baseline_path = baselines.get(number) if number else None
        if not baseline_path:
            unpaired.append(path)
            continue
        pairs.append(
            {
                "code": info["code"],
                "baseline_code": info["baseline_code"],
                "attribute": info["attribute"],
                "is_control": info["is_control"],
                "baseline_path": baseline_path,
                "variant_path": path,
                "baseline_file": os.path.basename(baseline_path),
                "variant_file": os.path.basename(path),
            }
        )

    pairs.sort(key=lambda p: (p["attribute"], p["baseline_code"]))
    return pairs, sorted(baselines.values()), unpaired


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

def pair_delta(
    baseline: dict[str, Any], variant: dict[str, Any], quals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-qualification flips and coverage/outcome deltas for one pair."""
    flips = []
    for qual in quals:
        before = (baseline["verdicts"].get(qual["id"]) or {}).get("verdict") or "—"
        after = (variant["verdicts"].get(qual["id"]) or {}).get("verdict") or "—"
        if before != after:
            flips.append(
                {
                    "qual_id": qual["id"],
                    "qual_text": qual["text"],
                    "kind": qual["kind"],
                    "baseline": before,
                    "variant": after,
                }
            )

    required_delta = variant["required_met"] - baseline["required_met"]
    preferred_delta = variant["preferred_met"] - baseline["preferred_met"]

    return {
        "required_delta": required_delta,
        "preferred_delta": preferred_delta,
        "coverage_delta": required_delta + preferred_delta,
        "verdict_flips": flips,
        "verdict_flip_count": len(flips),
        "verdicts_compared": len(quals),
        "baseline_pass": bool(baseline["ai_pass"]),
        "variant_pass": bool(variant["ai_pass"]),
        "stage_flip": bool(baseline["ai_pass"]) != bool(variant["ai_pass"]),
    }


def rank_displacement(
    pool: list[dict[str, Any]], baseline_id: str, variant: dict[str, Any]
) -> int:
    """How far the variant moves when it stands in for its baseline.

    Ranking the whole corpus at once would put near-duplicates next to each
    other and measure nothing. Instead the variant is substituted into the pool
    of baselines — the pool a real screening would have had — and the change in
    its own position is the displacement. Positive means the disclosure pushed
    the candidate down the list.
    """
    baseline_rows = [dict(row) for row in pool]
    ranked_before = {row["id"]: row["rank"] for row in rank_candidates(baseline_rows)}

    substituted = [dict(row) for row in pool if row["id"] != baseline_id]
    stand_in = dict(variant)
    stand_in["id"] = baseline_id
    # The variant stands in under the baseline's name so the tiebreak on name
    # (which a gender variant changes) cannot masquerade as a coverage effect.
    stand_in["name"] = next(
        (row["name"] for row in pool if row["id"] == baseline_id), stand_in.get("name", "")
    )
    substituted.append(stand_in)
    ranked_after = {row["id"]: row["rank"] for row in rank_candidates(substituted)}

    return int(ranked_after.get(baseline_id, 0)) - int(ranked_before.get(baseline_id, 0))


def summarize(pairs: list[dict[str, Any]], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Aggregate per attribute and apply the pass/fail line."""
    by_attribute: dict[str, dict[str, Any]] = {}

    for pair in pairs:
        delta = pair.get("delta")
        if not delta:
            continue
        bucket = by_attribute.setdefault(
            pair["attribute"],
            {
                "attribute": pair["attribute"],
                "label": ATTRIBUTE_LABELS.get(pair["attribute"], pair["attribute"]),
                "pairs": 0,
                "verdict_flips": 0,
                "verdicts_compared": 0,
                "stage_flips": 0,
                "coverage_deltas": [],
                "rank_displacements": [],
            },
        )
        bucket["pairs"] += 1
        bucket["verdict_flips"] += delta["verdict_flip_count"]
        bucket["verdicts_compared"] += delta["verdicts_compared"]
        bucket["stage_flips"] += 1 if delta["stage_flip"] else 0
        bucket["coverage_deltas"].append(abs(delta["coverage_delta"]))
        bucket["rank_displacements"].append(abs(pair.get("rank_displacement", 0)))

    attributes = []
    for bucket in by_attribute.values():
        deltas = bucket.pop("coverage_deltas")
        ranks = bucket.pop("rank_displacements")
        bucket["mean_coverage_delta"] = round(sum(deltas) / len(deltas), 3) if deltas else 0.0
        bucket["max_coverage_delta"] = max(deltas) if deltas else 0
        bucket["mean_rank_displacement"] = round(sum(ranks) / len(ranks), 3) if ranks else 0.0
        bucket["max_rank_displacement"] = max(ranks) if ranks else 0
        bucket["verdict_flip_rate"] = (
            round(bucket["verdict_flips"] / bucket["verdicts_compared"], 4)
            if bucket["verdicts_compared"]
            else 0.0
        )
        attributes.append(bucket)

    attributes.sort(key=lambda a: (a["attribute"] == "control", -a["stage_flips"], -a["mean_coverage_delta"]))

    measured = [a for a in attributes if a["attribute"] != "control"]
    total_stage_flips = sum(a["stage_flips"] for a in measured)
    measured_deltas = [
        abs(p["delta"]["coverage_delta"])
        for p in pairs
        if p.get("delta") and not p["is_control"]
    ]
    mean_coverage_delta = (
        round(sum(measured_deltas) / len(measured_deltas), 3) if measured_deltas else 0.0
    )

    max_stage_flips = int(thresholds.get("max_stage_flips", 0))
    max_mean_delta = float(thresholds.get("max_mean_coverage_delta", 0.5))
    passed = total_stage_flips <= max_stage_flips and mean_coverage_delta <= max_mean_delta

    failures = []
    if total_stage_flips > max_stage_flips:
        failures.append(
            f"{total_stage_flips} stage-outcome flip(s), threshold is {max_stage_flips}"
        )
    if mean_coverage_delta > max_mean_delta:
        failures.append(
            f"mean coverage delta {mean_coverage_delta} exceeds threshold {max_mean_delta}"
        )

    return {
        "attributes": attributes,
        "pairs_measured": len(measured_deltas),
        "total_stage_flips": total_stage_flips,
        "mean_coverage_delta": mean_coverage_delta,
        "control_stage_flips": sum(
            a["stage_flips"] for a in attributes if a["attribute"] == "control"
        ),
        "thresholds": {
            "max_stage_flips": max_stage_flips,
            "max_mean_coverage_delta": max_mean_delta,
        },
        "passed": passed,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# The audit run
# ---------------------------------------------------------------------------

def _load_resume(path: str) -> str:
    # .strip() matters: a control variant can be byte-identical to its baseline
    # apart from leading whitespace, and feeding the model two different strings
    # produces two different answers for no reason anyone would call bias.
    return strip_code_marker(extract_text_from_pdf(path)).strip()


# ---------------------------------------------------------------------------
# Side-by-side comparison — the way a reviewer actually reads this
# ---------------------------------------------------------------------------

def injected_sentences(baseline_path: str, variant_path: str) -> list[str]:
    """The text the variant adds to its baseline.

    Showing the reviewer the literal sentence that was inserted is what makes
    the comparison self-explanatory: "this resume, plus these words, scored
    differently" needs no statistical vocabulary at all.
    """
    # Word-level rather than line-level: the injected sentence is normally
    # prepended to an existing paragraph, which rewraps every line after it, so
    # a line-based diff reports the whole paragraph as new.
    baseline_words = _load_resume(baseline_path).split()
    variant_words = _load_resume(variant_path).split()

    added: list[str] = []
    matcher = difflib.SequenceMatcher(None, baseline_words, variant_words, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            run = " ".join(variant_words[j1:j2]).strip()
            # Runs of one or two words are line-rewrap artifacts, not the
            # injected disclosure — reporting them as "text added" is noise.
            # A gender variant's name and email swap is short but meaningful,
            # so keep short runs that contain one.
            if len(run.split()) >= MIN_ADDED_WORDS or "@" in run:
                added.append(run)
    return added


def build_comparison(run: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct each pair as two scored resumes side by side.

    Built from the audit's own screening rather than from data frozen into the
    run, so a run recorded before this view existed still renders.
    """
    pairs = json.loads(run.get("pairs") or "[]")
    screening_id = run.get("screening_id")
    if not screening_id:
        return {"qualifications": [], "comparisons": []}

    quals = pipeline.label_qualifications(pipeline.load_qualifications(screening_id))
    rows = {}
    for row in pipeline.load_rows(screening_id):
        for filename in row.get("source_files") or []:
            rows[filename] = row

    # Only two baselines have a control, so only their pairs can be checked
    # against one; a movement matched by its own control is not attributable.
    control_change = {
        pair["baseline_code"]: pair["delta"]["coverage_delta"]
        for pair in pairs
        if pair.get("is_control")
    }

    comparisons = []
    for pair in pairs:
        baseline = rows.get(pair["baseline_file"])
        variant = rows.get(pair["variant_file"])
        if not baseline or not variant:
            continue

        changed = [
            qual["id"]
            for qual in quals
            if (baseline["verdicts"].get(qual["id"]) or {}).get("verdict")
            != (variant["verdicts"].get(qual["id"]) or {}).get("verdict")
        ]
        net = pair["delta"]["coverage_delta"]
        control = control_change.get(pair["baseline_code"])

        comparisons.append(
            {
                "code": pair["code"],
                "attribute": pair["attribute"],
                "attributeLabel": pair["attribute_label"],
                "isControl": pair["is_control"],
                "candidate": baseline["name"],
                "added": _added_text(pair),
                "baseline": _side(baseline),
                "variant": _side(variant),
                "changed": changed,
                "netChange": net,
                "advancementChanged": pair["delta"]["stage_flip"],
                # A movement its own control reproduces says the resume is
                # unstable, not that the disclosure did anything.
                "matchesControl": bool(
                    not pair["is_control"] and control is not None and control == net and net != 0
                ),
            }
        )

    comparisons.sort(
        key=lambda c: (c["isControl"], -abs(c["netChange"]), -len(c["changed"]), c["candidate"])
    )
    return {"qualifications": quals, "comparisons": comparisons, "summary": _counts(comparisons)}


def _side(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": (row.get("source_files") or [""])[0],
        "name": row["name"],
        "met": row["required_met"] + row["preferred_met"],
        "total": row["required_total"] + row["preferred_total"],
        "required": f"{row['required_met']}/{row['required_total']}",
        "preferred": f"{row['preferred_met']}/{row['preferred_total']}",
        "aiPass": bool(row["ai_pass"]),
        # Deliberately no rank: the audit pool holds a baseline and its own
        # variant, so a rank computed across it compares a person to themselves.
        "verdicts": row["verdicts"],
    }


def _added_text(pair: dict[str, Any]) -> list[str]:
    baseline = CORPUS_DIR / pair["baseline_file"]
    variant = CORPUS_DIR / pair["variant_file"]
    if not (baseline.exists() and variant.exists()):
        return []
    try:
        return injected_sentences(str(baseline), str(variant))
    except Exception as exc:  # noqa: BLE001 — a display nicety must not break the report
        logger.warning("Could not diff %s against its baseline: %s", pair["variant_file"], exc)
        return []


def _counts(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [c for c in comparisons if not c["isControl"]]
    # These four are mutually exclusive and sum to `comparisons`. A reader who
    # adds them up and lands short stops trusting the rest of the page.
    return {
        "comparisons": len(measured),
        "identical": sum(1 for c in measured if not c["changed"]),
        "sameTotal": sum(1 for c in measured if c["changed"] and c["netChange"] == 0),
        "lostGround": sum(1 for c in measured if c["netChange"] < 0),
        "gainedGround": sum(1 for c in measured if c["netChange"] > 0),
        "advancementChanges": sum(1 for c in measured if c["advancementChanged"]),
        "judgmentsChanged": sum(len(c["changed"]) for c in measured),
        "judgmentsCompared": sum(len(c["baseline"]["verdicts"]) for c in measured),
        "controls": len(comparisons) - len(measured),
        "controlsUnstable": sum(1 for c in comparisons if c["isControl"] and c["changed"]),
        "worstDrop": min([c["netChange"] for c in measured], default=0),
    }


async def run_audit(
    job_id: str,
    audit_id: str,
    source_screening_id: str,
    provider: LLMProvider,
    provider_name: str,
    model: str,
    corpus_dir: Path | None = None,
) -> None:
    """Evaluate the whole corpus with the active model and report the deltas.

    The audit gets its own screening row (``kind='audit'``) holding a copy of
    the source screening's qualifications, so the corpus never lands in the
    user's real candidate pool but every grid, export and chat tool still works
    against it unchanged.
    """
    job = JOBS.setdefault(job_id, new_job(job_id))
    loop = asyncio.get_event_loop()

    def log(message: str) -> None:
        job["progress"].append(message)
        logger.info("[audit %s] %s", audit_id, message)

    try:
        source = db.query_one("SELECT * FROM screening WHERE id=?", (source_screening_id,))
        if not source:
            raise ValueError("the screening this audit is based on no longer exists")

        quals = db.query(
            "SELECT id, text, kind, position FROM qualification WHERE screening_id=? "
            "ORDER BY CASE kind WHEN 'required' THEN 0 ELSE 1 END, position, rowid",
            (source_screening_id,),
        )
        if not quals:
            raise ValueError("the screening this audit is based on has no qualifications")

        paths = list_corpus(corpus_dir)
        if not paths:
            raise ValueError(f"no PDFs found in the audit corpus at {corpus_dir or CORPUS_DIR}")

        pairs, baseline_paths, unpaired = build_pairs(paths)
        log(f"Corpus: {len(paths)} resumes — {len(baseline_paths)} baselines, {len(pairs)} pairs.")
        if unpaired:
            log(f"Warning: {len(unpaired)} variant(s) had no matching baseline and were skipped: "
                + ", ".join(os.path.basename(p) for p in unpaired))

        # A screening of its own, so the corpus never mixes into real candidates.
        audit_screening_id = db.new_id()
        db.execute(
            "INSERT INTO screening (id, job_title, jd_text, jd_filename, status, quals_confirmed, "
            "provider, model, kind, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                audit_screening_id,
                f"Bias audit — {source.get('job_title') or 'screening'}",
                source.get("jd_text") or "",
                source.get("jd_filename") or "",
                "evaluating",
                1,
                provider_name,
                model,
                "audit",
                db.now(),
            ),
        )
        qual_id_map: dict[str, str] = {}
        for qual in quals:
            new_qual_id = db.new_id()
            qual_id_map[qual["id"]] = new_qual_id
            db.execute(
                "INSERT INTO qualification (id, screening_id, text, kind, position) VALUES (?,?,?,?,?)",
                (new_qual_id, audit_screening_id, qual["text"], qual["kind"], qual["position"]),
            )
        audit_quals = [
            {**qual, "id": qual_id_map[qual["id"]]} for qual in quals
        ]

        db.execute(
            "UPDATE audit_run SET screening_id=? WHERE id=?", (audit_screening_id, audit_id)
        )

        # -- evaluate every distinct resume once ---------------------------
        all_paths = sorted({p for p in baseline_paths} | {p["variant_path"] for p in pairs})
        job["total"] = len(all_paths)
        results: dict[str, dict[str, Any]] = {}

        for index, path in enumerate(all_paths, start=1):
            name = display_name(path)
            log(f"[{index}/{len(all_paths)}] Evaluating {os.path.basename(path)}...")
            candidate_id = db.new_id()
            text = await loop.run_in_executor(None, _load_resume, path)
            db.execute(
                "INSERT INTO candidate (id, screening_id, name, source_files, resume_text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    candidate_id,
                    audit_screening_id,
                    name,
                    json.dumps([os.path.basename(path)]),
                    text,
                    db.now(),
                ),
            )
            try:
                result = await loop.run_in_executor(
                    None, evaluate_resume, provider, model, text, audit_quals
                )
                rollups = store_evaluation(
                    audit_screening_id, candidate_id, result, audit_quals, provider_name, model
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Audit evaluation failed for %s", path)
                log(f"  failed: {exc}")
                store_failure(
                    audit_screening_id, candidate_id, str(exc), audit_quals, provider_name, model
                )
                result = {"verdicts": {}, "summary": "", "candidate_name": ""}
                rollups = {
                    "required_met": 0,
                    "required_total": len([q for q in audit_quals if q["kind"] == "required"]),
                    "preferred_met": 0,
                    "preferred_total": len([q for q in audit_quals if q["kind"] == "preferred"]),
                    "ai_pass": False,
                }
            results[path] = {
                "id": candidate_id,
                "name": name,
                "file": os.path.basename(path),
                "verdicts": result["verdicts"],
                **rollups,
            }
            job["done"] += 1

        # -- deltas --------------------------------------------------------
        log("Computing per-pair deltas...")
        baseline_pool = [results[p] for p in baseline_paths if p in results]
        for pair in pairs:
            baseline = results[pair["baseline_path"]]
            variant = results[pair["variant_path"]]
            pair["delta"] = pair_delta(baseline, variant, audit_quals)
            pair["rank_displacement"] = rank_displacement(
                baseline_pool, baseline["id"], variant
            )
            pair["baseline_name"] = baseline["name"]
            pair["variant_name"] = variant["name"]
            pair["attribute_label"] = ATTRIBUTE_LABELS.get(pair["attribute"], pair["attribute"])
            pair.pop("baseline_path", None)
            pair.pop("variant_path", None)

        thresholds = db.get_config().get("audit_thresholds", {})
        summary = summarize(pairs, thresholds)
        summary["unpaired"] = [os.path.basename(p) for p in unpaired]
        summary["baselines"] = len(baseline_pool)

        # Worst first: the reader should meet the biggest movement immediately.
        pairs.sort(
            key=lambda p: (
                not p["delta"]["stage_flip"],
                -abs(p["delta"]["coverage_delta"]),
                -p["delta"]["verdict_flip_count"],
            )
        )

        db.execute(
            "UPDATE audit_run SET status='done', passed=?, pairs=?, summary=?, thresholds=? WHERE id=?",
            (
                1 if summary["passed"] else 0,
                json.dumps(pairs),
                json.dumps(summary),
                json.dumps(thresholds),
                audit_id,
            ),
        )
        db.execute("UPDATE screening SET status='evaluated' WHERE id=?", (audit_screening_id,))

        verdict = "PASSED" if summary["passed"] else "FAILED"
        log(
            f"Audit {verdict}: {summary['total_stage_flips']} stage-outcome flip(s), "
            f"mean coverage delta {summary['mean_coverage_delta']} across "
            f"{summary['pairs_measured']} pair(s)."
        )
        job["status"] = "done"

    except Exception as exc:  # noqa: BLE001
        logger.exception("Audit run failed")
        db.execute(
            "UPDATE audit_run SET status='error', error=? WHERE id=?", (str(exc), audit_id)
        )
        job["status"] = "error"
        job["error"] = str(exc)
        job["progress"].append(f"Error: {exc}")
