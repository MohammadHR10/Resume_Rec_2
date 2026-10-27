# Implementation Plan — Staged Resume Screening Rebuild

Implements `requirements.md` (the single source of truth for scope and decisions — read it first).
Reference architecture: the sibling `ResumeAI` repo (FastAPI + vanilla TypeScript/Vite/Bootstrap, dropzone upload, LLM JD parsing, dynamic Pydantic schemas, async jobs with polling progress).

## Target repo layout

```
Resume_Rec_2/
├── backend/
│   ├── server.py            # FastAPI app, API routes, serves dist/
│   ├── db.py                # SQLite persistence (screenings, candidates, evaluations, stages, config)
│   ├── llm/
│   │   ├── base.py          # Provider interface: structured_extract(text, schema), chat(messages, schema)
│   │   ├── fastllm.py       # SIS fastLLM gateway client (stubbed until connection details arrive)
│   │   ├── ulproxy.py       # UL AI proxy client (pattern: agent-fight/app/proxy.py)
│   │   └── registry.py      # Active provider/model resolution from stored config
│   ├── jd_parse.py          # PDF JD → itemized required/preferred qualifications
│   ├── pipeline.py          # Per-candidate evaluation: qual verdicts, rollups, batching
│   ├── extraction.py        # PDF text extraction (PyMuPDF, lifted from ResumeAI)
│   ├── chat/
│   │   ├── session.py       # Chat session lifecycle, action queue, mode selection + fallback
│   │   ├── adapters/        # Harness adapters (hive-edge-minds mind_templates pattern)
│   │   │   ├── claude_cli.py    # Long-lived stream-json claude process per session
│   │   │   └── codex_cli.py     # Per-turn codex exec --json, thread-id resume
│   │   ├── structured.py    # Mode B: mini agentic loop over llm.chat() for small models
│   │   ├── workspace.py     # Per-session scratch dir: screening snapshot + tools + CHAT.md brief
│   │   └── tools/           # Stateless analysis tools (argparse + JSON stdout), shared by both modes
│   ├── audit.py             # Bias audit: filename-code pairing, delta computation
│   └── export.py            # Styled Excel export per stage (openpyxl)
├── frontend/                # Vite + TypeScript + Bootstrap CSS (ResumeAI stack) + AG Grid Community
│   └── src/
│       ├── api.ts, types.ts, main.ts
│       ├── configPage.ts    # Provider/model switcher
│       ├── jdIntake.ts      # JD dropzone + editable qual checklists
│       ├── uploadForm.ts    # Resume dropzone (PDF/ZIP) — adapted from ResumeAI
│       ├── progress.ts      # Job progress polling — adapted from ResumeAI
│       ├── stageGrid.ts     # AG Grid wrapper: verdict columns, promote/reject, export
│       ├── chatPanel.ts     # Per-stage chat with grid actions
│       └── auditPage.ts     # Bias audit runner + report grid
├── test-resumes/SWE_pdf/    # Bias audit corpus (already present)
├── tests/                   # pytest: pairing logic, rollups, ranking, JD parse schema, providers (mocked)
└── docs/
```

**Clean slate:** the rebuild happens on a new branch, and Phase 0 **deletes** the entire Streamlit-era codebase — no `legacy/` folder, no ported code. Git history on `main` is the archive; anything worth resurrecting later gets rebuilt against the new architecture, not copied. The only code in the repo is code the requirements list demands. `CLAUDE.md` is rewritten at the end.

## Core data model (SQLite)

- **screening** — one hiring exercise: job title, JD text, created_at, provider+model used, status
- **qualification** — belongs to screening; text, kind (`required`|`preferred`), position (user-editable list)
- **candidate** — belongs to screening; name, source filename(s), extracted text
- **evaluation** — one candidate × one run: per-qual verdicts JSON, rollups (required_met, required_total, preferred_met, preferred_total), overall notes, provider+model tag, timestamp
- **stage_state** — candidate's current stage (1|2|3|rejected) + audit trail of human promote/reject actions (who/when/optional note)
- **config** — active provider, model, per-provider connection details, per-model `chat_mode` (`harness` | `structured`)
  - *Revised 2026-07-28:* the original plan kept every secret in `.env` and stored only non-secret selections. At the user's request the configuration page now accepts a gateway URL and bearer token directly, so the `connections` config key may hold a token. Consequences handled in code: the token is write-only over the API (`GET /api/config` returns only `hasKey` and a masked hint), a stored value overrides the environment, and `chat/workspace.py` strips the `config` table from the database copy it hands to a chat harness — otherwise the credential would land in a directory an agent is explicitly allowed to read. The app still has no authentication of its own, so reaching the page means being able to use the credentials.
