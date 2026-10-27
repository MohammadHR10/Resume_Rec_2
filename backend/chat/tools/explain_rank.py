#!/usr/bin/env python
"""Explain the rank order between two candidates, verdict by verdict."""

from __future__ import annotations

from _common import argument_parser, emit, fail, find_candidate, load_snapshot


def main() -> None:
    parser = argument_parser("Compare two candidates and explain why one outranks the other.")
    parser.add_argument("a", help="First candidate: name, name substring, or id.")
    parser.add_argument("b", help="Second candidate: name, name substring, or id.")
    args = parser.parse_args()

    snapshot = load_snapshot(args)
    quals = snapshot["qualifications"]

    first = find_candidate(snapshot, args.a)
    second = find_candidate(snapshot, args.b)
    if not first or not second:
        missing = [n for n, c in ((args.a, first), (args.b, second)) if not c]
        fail(
            f"could not uniquely identify: {', '.join(missing)}",
            known=[c["name"] for c in snapshot["candidates"]],
        )
    if first["id"] == second["id"]:
        fail("both arguments resolved to the same candidate")

    # The ranking rule, applied here in the same order the grid applies it.
    if first["required_met"] != second["required_met"]:
        decided_by = "required coverage"
        higher = first if first["required_met"] > second["required_met"] else second
    elif first["preferred_met"] != second["preferred_met"]:
        decided_by = "preferred coverage (required coverage is tied)"
        higher = first if first["preferred_met"] > second["preferred_met"] else second
    else:
        decided_by = "name (both coverage counts are tied — they share a rank)"
        higher = first if first["name"].lower() <= second["name"].lower() else second

    differences = []
    for qual in quals:
        a_verdict = (first["verdicts"].get(qual["id"]) or {}).get("verdict") or "—"
        b_verdict = (second["verdicts"].get(qual["id"]) or {}).get("verdict") or "—"
        if a_verdict == b_verdict:
            continue
        differences.append(
            {
                "qualification": qual["label"],
                "kind": qual["kind"],
                "text": qual["text"],
                first["name"]: {
                    "verdict": a_verdict,
                    "evidence": (first["verdicts"].get(qual["id"]) or {}).get("evidence", ""),
                },
                second["name"]: {
                    "verdict": b_verdict,
                    "evidence": (second["verdicts"].get(qual["id"]) or {}).get("evidence", ""),
                },
            }
        )

    emit(
        {
            "ranking_rule": (
                "required qualifications met (desc), then preferred qualifications met (desc), "
                "then candidate name (asc). Computed in code, not by a model."
            ),
            "candidates": [_summary(first), _summary(second)],
            "ranked_higher": higher["name"],
            "decided_by": decided_by,
            "differing_qualifications": differences,
            "identical_verdicts": len(quals) - len(differences),
        }
    )


def _summary(candidate: dict) -> dict:
    return {
        "name": candidate["name"],
        "rank": candidate["rank"],
        "stage": candidate["stage"],
        "required": f"{candidate['required_met']}/{candidate['required_total']}",
        "preferred": f"{candidate['preferred_met']}/{candidate['preferred_total']}",
        "ai_recommendation": "Pass" if candidate["ai_pass"] else "Fail",
        "summary": candidate.get("summary", ""),
    }


if __name__ == "__main__":
    main()
