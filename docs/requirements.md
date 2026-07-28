# Requirements — Staged Resume Screening Rebuild

Single source of truth for what is being built. The build itself is sequenced in `implementation-plan.md`. Everything here is settled unless listed under Open Items in the plan.

## Summary

Rebuild Resume_Rec_2 as a **FastAPI + TypeScript staged resume-screening app**, modeled on the sibling `ResumeAI` repo's architecture. The rebuild happens on a new branch and **deletes the entire existing Streamlit codebase in its first commit** — no legacy folder, no ported code; git history on `main` is the archive. The only code in the repo is code these requirements demand.

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

### 5. Per-stage chat — agentic harness first, structured fallback
- One chat panel per stage. It must handle **open-ended analytical questions**, not just predictable commands: "why is A ranked higher than B?", "why did so many make it past this stage?", "if we remove qual X, what happens to the list?"
- **Mode A (harness):** capable models drive a full CLI harness (Claude CLI / Codex CLI) with a scoped workspace containing a read-only screening snapshot and stateless analysis tools — per the `hive-edge-minds` repo's `mind_templates/` adapter pattern.
- **Mode B (structured fallback):** small open-weight models (SIS) get a mini agentic loop in our code — strict-JSON `{tool_calls, answer}` turns where the backend executes the **same tools** on the model's behalf.
- What-if analysis is a **deterministic recompute** (ranking math is code), never an LLM guess.
- Chat can also drive the grid: sort/filter/clear actions flow to the frontend regardless of mode.
- Per-model `chat_mode` config; a failed harness turn degrades to structured for that turn. If SIS models prove they can drive the harness, switching them is a config flip — the tools are shared.

### 6. Bias audit — built-in, re-runnable
- The `test-resumes/SWE_pdf/` corpus contains baseline resumes plus duplicate variants with injected protected-class information (filename codes like `(BG1_G1)`, `(BS2_R2)`, `(BE1_RA1)` — gender / race / race+age variants; exact semantics to be confirmed from the PDFs before hard-coding).
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
