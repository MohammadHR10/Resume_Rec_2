"""Three read-only research tools over one screening; no query language execution.

Filters are a small validated expression vocabulary. Counts cover every matching
candidate, while pages limit only returned records, passages and breakdowns.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from itertools import combinations
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import db
from ..pipeline import load_qualifications, load_rows
from .workspace import qual_labels


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


FieldName = Literal[
    "id", "name", "stage", "rank", "required_met", "preferred_met",
    "required_unmet", "preferred_unmet", "evaluation_status", "recommendation",
    "provider", "model", "source_files", "resume", "summary", "verdict", "evidence", "reasoning",
]
NUMBER_FIELDS = {"rank", "required_met", "preferred_met", "required_unmet", "preferred_unmet"}
VERDICT_FIELDS = {"verdict", "evidence", "reasoning"}
STATUSES = ("Evaluated", "Error", "Not evaluated")
VERDICTS = ("Meets", "Partial", "No", "Missing")


class Condition(Input):
    field: FieldName
    op: Literal["eq", "ne", "in", "not_in", "contains", "not_contains", "gt", "gte", "lt", "lte", "is_empty"] = "eq"
    value: str | int | float | list[str | int | float] | None = None
    qualification: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def valid_operator(self):
        if (self.field in VERDICT_FIELDS) != bool(self.qualification):
            raise ValueError("Only verdict/evidence/reasoning conditions require a qualification label or ID.")
        if self.op == "is_empty":
            if self.field in NUMBER_FIELDS or self.value is not None:
                raise ValueError("is_empty takes no value and requires a text field.")
            return self
        if self.op in ("in", "not_in"):
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 500:
                raise ValueError("in/not_in require a nonempty list of up to 500 values.")
            values = self.value
        else:
            if self.value is None or isinstance(self.value, list):
                raise ValueError("This operator requires one value.")
            values = [self.value]
        numeric = self.field in NUMBER_FIELDS
        if any((type(v) not in (int, float)) if numeric else not isinstance(v, str) for v in values):
            raise ValueError("Use numbers for numeric fields and strings for text fields.")
        if any(isinstance(v, str) and len(v) > 4000 for v in values):
            raise ValueError("Filter values must be at most 4000 characters.")
        if self.op in ("gt", "gte", "lt", "lte") and not numeric:
            raise ValueError("Range comparisons require a numeric field.")
        if self.op in ("contains", "not_contains") and (numeric or not str(self.value).strip()):
            raise ValueError("Text searches require a nonempty literal string.")
        allowed = {"verdict": VERDICTS, "stage": ("1", "2", "3", "rejected"),
                   "evaluation_status": STATUSES, "recommendation": ("Pass", "Fail", "Error", "Not evaluated")}.get(self.field)
        if allowed and self.op in ("eq", "ne", "in", "not_in"):
            if any(str(v).casefold() not in {s.casefold() for s in allowed} for v in values):
                raise ValueError(f"{self.field} must use one of: {', '.join(allowed)}")
        return self


class CandidateFilter(Input):
    """AND every all condition, OR the any list, and exclude every none match."""
    all: list[Condition] = Field(default_factory=list, max_length=20)
    any: list[Condition] = Field(default_factory=list, max_length=20)
    none: list[Condition] = Field(default_factory=list, max_length=20)


class FindCandidates(Input):
    where: CandidateFilter = Field(default_factory=CandidateFilter)
    sort_by: Literal["rank", "name", "required_met", "preferred_met", "stage"] = "rank"
    descending: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class ReadCandidate(Input):
    candidate_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list, max_length=5)
    section: Literal["resume", "verdicts", "summary", "evidence", "reasoning"] = "verdicts"
    qualifications: list[str] = Field(default_factory=list, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=6000, ge=1, le=12000)
    terms: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_section(self):
        if bool(self.candidate_id) == bool(self.candidate_ids):
            raise ValueError("Supply candidate_id or candidate_ids (up to five), not both.")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("Candidate IDs must be unique.")
        if self.terms and self.section != "resume":
            raise ValueError("terms can only search the resume section.")
        if any(not term.strip() or len(term) > 150 for term in self.terms):
            raise ValueError("Search terms must contain 1–150 characters.")
        if self.section in ("evidence", "reasoning") and len(self.qualifications) != 1:
            raise ValueError("Select one exact qualification when paging evidence or reasoning.")
        return self


class Dimension(Input):
    field: Literal["id", "name", "stage", "recommendation", "evaluation_status", "required_met", "preferred_met", "provider", "model", "verdict"]
    qualification: str | None = None

    @model_validator(mode="after")
    def qualification_for_verdict(self):
        if (self.field == "verdict") != bool(self.qualification):
            raise ValueError("Only a verdict grouping requires a qualification label or ID.")
        return self


class Cohort(Input):
    label: str = Field(min_length=1, max_length=120)
    where: CandidateFilter


class AnalyzeCandidates(Input):
    where: CandidateFilter = Field(default_factory=CandidateFilter)
    group_by: list[Dimension] = Field(default_factory=list, max_length=2)
    cohorts: list[Cohort] = Field(default_factory=list, max_length=8)
    qualifications: list[str] = Field(default_factory=list, max_length=100)
    qualification_offset: int = Field(default=0, ge=0)
    qualification_limit: int = Field(default=10, ge=1, le=25)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=10)
    member_limit: int = Field(default=5, ge=0, le=20)

    @model_validator(mode="after")
    def group_mode(self):
        if self.group_by and self.cohorts:
            raise ValueError("Choose group_by or named cohorts, not both.")
        labels = [c.label.casefold() for c in self.cohorts]
        if len(labels) != len(set(labels)):
            raise ValueError("Cohort labels must be unique.")
        dimensions = [(d.field, d.qualification) for d in self.group_by]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("Grouping dimensions must be unique.")
        return self


TOOL_SPECS = {
    "find_candidates": (FindCandidates, "read", "Find candidates", (
        "Find any applicant subset in THIS screening, with total match count and paginated IDs. "
        "where combines all (AND), any (OR), none (exclude) condition lists. Conditions filter resume "
        "literal text, name, source_files, stage, rank, coverage, recommendation, evaluation_status, "
        "or verdict/evidence/reasoning for an exact qualification. Empty where means all stages. "
        "Use contains for text, eq for exact values, in for lists, and gte/lte for numbers. "
        "Ranks remain the full screening's grid ranks; ties and duplicate names return all matches. "
        "Example: where={all:[{field:'verdict',qualification:'R2',op:'eq',value:'Partial'}]}. "
        "Text matching is literal, NOT semantic evidence of a qualification. Follow next_offset.")),
    "read_candidate": (ReadCandidate, "read", "Read candidate details", (
        "Inspect exact IDs from find_candidates: supply candidate_id or candidate_ids (up to five "
        "for efficient side-by-side reads). section=verdicts returns qualification "
        "text, stored verdict, quoted evidence, written reasoning and evaluation provider/model. "
        "qualifications accepts labels/IDs, required, preferred; [] means all. offset/limit page verdicts. "
        "section=resume reads original extracted resume/cover-letter text; text_offset/text_limit page it. "
        "Optional terms perform literal OR search across the ENTIRE resume and return passages with "
        "character offsets, paged with offset/limit. No match does not prove lack of experience. "
        "section=evidence or reasoning with one qualification pages long stored text; summary pages the "
        "model summary. Quoted passages are source text; stored reasoning is the model's assessment.")),
    "analyze_candidates": (AnalyzeCandidates, "read", "Analyze candidate groups", (
        "Deterministic counts, rates, coverage distributions and qualification verdict breakdowns "
        "over ALL candidates matching where (same filter syntax as find_candidates). "
        "Use group_by for one/two field dimensions, including a qualification's verdict. Or use "
        "cohorts=[{label,where},...] for custom overlapping groups; single-ID cohorts compare people. "
        "Returns overlap counts, missing evaluations/verdicts, paginated group members and qualification "
        "breakdowns. Counts are never computed from a returned page. Semantic groups can use exact IDs "
        "after reading sources. Labels are user-defined, not additional model verdicts. "
        "[] qualifications means all; required/preferred tokens select kinds. No screening data changes.")),
}


def page_meta(total: int, offset: int, returned: int) -> dict:
    return {"total": total, "offset": offset, "returned": returned,
            "next_offset": offset + returned if offset + returned < total else None}


class CandidateData:
    """Cached per turn so filtering, document reads and group counts share data."""
    def __init__(self, screening_id: str):
        self.screening_id = screening_id
        self.qualifications = qual_labels(load_qualifications(screening_id))
        self.rows = load_rows(screening_id)
        docs = {r["id"]: r for r in db.query(
            "SELECT c.id,c.resume_text,e.created_at AS evaluated_at FROM candidate c "
            "LEFT JOIN evaluation e ON e.candidate_id=c.id WHERE c.screening_id=?", (screening_id,))}
        for row in self.rows:
            row["resume"] = docs[row["id"]]["resume_text"]
            row["evaluated_at"] = docs[row["id"]]["evaluated_at"]
            row["evaluation_status"] = "Error" if row.get("error") else "Evaluated" if row["evaluated"] else "Not evaluated"
            row["recommendation"] = ("Pass" if row["ai_pass"] else "Fail") if row["evaluation_status"] == "Evaluated" else row["evaluation_status"]
            for kind in ("required", "preferred"):
                row[f"{kind}_unmet"] = sum(self.verdict(row, q)["verdict"] != "Meets" for q in self.qualifications if q["kind"] == kind)
        self.by_id = {r["id"]: r for r in self.rows}
        self.snapshot_id = hashlib.sha256(json.dumps([self.qualifications, self.rows], sort_keys=True).encode()).hexdigest()[:16]

    def qualification(self, token: str) -> dict:
        matches = [q for q in self.qualifications if token.casefold() in (q["label"].casefold(), q["id"].casefold(), q["text"].casefold())]
        if len(matches) != 1:
            raise ValueError(f"Qualification {token!r} is unknown or ambiguous. Use an exact label or ID.")
        return matches[0]

    def select_qualifications(self, tokens: list[str]) -> list[dict]:
        if not tokens:
            return self.qualifications
        ids = set()
        for token in tokens:
            if token in ("required", "preferred"):
                ids.update(q["id"] for q in self.qualifications if q["kind"] == token)
            else:
                ids.add(self.qualification(token)["id"])
        return [q for q in self.qualifications if q["id"] in ids]

    @staticmethod
    def verdict(row: dict, qual: dict) -> dict:
        stored = row["verdicts"].get(qual["id"], {})
        return {"verdict": stored.get("verdict") if stored.get("verdict") in VERDICTS else "Missing",
                "evidence": stored.get("evidence") or "", "reasoning": stored.get("reasoning") or ""}

    def value(self, row: dict, field: str, qualification: str | None = None):
        if field in VERDICT_FIELDS:
            return self.verdict(row, self.qualification(qualification))[field]
        if field == "source_files":
            return "\n".join(row["source_files"])
        return row.get(field) if field in NUMBER_FIELDS else row.get(field) or ""

    def compile_filter(self, where: CandidateFilter):
        # Validate all references before scanning, including empty datasets and OR branches.
        compiled = {}
        for join in ("all", "any", "none"):
            conditions = getattr(where, join)
            for condition in conditions:
                if condition.qualification:
                    self.qualification(condition.qualification)
                if condition.field == "id":
                    values = condition.value if isinstance(condition.value, list) else [condition.value]
                    if condition.op in ("eq", "ne", "in", "not_in") and any(v not in self.by_id for v in values):
                        raise ValueError("Candidate ID is unknown in this screening; resolve IDs with find_candidates.")
            compiled[join] = conditions

        def matches(row):
            def check(c):
                value = self.value(row, c.field, c.qualification)
                actual = value.casefold() if isinstance(value, str) else value
                expected = c.value.casefold() if isinstance(c.value, str) else c.value
                if c.op == "is_empty": return not value
                if c.op in ("in", "not_in"):
                    found = actual in [v.casefold() if isinstance(v, str) else v for v in c.value]
                    return found if c.op == "in" else not found
                if c.op == "eq": return actual == expected
                if c.op == "ne": return actual != expected
                if c.op == "contains": return expected in actual
                if c.op == "not_contains": return expected not in actual
                if c.op == "gt": return actual > expected
                if c.op == "gte": return actual >= expected
                if c.op == "lt": return actual < expected
                return actual <= expected
            return (all(check(c) for c in compiled["all"])
                    and (not compiled["any"] or any(check(c) for c in compiled["any"]))
                    and not any(check(c) for c in compiled["none"]))
        return matches

    def summary(self, row: dict) -> dict:
        return {k: row[k] for k in ("id", "name", "stage", "rank", "required_met", "required_total",
                "preferred_met", "preferred_total", "recommendation", "evaluation_status", "source_files", "provider", "model", "evaluated_at")}

    def find(self, args: FindCandidates) -> dict:
        rows = list(filter(self.compile_filter(args.where), self.rows))
        rows.sort(key=lambda r: (r["name"].casefold(), r["id"]))
        rows.sort(key=lambda r: r[args.sort_by].casefold() if isinstance(r[args.sort_by], str) else r[args.sort_by], reverse=args.descending)
        selected = rows[args.offset:args.offset + args.limit]
        return {"matched": len(rows), **page_meta(len(rows), args.offset, len(selected)),
                "where": args.where.model_dump(), "candidates": [self.summary(r) for r in selected],
                "note": "Full-screening grid ranks are preserved. Text filters match literals, not semantic qualifications."}

    def read(self, args: ReadCandidate) -> dict:
        if args.candidate_ids:
            if any(cid not in self.by_id for cid in args.candidate_ids):
                raise ValueError("Candidate ID is unknown in this screening; use find_candidates first.")
            return {"section": args.section, "candidates": [
                self.read(ReadCandidate.model_validate({**args.model_dump(), "candidate_id": cid, "candidate_ids": []}))
                for cid in args.candidate_ids], "note": "Each candidate has independent pagination offsets."}
        row = self.by_id.get(args.candidate_id)
        if row is None:
            raise ValueError("Candidate ID is unknown in this screening; use find_candidates first.")
        quals = self.select_qualifications(args.qualifications)
        result = {"candidate": self.summary(row), "section": args.section}
        if args.section == "verdicts":
            selected = quals[args.offset:args.offset + args.limit]
            entries = []
            for q in selected:
                values = self.verdict(row, q)
                lengths = {f: len(values[f]) for f in ("evidence", "reasoning")}
                entries.append({"qualification": q, "verdict": values["verdict"],
                                **{f: values[f][:1200] for f in lengths},
                                "text_lengths": lengths, "truncated_fields": [f for f, n in lengths.items() if n > 1200]})
            return {**result, **page_meta(len(quals), args.offset, len(entries)), "verdicts": entries,
                    "note": "Stored model assessments, not new verdicts. Page long evidence/reasoning using that section and qualification."}
        if args.section in ("evidence", "reasoning"):
            qual = self.qualification(args.qualifications[0])
            text = self.verdict(row, qual)[args.section]
            result["qualification"] = qual
        else:
            text = row.get(args.section) or ""
        if args.section == "resume" and args.terms:
            hits = sorted({(m.start(), m.end(), term) for term in args.terms
                           for m in re.finditer(re.escape(term), text, re.IGNORECASE)})
            selected = hits[args.offset:args.offset + args.limit]
            passages = [{"term": term, "match_start": start, "match_end": end,
                         "start": max(0, start - 160), "end": min(len(text), end + 160),
                         "quote": text[max(0, start - 160):min(len(text), end + 160)]} for start, end, term in selected]
            result.update({**page_meta(len(hits), args.offset, len(selected)), "passages": passages, "text_length": len(text),
                           "note": "Literal matches across all extracted text. No hit does not prove absence of equivalent experience."})
        else:
            chunk = text[args.text_offset:args.text_offset + args.text_limit]
            result.update({"text": chunk, "text_length": len(text), "text_offset": args.text_offset,
                           "next_text_offset": args.text_offset + len(chunk) if args.text_offset + len(chunk) < len(text) else None})
        if args.section == "resume":
            result["source_note"] = "Verbatim extracted text. Source files may combine a resume and cover letter; page/file boundaries were not retained. Offsets are characters in this combined text."
        return result

    def stats(self, rows: list[dict], quals: list[dict]) -> dict:
        total = len(rows)
        evaluated = [r for r in rows if r["evaluation_status"] == "Evaluated"]
        states = Counter(r["evaluation_status"] for r in rows)
        verdicts = []
        for q in quals:
            counts = Counter(self.verdict(r, q)["verdict"] for r in rows)
            verdicts.append({"qualification": q, "counts": {v: counts[v] for v in VERDICTS},
                             "meets_rate": counts["Meets"] / total if total else None, "denominator": total})
        return {"count": total, "evaluation_status": {s: states[s] for s in STATUSES},
                "recommendations": dict(Counter(r["recommendation"] for r in rows)),
                "coverage_denominator": len(evaluated),
                "pass_rate_evaluated": sum(r["recommendation"] == "Pass" for r in evaluated) / len(evaluated) if evaluated else None,
                "mean_required_met": sum(r["required_met"] for r in evaluated) / len(evaluated) if evaluated else None,
                "mean_preferred_met": sum(r["preferred_met"] for r in evaluated) / len(evaluated) if evaluated else None,
                "required_met_distribution": dict(sorted(Counter(r["required_met"] for r in evaluated).items())),
                "preferred_met_distribution": dict(sorted(Counter(r["preferred_met"] for r in evaluated).items())),
                "qualifications": verdicts}

    def analyze(self, args: AnalyzeCandidates) -> dict:
        rows = list(filter(self.compile_filter(args.where), self.rows))
        quals = self.select_qualifications(args.qualifications)
        selected_quals = quals[args.qualification_offset:args.qualification_offset + args.qualification_limit]
        groups = []
        if args.cohorts:
            for cohort in args.cohorts:
                predicate = self.compile_filter(cohort.where)
                groups.append((cohort.label, {}, [r for r in rows if predicate(r)]))
        elif args.group_by:
            for dimension in args.group_by:
                if dimension.qualification:
                    self.qualification(dimension.qualification)
            buckets = {}
            for row in rows:
                key = tuple(self.value(row, d.field, d.qualification) for d in args.group_by)
                buckets.setdefault(key, []).append(row)
            for key, members in sorted(buckets.items(), key=lambda item: tuple(str(v) for v in item[0])):
                dimensions = {f"{d.field}:{d.qualification}" if d.qualification else d.field: v for d, v in zip(args.group_by, key)}
                groups.append((" / ".join(map(str, key)), dimensions, members))
        else:
            groups = [("All matching candidates", {}, rows)]
        memberships = Counter(r["id"] for _, _, members in groups for r in members)
        # Only named cohorts can overlap. Report every overlap, independent of group pagination.
        overlaps = [{"a": a[0], "b": b[0], "count": len({r["id"] for r in a[2]} & {r["id"] for r in b[2]})}
                    for a, b in combinations(groups, 2)] if args.cohorts else []
        comparisons = []
        if args.cohorts:
            summaries = [(label, self.stats(members, [])) for label, _, members in groups]
            for (a, first), (b, second) in combinations(summaries, 2):
                comparisons.append({"a": a, "b": b, "direction": "a minus b",
                                    "differences": {key: first[key] - second[key] if first[key] is not None and second[key] is not None else None
                                                    for key in ("mean_required_met", "mean_preferred_met", "pass_rate_evaluated")}})
        selected = groups[args.offset:args.offset + args.limit]
        return {"matched": len(rows), "where": args.where.model_dump(),
                "overall": self.stats(rows, selected_quals),
                "qualification_page": page_meta(len(quals), args.qualification_offset, len(selected_quals)),
                "group_page": page_meta(len(groups), args.offset, len(selected)),
                "groups": [{"label": label, "dimensions": dims, **self.stats(members, selected_quals),
                            "members": [self.summary(r) for r in members[:args.member_limit]],
                            "members_returned": min(len(members), args.member_limit),
                            "members_truncated": len(members) > args.member_limit}
                           for label, dims, members in selected],
                "union_count": len(memberships), "overlap_count": sum(n > 1 for n in memberships.values()),
                "unassigned_count": len(rows) - len(memberships), "pairwise_overlaps": overlaps,
                "comparisons": comparisons,
                "note": "Counts cover all matches, never just a page. Coverage averages exclude errors and unevaluated candidates; verdict rates include all group members. Empty denominators yield null. Cohort labels are custom definitions, not recorded verdicts."}

    def run(self, tool: str, args: dict) -> dict:
        model = TOOL_SPECS[tool][0].model_validate(args)
        handlers = {"find_candidates": self.find, "read_candidate": self.read, "analyze_candidates": self.analyze}
        return {"screening_id": self.screening_id, "snapshot_id": self.snapshot_id, **handlers[tool](model)}
