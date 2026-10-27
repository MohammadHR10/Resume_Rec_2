# Backlog — features deleted in the rebuild

These existed in the Streamlit app and were deliberately dropped. **No code seams
are reserved for them.** If one is ever wanted again it gets built fresh against
the current architecture; the old implementation is on `main`'s git history and
is not a starting point.

## Anonymization (redact-before-LLM)

The old app could redact names, contact details and schools from resume text
before sending it to the model, and exported a mapping report linking
placeholders back to originals.

This is the **first thing to revisit if the bias audit finds non-negligible
deltas** — it attacks the same problem from the other end. A rebuild would sit
between `extraction.py` and `pipeline.evaluate_resume`, and would need its own
audit run to prove it actually reduced the deltas rather than just hiding the
signal from the reviewer too.

## Cover-letter AI detection

A second tab that classified a cover letter as AI-generated or human-written,
with its own confidence score and Excel report.

Out of scope for a qualification-driven screening funnel: the verdict is about
the document's provenance, not about whether the candidate meets a requirement,
so it has nowhere to live in a per-qualification grid. If it returns it should
probably be a separate tool rather than a tab in this one.

## Custom evaluation fields

The old app let a user define arbitrary extra fields (string or boolean) with
their own scoring criteria, and built a Pydantic model per run to hold them.

The parsed qualification checklist replaces this: anything a reviewer wants the
model to judge is added as a qualification, which then gets a verdict, a rollup
and a grid column for free. A separate custom-field mechanism would be a second
way to say the same thing, with none of the ranking or staging behaviour.

## Deferred, not deleted

- **Per-qualification evaluation calls.** Evaluation is one call per candidate
  covering the whole checklist. If verdict quality proves weak on long
  checklists, splitting into one call per qualification is the next lever —
  `pipeline.evaluate_resume` is the only place that would change.
- **Long-lived harness processes.** The chat adapters run one CLI process per
  turn, resumed by id. A resident stream-json process (the hive-edge-minds
  `mind_server` pattern) would cut per-turn startup cost if chat latency ever
  matters more than the lifecycle simplicity.
