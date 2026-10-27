#!/usr/bin/env python
"""Recompute the screening without some qualifications and report the diff.

Not a prediction. The rollup and ranking functions imported here are the ones
that produced the grid, so removing R3 and re-running them gives exactly the
grid the user would see if R3 had never been on the list.
"""

from __future__ import annotations

from _common import (
    argument_parser,
    compute_rollups,
    emit,
    fail,
    load_snapshot,
    rank_candidates,
    resolve_qual,
    stage_label,
)


def main() -> None:
    parser = argument_parser("Recompute coverage, gate outcomes and ranking without given quals.")
    parser.add_argument(
        "--remove-qual",
        nargs="+",
        required=True,
        metavar="QUAL",
        help="Qualification labels (R1, P2), ids, or text substrings to drop.",
    )
    parser.add_argument("--stage", help="Restrict the diff to candidates in this stage.")
    args = parser.parse_args()

    snapshot = load_snapshot(args)
    quals = snapshot["qualifications"]

    removed = []
    unknown = []
    for token in args.remove_qual:
        qual = resolve_qual(snapshot, token)
        if qual is None:
            unknown.append(token)
        elif qual["id"] not in {q["id"] for q in removed}:
            removed.append(qual)
    if unknown:
        fail(
            f"could not resolve qualification(s): {', '.join(unknown)}",
            known=[f"{q['label']}: {q['text']}" for q in quals],
        )

    removed_ids = {q["id"] for q in removed}
    kept = [q for q in quals if q["id"] not in removed_ids]
    if not kept:
        fail("removing those qualifications would leave the checklist empty")

    rows = snapshot["candidates"]
    if args.stage:
        rows = [r for r in rows if str(r["stage"]) == str(args.stage)]

    before = {r["id"]: r for r in rank_candidates([dict(r) for r in rows])}

    after_rows = []
    for row in rows:
        recomputed = dict(row)
        recomputed.update(compute_rollups(row["verdicts"], kept))
        after_rows.append(recomputed)
    after = {r["id"]: r for r in rank_candidates(after_rows)}

    newly_passing, newly_failing, moved = [], [], []
    for candidate_id, old in before.items():
        new = after[candidate_id]
        entry = {
            "name": old["name"],
            "required_before": f"{old['required_met']}/{old['required_total']}",
            "required_after": f"{new['required_met']}/{new['required_total']}",
            "preferred_before": f"{old['preferred_met']}/{old['preferred_total']}",
            "preferred_after": f"{new['preferred_met']}/{new['preferred_total']}",
            "rank_before": old["rank"],
            "rank_after": new["rank"],
        }
        if not old["ai_pass"] and new["ai_pass"]:
            newly_passing.append(entry)
        elif old["ai_pass"] and not new["ai_pass"]:
            newly_failing.append(entry)
        if old["rank"] != new["rank"]:
            moved.append({**entry, "rank_change": new["rank"] - old["rank"]})

    moved.sort(key=lambda e: (e["rank_after"], e["name"]))
    ranking_after = [
        {
            "rank": row["rank"],
            "name": row["name"],
            "required": f"{row['required_met']}/{row['required_total']}",
            "preferred": f"{row['preferred_met']}/{row['preferred_total']}",
            "ai_recommendation": "Pass" if row["ai_pass"] else "Fail",
        }
        for row in sorted(after.values(), key=lambda r: (r["rank"], r["name"]))
    ]

    emit(
        {
            "removed": [{"label": q["label"], "kind": q["kind"], "text": q["text"]} for q in removed],
            "remaining_required": sum(1 for q in kept if q["kind"] == "required"),
            "remaining_preferred": sum(1 for q in kept if q["kind"] == "preferred"),
            "scope": stage_label(args.stage) if args.stage else "all candidates",
            "candidates_considered": len(rows),
            "ai_pass_before": sum(1 for r in before.values() if r["ai_pass"]),
            "ai_pass_after": sum(1 for r in after.values() if r["ai_pass"]),
            "newly_passing": newly_passing,
            "newly_failing": newly_failing,
            "rank_changes": moved,
            "ranking_after": ranking_after,
            "note": (
                "Deterministic recompute using the same rollup and ranking code as the "
                "grid — no model involved. Nothing has been changed in the screening."
            ),
        }
    )


if __name__ == "__main__":
    main()