- **chat_session** — per screening+stage: transcript, mode + model tags, harness resume id (`harness_sid` pattern from hive-edge-minds)
- **audit_run** — bias audit executions: corpus used, pairings, deltas JSON, provider+model tag

Persistence is the biggest departure from ResumeAI (in-memory jobs): human-gated stages and per-run model tagging require state that survives restarts. Raw `sqlite3` with a thin `db.py` — no ORM needed at this scale.

## Phases

### Phase 0 — Clean slate + scaffold

1. Create the rebuild branch. **Delete** all Streamlit-era files: `app.py`, `main.py`, `mistral_client.py`, `pdf_extract.py`, `cover_letter_analyzer.py`, `test_api.py`, `enhanced_client.py`, `fix_indentation.py`, `nginx.conf`, the old `Dockerfile`, both docker-compose files, and the old `requirements.txt`. Recovery path is git history on `main` — nothing is quarantined or ported.
2. Scaffold `backend/` (FastAPI skeleton, `db.py` with schema init) and `frontend/` (Vite + TS + Bootstrap, conventions from `ResumeAI/frontend`), plus AG Grid Community dependency. Fresh `requirements.txt` containing only what the new code imports.
3. New Dockerfile (multi-stage: Node builds frontend → Python serves `dist/`), docker-compose, `.env.example` documenting `ULMAIPROXY_*` and `FASTLLM_*` vars.
4. `pytest` wiring with one smoke test (app boots, schema creates).

**Done when:** `docker compose up` serves an empty shell UI; `uvicorn backend.server:app` + `npm run dev` works locally.

### Phase 1 — LLM provider abstraction + config page

1. `llm/base.py`: interface with two operations — `structured_extract(text, json_schema)` (strict-JSON evaluation calls) and `chat(messages, json_schema)` (chat turns). Retry/backoff lifted from `ResumeAI/llm.py`.
2. `llm/ulproxy.py`: UL AI proxy client following `agent-fight/app/proxy.py` — Bearer auth (`ULMAIPROXY_AUTH_TOKEN`), OpenAI-compatible `{root}/v1/chat/completions` with `response_format` json_schema, model listing via `GET /v1/models`, health via `GET /health`.
3. `llm/fastllm.py`: SIS fastLLM gateway client. **Assume OpenAI-compatible until told otherwise** (mirror ulproxy with different env vars); isolate all assumptions here so corrections are one-file. Blocked details flagged in Open Items.
4. `llm/registry.py` + `/api/config` (GET/PUT) + `/api/config/models` (list models from active provider, live).
5. `configPage.ts`: provider dropdown, model dropdown (populated from the provider), connection test button (health/model-list ping). Every evaluation and audit run records provider+model.

**Done when:** switching provider/model in the UI changes which backend serves a test completion; both providers listed (fastLLM may show "not configured" until creds exist).

### Phase 2 — JD intake and editable qualification checklists

1. `POST /api/screenings` (create) and `POST /api/screenings/{id}/parse-jd` accepting a **PDF upload**: extract text (PyMuPDF), then LLM-parse with a strict schema `{required: [{text}], preferred: [{text}]}`. Prompt adapted from ResumeAI's `_JD_PARSE_PROMPT`, but targeting itemized qualifications rather than sections — one atomic, verdict-able qualification per item (split compound bullets).
2. Qualification CRUD: `GET/PUT /api/screenings/{id}/qualifications` — add, remove, reword, reorder, move between required/preferred.
3. `jdIntake.ts`: JD dropzone → parsed results render as two editable lists (inline edit, delete, add, drag or button to move between lists). Evaluation cannot start until the user confirms the lists.

**Done when:** dropping a real UT position-description PDF yields editable required/preferred lists that persist.

### Phase 3 — Evaluation pipeline with per-qual verdicts

