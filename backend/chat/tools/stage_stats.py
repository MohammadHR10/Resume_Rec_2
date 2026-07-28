#!/usr/bin/env python
"""Aggregates for a stage: who passes, what they miss, how coverage is spread.

This is what answers "why did so many make it past this stage?" — the answer is
almost always a qualification nearly everyone Meets, which shows up here as a
near-zero failure rate on that row.
"""

from __future__ import annotations

from collections import Counter

from _common import argument_parser, emit, load_snapshot, stage_label


def main() -> None:
    parser = argument_parser("Aggregate statistics for one stage of a screening.")
    parser.add_argument("--stage", default="1", help="Stage to summarize (1, 2, 3, rejected, all).")
    args = parser.parse_args()

    snapshot = load_snapshot(args)
    quals = snapshot["qualifications"]
    rows = snapshot["candidates"]
    if args.stage != "all":
        rows = [r for r in rows if str(r["stage"]) == str(args.stage)]

    total = len(rows)
    passing = sum(1 for r in rows if r["ai_pass"])
    errored = sum(1 for r in rows if r.get("error"))

    per_qual = []
    for qual in quals:
        counts = Counter(
            (r["verdicts"].get(qual["id"]) or {}).get("verdict") or "Missing" for r in rows
        )
        met = counts.get("Meets", 0)
        per_qual.append(
            {
                "qualification": qual["label"],
                "kind": qual["kind"],
                "text": qual["text"],
                "meets": met,
                "partial": counts.get("Partial", 0),
                "no": counts.get("No", 0),
                "missing": counts.get("Missing", 0),
                "meets_rate": round(met / total, 3) if total else 0.0,
                "failure_rate": round((total - met) / total, 3) if total else 0.0,
            }
        )

    most_missed = sorted(per_qual, key=lambda q: -q["failure_rate"])[:5]
    easiest = sorted(per_qual, key=lambda q: q["failure_rate"])[:5]

    required_hist = Counter(r["required_met"] for r in rows)
    preferred_hist = Counter(r["preferred_met"] for r in rows)

    emit(
        {
            "stage": args.stage,
            "stage_label": stage_label(args.stage) if args.stage != "all" else "All stages",
            "candidates": total,
            "ai_pass": passing,
            "ai_fail": total - passing,
            "ai_pass_rate": round(passing / total, 3) if total else 0.0,
            "evaluation_errors": errored,
            "pass_rule": (
                "A candidate is recommended to pass stage 1 only when every required "
                "qualification is 'Meets'. 'Partial' does not pass. Computed in code."
            ),
            "per_qualification": per_qual,
            "most_missed": most_missed,
            "least_missed": easiest,
            "required_met_distribution": dict(sorted(required_hist.items())),
            "preferred_met_distribution": dict(sorted(preferred_hist.items())),
            "stage_counts": snapshot.get("stages", {}),
        }
    )


if __name__ == "__main__":
    main()
