# Requirements — Staged Resume Screening Rebuild

Single source of truth for what is being built. The build itself is sequenced in `implementation-plan.md`. Everything here is settled unless listed under Open Items in the plan.

## Summary

Rebuild Resume_Rec_2 as a **FastAPI + TypeScript staged resume-screening app**, modeled on the sibling `ResumeAI` repo's architecture. The rebuild happens on a new branch and **deletes the entire existing Streamlit codebase in its first commit** — no legacy folder, no ported code. (That codebase has since been removed from the repository's history as well.) The only code in the repo is code these requirements demand.

## Functional requirements

### 1. Job description intake
- The user drags and drops a **position-description PDF** into a dropzone.
- The app extracts the text and LLM-parses it into **itemized required and preferred qualifications** (one atomic, verdict-able item each — the ResumeAI `/api/parse-jd` pattern, extended to PDF input).
- Parsed qualifications render as **two editable checklists** (required / preferred): add, remove, reword, reorder, move between lists. Evaluation cannot start until the user confirms them.

### 2. Evaluation — required first, preferred second
- Every candidate gets a **per-qualification verdict**: Meets / Partial / No, with evidence quoted from the resume.
- Rollups (required met N/M, preferred met N/M) and ranking are computed **in code, never by the model**.
- Ranking: required coverage first, preferred coverage as tiebreak. Candidates who meet more qualifications rank higher, reducing the pool.

### 3. Staging — human-gated funnel
- Stage 1 (minimum requirements) → Stage 2 (preferred qualifications) → Stage 3 (interview candidates).
- Stage-1 pass recommendation = all required quals Meets (Partial does not pass).
- **Nothing advances automatically.** Each stage has promote/reject controls; the human can override AI verdicts in either direction, and overrides are recorded with an audit trail.

### 4. Stage grids (replaces the old straight-to-Excel flow)
- Each stage shows its candidates in an **Excel-like grid** (AG Grid): fixed, bold, background-colored header row; per-column sort and filter.
- Columns: candidate, AI recommendation, required N/M, preferred N/M, one color-coded column per qualification (evidence on hover/click), stage actions.
- Each stage grid keeps a **styled Excel export button** (plus CSV) — export is secondary, not the primary flow.

### 5. Per-stage app agent — tool use only
- All providers/models use the backend's allowlisted tool dispatcher; CLI harness settings cannot enable command execution.
- Every model response contains tool calls. User-facing explanations use `reply`; no prose-only fallback.
- Analyses and what-if calculations reuse deterministic ranking and rollup code.
- Three flexible research tools support compound candidate filters; extracted resume/cover-letter
  text, evidence and recorded reasoning; individual comparisons and custom/grouped cohorts.
  Counts cover full subsets regardless of pagination, overlapping groups are explicit, and missing
  evaluations/verdicts are distinguished from failures. Source text and model assessments remain distinct.
- Expose human-facing controls as tools: screening/stage navigation, sorting/filtering/selection, evidence, panels, exports, file selection, checklist editing/confirmation, evaluations, candidate moves, screening creation/deletion, model selection, and bias audit operations.
- Persistent review cards gate data changes. A reviewer applies or cancels; the agent has no approval tool. Revalidate current data and reject stale or duplicate approvals.
- Candidate moves retain the human decision/override audit trail. Validate the full selection atomically against the source stage.
- App actions have stable IDs and apply in tool-call order, including when chat is popped out. Expose the actual grid selection to chat.
- Local files require a user file chooser. Provider credentials remain on the Configuration page and are never exposed to the agent.
- See `agent-tools.md` for the implemented capability catalog and deliberate boundaries.

### 6. Bias audit — built-in, re-runnable
- The `test-resumes/SWE_pdf/` corpus contains baseline resumes plus duplicate variants with injected protected-class information (filename codes like `(BG1_G1)`, `(BS2_R2)`, `(BE1_RA1)`).
  **Decoded from the PDFs and confirmed 2026-07-28:** the first code names the baseline (BG1–BG4 mid-level, BS1–BS2 senior/staff, BE1–BE2 entry level); the second names the injected attribute — `G` = **gender**, `R` = **religion**, `RA` = **race / ethnicity & national origin**. Nothing in the corpus encodes age, so the earlier "race+age" reading was wrong. Two variants (`BG4_G4`, `BE2_G2`) inject nothing and serve as **negative controls**, reported separately as the noise floor. Each variant also carries a construction marker on its name line (`Ayaan Rahman ◆ BG1/R1`) which is stripped before evaluation, so the injected sentence is the only difference that reaches the model.
- Audit mode pairs variants with baselines, runs the full corpus through the pipeline with the active model, and reports deltas per protected attribute: verdict flips, coverage deltas, stage-outcome flips, rank displacement.
- Pass/fail against a configurable "negligible bias" threshold. Persisted per run so results are comparable across models.

### 7. Model configuration — switch per run
- A configuration page selects the active LLM provider and model:
  - **SIS fastLLM gateway** — SIS is a UT System department hosting private AI infrastructure (Mistral and other open-weight models). Connection details pending; assumed OpenAI-compatible.
  - **Azure OpenAI via the UL AI proxy** — Bearer-authed (`ULMAIPROXY_AUTH_TOKEN`); OpenAI-compatible under `{root}/v1/*` (`GET /v1/models` for listing), Anthropic-compatible at `POST /v1/messages`, health at `GET /health`. Reference client: `agent-fight/app/proxy.py`.
- Every evaluation, chat session, and audit run is **tagged with the provider+model used**. Comparing models = running the same screening under different configs. No side-by-side diff view.

## Explicitly out of scope

Deleted with the Streamlit app; rebuild fresh only if ever requested — no code seams reserved:

- Anonymization (redact-before-LLM) — first candidate to revisit if the bias audit finds non-negligible deltas
- Cover-letter AI-detection
- Custom evaluation fields beyond the parsed qualifications