1. Resume intake: dropzone for PDFs/ZIP (reuse ResumeAI's `uploadForm.ts` + server-side ZIP flattening + `_candidate_key` grouping).
2. Per-candidate evaluation call: one `structured_extract` per candidate with schema generated from the confirmed qualifications —
   `{candidate_name, verdicts: [{qual_id, verdict: "Meets"|"Partial"|"No", evidence}], summary}`.
   (Single call per candidate, not per qual — cheaper and lets the model see the whole resume once. Revisit if quality demands per-qual calls.)
3. Rollups computed **in code, not by the model**: required_met/total, preferred_met/total, stage-1 pass = all required quals `Meets` (Partial does not pass; human can override at the gate).
4. Ranking in code: required coverage desc, then preferred coverage desc, then name. No LLM-composed overall score.
5. Batching + progress: adapt ResumeAI's batch loop (BATCH_SIZE/BATCH_DELAY) and polling progress endpoint; results write to SQLite as they complete.
6. Every candidate starts in stage 1 with an AI-recommended pass/fail flag — nothing auto-advances.

**Done when:** uploading the 34 SWE_pdf resumes against a confirmed qual list produces persisted per-qual verdicts and correct rollups/ranking, tagged with the model used.

### Phase 4 — Stage grids, promotion controls, Excel export

1. `stageGrid.ts` on AG Grid Community: pinned styled header row (bold, background color), per-column sort and filter — this natively satisfies req 6. Columns: candidate, AI recommendation, required N/M, preferred N/M, then one column per qualification (verdict cell, color-coded Meets/Partial/No, evidence on hover/click), stage actions.
2. Three stage tabs (Stage 1: Minimum Requirements, Stage 2: Preferred Qualifications, Stage 3: Interview Candidates). Stage 2's grid emphasizes preferred-qual columns and coverage ranking; stage 3 is the confirmed shortlist.
3. Human gating: promote/reject buttons per row + bulk action on selection; writes `stage_state` with audit trail; user can promote a candidate the AI failed (override recorded).
4. `export.py`: styled Excel per stage grid (openpyxl: bold filled header, borders, wrapped text, sized columns — written fresh, nothing ported) via `GET /api/screenings/{id}/stages/{n}/export`. CSV variant included.

**Done when:** a full human-gated pass — evaluate → review stage 1 grid → promote → rank in stage 2 → promote → stage 3 shortlist → export any stage to Excel — works end to end.

### Phase 5 — Per-stage chat: agentic harness first, structured fallback

The chat must handle open-ended analytical questions — "why is A ranked higher than B?", "why did so many make it past this stage?", "if we remove qual X, what happens to the list?" — not just predictable grid commands. Architecture modeled on the `hive-edge-minds` repo's harness adapters (`mind_templates/`), with a fallback path for models that can't drive a harness (SIS's small open-weight models).

1. **Analysis tools — one implementation, both modes.** Stateless scripts in `backend/chat/tools/` (hive-edge-minds `tools/stateless` pattern: argparse in, JSON on stdout, editable without restart):
   - `explain_rank.py A B` — verdict-by-verdict comparison explaining the rank order between two candidates
   - `stage_stats.py --stage n` — aggregates (pass counts, per-qual failure rates, verdict distributions) for "why did so many pass?"
   - `whatif.py --remove-qual <id> ...` — recomputes rollups, gate outcomes, and ranking **without** the named quals and returns the diff (who moves, who newly passes/fails). Deterministic — ranking math lives in code (Phase 3), so what-if is a recompute, not an LLM guess.
   - `query_candidates.py` — filtered candidate/verdict/evidence dump from the screening snapshot
   - `grid_action.py --sort ... --filter ...` — posts a sort/filter/clear action to the backend's per-chat-session action queue, which `chatPanel.ts` consumes and applies to AG Grid
2. **Mode A — harness chat.** Adapter layer per `hive-edge-minds/mind_templates/`:
   - `claude_cli` adapter: long-lived `claude -p --input-format stream-json --output-format stream-json` per chat session, `--resume` for continuity, idle-timeout reaper (session lifecycle per `mind_server.py`).
   - `codex_cli` adapter: one `codex exec --json` subprocess per turn; provider-native thread id persisted for resume (the `harness_sid` pattern).
   - **Workspace per chat session:** scratch dir containing a read-only snapshot of the screening (SQLite copy + JSON exports), the analysis tools, and a `CHAT.md` brief (data schema, tool usage, stage semantics). Harness scoped to it via allowed-directory; runs inside the app container.
   - **Model routing via env overrides** (hive-edge-minds `ModelRegistry.env_overrides` pattern): `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` → UL AI proxy's Anthropic-compatible `/v1/messages` for the claude CLI; `OPENAI_BASE_URL` → proxy `/v1` or the fastLLM gateway for the codex CLI. If SIS's models prove they can drive the harness 100%, enabling that is a config flip — the tools don't change.
3. **Mode B — structured chat (small-model fallback).** A mini agentic loop in our code via `llm.chat()`: each turn the model returns strict-JSON `{tool_calls: [{tool, args}], answer?}`; the backend executes the **same tools** on the model's behalf, appends results, and iterates (capped rounds) until the model emits a final `answer`. Grid actions flow through the same action queue. This keeps what-if and rank-explanation working even on models that can only emit JSON.
4. **Mode selection + degradation:** per-model `chat_mode` (`harness` | `structured`) on the config page — default `harness` for Claude/Codex-capable models, `structured` for SIS small models. A harness turn that errors or times out falls back to structured for that turn (logged, surfaced in the UI).
5. `chatPanel.ts`: chat sidebar per stage tab, mode-agnostic — answers stream into the thread, grid actions arrive over the session action channel and apply via the AG Grid API. Transcript persisted per screening+stage, tagged with mode + model.

**Done when:** the three canonical questions work in **both** modes — including a real recomputed diff for the what-if — and chat-driven sort/filter still manipulates the grid (reqs 7 + 8).

### Phase 6 — Bias audit mode

1. Decode the SWE_pdf filename convention first: extract text from baseline/variant pairs (e.g. `Resume_1_Ayaan_Rahman` vs `Resume_31_(BG1_R1)Ayaan_Rahman`) and diff to confirm what BG/BS/BE and G/R/RA encode. Confirm findings with the user before hard-coding the pairing map.
2. `audit.py`: pairing parser (filename codes → baseline↔variant pairs grouped by protected attribute), audit runner (evaluate the full corpus with the active provider/model against a chosen qual list), delta computation per pair: per-qual verdict flips, required/preferred coverage deltas, stage-1 pass/fail flips, rank displacement.
3. Report: aggregate by attribute (gender / race / race+age): mean|max coverage delta, count of verdict flips, count of stage-outcome flips. Pass/fail against a **configurable threshold** (default proposal: zero stage-outcome flips, mean coverage delta ≤ 0.5 quals — TBD with user, stored in config).
4. `auditPage.ts`: pick qual list + provider/model, run (reuses progress polling), results grid (pairs + deltas, worst first) + summary banner + Excel export. Persisted as `audit_run` so results across models are comparable.

**Done when:** one click runs the 34-resume corpus and produces a per-attribute delta report proving (or disproving) negligible bias for the active model.

### Phase 7 — Docs

1. Rewrite `CLAUDE.md` for the new architecture; rewrite `README.md` (setup, env vars, screening workflow, audit mode).
2. One backlog list in docs naming the deleted features that could return as new builds if ever needed: anonymization, cover-letter AI detection, custom evaluation fields. No code seams reserved for them.

## Test strategy

Deterministic logic gets real tests (pytest): filename pairing parser, rollup/ranking math, stage-gating rules, ChatTurn action validation, JD-parse schema handling, provider clients against mocked HTTP (httpx MockTransport / responses). LLM-quality concerns (verdict accuracy, JD parse quality) are validated manually with the SWE_pdf corpus and a real UT position description — no flaky LLM-in-the-loop unit tests.

## Order and dependencies

Phases are sequential by default (each builds on the previous), but Phase 6 step 1 (filename decoding) can happen anytime, and Phase 5 (chat) can slip after Phase 6 if audit results are needed sooner. Phases 0–3 are the critical path to anything demonstrable; Phase 4 makes it usable; 5 and 6 complete the requirements.

## Open items (blocking specific steps only)

| # | Item | Blocks | Status |
|---|------|--------|--------|
| 1 | SIS fastLLM gateway URL, auth, API flavor, model list | Phase 1 step 3 going live; also determines whether SIS models can drive the codex CLI harness | **Mostly resolved 2026-07-28.** The API flavor and auth were never unknown — the Streamlit app's own gateway client on `origin/VDI` (`mistral_client.py` at `4cc18c9`) documents them: OpenAI chat-completions shape, `LLM_GATEWAY_URL`/`LLM_GATEWAY_KEY`, `hl-project-id`/`hl-requester-id` attribution headers, pinned `seed`, models named `llama-3.2-90b-vision-instruct` / `Nemotron 49b` / `GPT 120b`. `backend/llm/fastllm.py` now implements that contract. **Still needed:** the URL and key values themselves, and confirmation of whether `/v1/models` listing works (the old client never listed). |
| 2 | Claude/Codex CLI availability + auth inside the app container (via UL AI proxy creds) | Phase 5 Mode A | **Still open.** Adapters ship and route by env override; neither CLI is installed in the image yet, so harness turns degrade to structured until one is added. |
| 3 | "Negligible" bias threshold | Phase 6 pass/fail line | **Settled 2026-07-28:** zero stage-outcome flips, mean absolute coverage delta ≤ 0.5 qualifications. Stored in config and editable on the Configuration page. |
| 4 | SWE_pdf filename code semantics | Phase 6 pairing map | **Settled 2026-07-28:** `G` = gender, `R` = religion, `RA` = race/ethnicity & national origin; no age signal. `BG4_G4` and `BE2_G2` are negative controls. See `requirements.md` §6 and `backend/audit.py`. |
