#!/usr/bin/env python
"""Queue a sort/filter/clear action for the stage grid the user is looking at.

Actions are appended to ``actions.jsonl`` in the workspace rather than posted
over HTTP: the workspace is the one thing both chat modes are guaranteed to
share, it needs no credentials, and the backend drains the file into the
session's action queue after every turn.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from _common import argument_parser, emit, fail, load_snapshot, resolve_qual, workspace_dir

SORTABLE = {"name", "rank", "required", "preferred", "ai"}


def main() -> None:
    parser = argument_parser("Sort, filter or reset the stage grid in the user's browser.")
    parser.add_argument(
        "--sort",
        metavar="COLUMN[:asc|desc]",
        help="Column to sort by: name, rank, required, preferred, ai, or a qualification label (R1).",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="COLUMN=VALUE",
        help="Filter, repeatable. Qualification labels take a verdict (R1=Meets); "
        "'ai' takes Pass/Fail; 'name' takes a substring.",
    )
    parser.add_argument("--clear", action="store_true", help="Clear all sorts and filters.")
    args = parser.parse_args()

    if not (args.sort or args.filter or args.clear):
        fail("nothing to do — pass --sort, --filter or --clear")

    snapshot = load_snapshot(args)
    action: dict[str, object] = {"type": "grid", "clear": bool(args.clear)}

    if args.sort:
        column, _, direction = args.sort.partition(":")
        column = column.strip()
        direction = (direction or "asc").strip().lower()
        if direction not in ("asc", "desc"):
            fail(f"sort direction must be asc or desc, got {direction!r}")
        resolved = _resolve_column(snapshot, column)
        if not resolved:
            fail(f"unknown sort column {column!r}", known=_known_columns(snapshot))
        action["sort"] = {"column": resolved, "direction": direction}

    filters = []
    for raw in args.filter:
        column, _, value = raw.partition("=")
        if not value:
            fail(f"filter must be COLUMN=VALUE, got {raw!r}")
        resolved = _resolve_column(snapshot, column.strip())
        if not resolved:
            fail(f"unknown filter column {column!r}", known=_known_columns(snapshot))
        filters.append({"column": resolved, "value": value.strip()})
    if filters:
        action["filters"] = filters

    path = workspace_dir(args) / "actions.jsonl"
    action["queued_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(action) + "\n")

    emit({"queued": action, "note": "The grid will update when this turn finishes."})


def _resolve_column(snapshot: dict, column: str) -> str | None:
    if column in SORTABLE:
        return column
    qual = resolve_qual(snapshot, column)
    return f"qual:{qual['id']}" if qual else None


def _known_columns(snapshot: dict) -> list[str]:
    return sorted(SORTABLE) + [q["label"] for q in snapshot["qualifications"]]


if __name__ == "__main__":
    main()
