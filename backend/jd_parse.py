"""Position description → itemized required/preferred qualifications.

The screening's whole shape comes from this step: every downstream verdict,
rollup and stage gate is per-qualification, so a qualification has to be one
atomic thing a reader can answer Meets/Partial/No about. Compound bullets
("Bachelor's degree in Computer Science and 3 years of Java") get split.
"""

from __future__ import annotations

import logging
from typing import Any

from .llm.base import LLMError, LLMProvider

logger = logging.getLogger(__name__)

QUALIFICATIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_title": {
            "type": "string",
            "description": "The position title as stated in the document.",
        },
        "required": {
            "type": "array",
            "description": "Required/minimum qualifications, one atomic item each.",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
        "preferred": {
            "type": "array",
            "description": "Preferred/desired qualifications, one atomic item each.",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
    },
}

PARSE_PROMPT = """\
You are parsing a position description into a checklist a resume screener will \
use to judge candidates one item at a time.

Split the document's qualifications into two lists:
- "required": the minimum qualifications a candidate must have to be considered \
(often labeled Required Qualifications, Minimum Qualifications, Basic \
Qualifications, or stated with "must").
- "preferred": qualifications that strengthen a candidate but are not \
mandatory (often labeled Preferred, Desired, Nice to Have, or stated with \
"preferred").

Rules for every item:
- One atomic, verdict-able requirement per item. A reader with a resume in hand \
must be able to answer Meets / Partial / No about it without splitting it further.

- SPLIT a statement that demands two independent things at once. "Bachelor's \
degree in Computer Science and 3 years of Java development" becomes two items, \
because a candidate can satisfy one and fail the other.

- DO NOT SPLIT a list of alternatives. When the document says "or", "one of", \
"such as", "including", or gives examples in parentheses, those are ways to \
satisfy a SINGLE requirement — keep them together in one item, with the \
alternatives intact.
    - "Proficiency in Python, Java, or C#" is ONE item. It is not three.
    - "Experience with CI/CD pipelines (Azure DevOps, GitHub Actions, GitLab \
CI, or Jenkins)" is ONE item. The tools are examples, not four requirements.
    - "A relational database — PostgreSQL, MySQL, or SQL Server — including \
schema design and query tuning" is ONE item. Schema design and query tuning \
describe how the database is used; they are not separate qualifications.
    - "Degree in Computer Science, Software Engineering, or a related field" is \
ONE item.
  Splitting these turns one requirement into several a candidate must satisfy \
all of, which changes what the position asks for.

- Never emit a bare technology name, tool name or fragment as its own item. If \
you have written an item that is just "GitHub Actions", "unit tests" or \
"branching", it belongs inside the requirement it came from.
- Keep the document's own wording and any measurable threshold (years, degree \
level, certification name, specific technology).
- Do not invent qualifications that are not in the document.
- Do not include duties, salary, benefits, EEO boilerplate, application \
instructions, or company background. Include a responsibility only when it is \
written as something the candidate must already be able to do.
- If the document states no preferred qualifications, return an empty preferred list.

Also return the position title exactly as written; use an empty string if absent.

Position description:
"""


def parse_qualifications(
    provider: LLMProvider, jd_text: str, *, model: str | None = None
) -> dict[str, Any]:
    """Return ``{"job_title": str, "required": [...], "preferred": [...]}``.

    Items are normalized to non-empty, de-duplicated strings so the editable
    checklists never open with blanks or exact repeats.
    """
    text = (jd_text or "").strip()
    if not text:
        raise LLMError("the position description contained no extractable text")

    result = provider.structured_extract(
        PARSE_PROMPT + text,
        QUALIFICATIONS_SCHEMA,
        model=model,
        schema_name="ParsedQualifications",
    )

    return {
        "job_title": str(result.get("job_title") or "").strip(),
        "required": _clean_items(result.get("required")),
        "preferred": _clean_items(result.get("preferred")),
    }


def _clean_items(items: Any) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip().lstrip("-•* ").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned
