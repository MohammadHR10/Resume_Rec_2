#!/usr/bin/env python
"""Filtered dump of candidates, verdicts and evidence from the snapshot."""

from __future__ import annotations

from typing import Any

from _common import argument_parser, emit, load_snapshot, resolve_qual, stage_label


def main() -> None:
    parser = argument_parser("List candidates with their verdicts and coverage.")
    parser.add_argument("--stage", help="Only candidates currently in this stage (1, 2, 3, rejected).")
    parser.add_argument("--name", help="Substring match on candidate name.")
    parser.add_argument(
        "--rank",
        help="Rank shown in the grid, e.g. 3. Repeatable as a comma-separated list: 3,4,6",
    )
    parser.add_argument("--qual", help="Qualification label (R1, P2), id, or text substring.")
    parser.add_argument(
        "--verdict",
        choices=["Meets", "Partial", "No"],
        help="With --qual, keep only candidates holding this verdict on it.",
    )
    parser.add_argument("--min-required", type=int, help="Minimum required qualifications met.")
    parser.add_argument("--ai-pass", choices=["yes", "no"], help="Filter on the AI recommendation.")
    parser.add_argument("--evidence", action="store_true", help="Include the quoted evidence text.")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    snapshot = load_snapshot(args)
    quals = {q["id"]: q for q in snapshot["qualifications"]}
    rows: list[dict[str, Any]] = list(snapshot["candidates"])

    target_qual = resolve_qual(snapshot, args.qual) if args.qual else None
    if args.qual and not target_qual:
        emit({"error": f"no qualification matched {args.qual!r}",
              "known": [f"{q['label']}: {q['text']}" for q in snapshot["qualifications"]]})
        return

    if args.stage:
        rows = [r for r in rows if str(r["stage"]) == str(args.stage)]
    if args.name:
        rows = [r for r in rows if args.name.lower() in r["name"].lower()]
    if args.rank:
        # One call answers "why did 3, 4 and 6 fail?" instead of three.
        wanted = {t.strip().lstrip("#") for t in args.rank.split(",") if t.strip()}
        rows = [r for r in rows if str(r.get("rank")) in wanted]
    if args.min_required is not None:
        rows = [r for r in rows if r["required_met"] >= args.min_required]
    if args.ai_pass:
        want = args.ai_pass == "yes"
        rows = [r for r in rows if bool(r["ai_pass"]) is want]
    if target_qual and args.verdict:
        rows = [
            r
            for r in rows
            if (r["verdicts"].get(target_qual["id"]) or {}).get("verdict") == args.verdict
        ]

    total = len(rows)
    rows = rows[: args.limit]

    out = []
    for row in rows:
        entry = {
            "id": row["id"],
            "name": row["name"],
            "stage": row["stage"],
            "stage_label": stage_label(row["stage"]),
            "rank": row["rank"],
            "required": f"{row['required_met']}/{row['required_total']}",
            "preferred": f"{row['preferred_met']}/{row['preferred_total']}",
            "ai_recommendation": "Pass" if row["ai_pass"] else "Fail",
        }
        if target_qual:
            verdict = row["verdicts"].get(target_qual["id"]) or {}
            entry["qualification"] = target_qual["label"]
            entry["verdict"] = verdict.get("verdict") or "—"
            if args.evidence:
                entry["evidence"] = verdict.get("evidence") or ""
        else:
            entry["verdicts"] = {
                quals[qid]["label"]: (
                    {"verdict": v.get("verdict"), "evidence": v.get("evidence")}
                    if args.evidence
                    else v.get("verdict")
                )
                for qid, v in row["verdicts"].items()
                if qid in quals
            }
        if row.get("error"):
            entry["error"] = row["error"]
        out.append(entry)

    emit({"matched": total, "returned": len(out), "candidates": out})


if __name__ == "__main__":
    main()
