# Applicant research tools

Three tools cover applicant lookup, source reading, and group analysis. They replace
`query_candidates`, `explain_rank`, and `stage_stats` in the agent's advertised tool list.
The legacy scripts remain testable but are not callable by the model. The deterministic
`whatif` tool remains available for changing hypothetical qualification requirements.

| Tool | Answers questions such as | Capabilities |
| --- | --- | --- |
| `find_candidates` | “Who got Partial on R2 and mentions Python?” | Combine AND, OR and exclusions across IDs, names, ranks, stages, coverage, resume text, source filenames, summaries, verdicts, evidence, written reasoning, evaluation status and model |
| `read_candidate` | “What did their resume actually say, and why did the model mark this Partial?” | Read one candidate or batch up to five exact IDs; qualification verdicts, evidence, reasoning, summaries, full extracted text or literal-search passages; every source identifies candidate and files |
| `analyze_candidates` | “Compare people who meet every required qualification with people who meet the preferred ones.” | Filter the whole pool, group by one or two dimensions, or define up to eight custom cohorts; counts, distributions, rates, per-qualification verdicts, coverage comparisons and overlaps |

These tools are read-only and always scoped to the current screening. They cannot change
verdicts, query other tables, or execute SQL/code. All numerical aggregation and comparison
is implemented in Python. Actual grid ranks are retained when selecting a smaller group.

## Combined filters

The same `where` object works for finding candidates, the analysis population, and each cohort.
All conditions in `all` must match; at least one in a nonempty `any` must match; none of the
conditions in `none` may match. Empty filters mean the entire screening, across all stages.

```json
{
  "where": {
    "all": [
      {"field": "stage", "value": "1"},
      {"field": "verdict", "qualification": "R2", "value": "Partial"}
    ],
    "any": [
      {"field": "resume", "op": "contains", "value": "Python"},
      {"field": "resume", "op": "contains", "value": "Java"}
    ],
    "none": [
      {"field": "evaluation_status", "value": "Error"}
    ]
  },
  "limit": 20
}
```

Text comparisons ignore case. `contains` is a literal substring match, not a semantic
evaluation. `eq`/`ne`, `in`/`not_in`, numeric `gt`/`gte`/`lt`/`lte`, and text `is_empty`
are also supported. A verdict/evidence/reasoning condition requires one exact qualification
label or ID. Unknown fields, operators, labels and IDs produce errors, including when another
condition already yields no matches. Invalid filters never silently broaden the query.

Numeric fields include required/preferred met and unmet counts. `recommendation` separates
Pass, Fail, Error and Not evaluated. `verdict` separates Meets, Partial, No and Missing.

## Source and verdict reading

After resolving exact IDs with `find_candidates`, call `read_candidate` with `candidate_id`
or `candidate_ids`. `section: "verdicts"` returns stored verdict, evidence and written
reasoning with provider, model and evaluation time. `qualifications: ["required"]`,
`["preferred"]`, or explicit labels/IDs select the breakdown; an empty list selects all.

`section: "resume"` returns verbatim extracted text, paged with `text_offset` and `text_limit`.
`terms: ["Python", "full-time"]` instead searches the entire text for literal occurrences
and returns surrounding quotes with character offsets. The `offset`/`limit` pair pages hits.
Terms can match beyond the initial text page. Regular-expression syntax has no special meaning.

Resume and cover-letter text may have been combined at upload. The tool identifies the stored
source filenames and explicitly reports that file/page boundaries were not retained. It never
invents a page reference. Long evidence/reasoning previews identify truncated fields and full
lengths; retrieve their remainder with `section: "evidence"` or `"reasoning"`, one qualification,
and `text_offset`. `section: "summary"` retrieves the stored model summary.

Batch results keep each candidate's sources, verdicts and pagination independent. Missing
reasoning stays empty. The agent must distinguish source facts, recorded model assessments,
and its own interpretation; these tools do not perform a new evaluation.

## Groups and comparisons

For a stage-by-recommendation breakdown:

```json
{
  "group_by": [{"field": "stage"}, {"field": "recommendation"}],
  "qualifications": ["required"],
  "member_limit": 3
}
```

For custom, potentially overlapping groups:

```json
{
  "cohorts": [
    {"label": "All required met", "where": {"all": [
      {"field": "required_unmet", "value": 0},
      {"field": "evaluation_status", "value": "Evaluated"}
    ]}},
    {"label": "At least two preferred met", "where": {"all": [
      {"field": "preferred_met", "op": "gte", "value": 2}
    ]}}
  ],
  "qualifications": ["preferred"]
}
```

Single-ID cohorts compare individual applicants using the same engine. For interpretive
groups such as “demonstrates project ownership,” first read the sources, then supply explicit
candidate IDs and explain the grouping criteria. Those labels are custom definitions, not
new stored model verdicts. An absent search term does not establish absent experience.

Every count uses the complete filtered population, regardless of returned member, group or
qualification pages. The response reports union, overlap, unassigned and pairwise overlap
counts, so overlapping cohorts cannot be added as if disjoint. Named cohort comparisons include
code-computed differences in mean coverage and evaluated pass rates, with direction stated.

Coverage averages and evaluated pass rates exclude failed and absent evaluations. Qualification
verdict rates use all group members, with Missing kept in the denominator. Empty denominators
return `null`, not a fabricated zero-percent result. Every response states these rules.

Follow `next_offset`, `next_text_offset`, `group_page` and `qualification_page` to read remaining
pages. Research calls share a cached screening snapshot within a turn; `snapshot_id` identifies
the data used. A new turn reloads current data. Counts and findings are never persisted as
candidate decisions.
