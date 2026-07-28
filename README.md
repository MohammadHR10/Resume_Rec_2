# Staged Resume Screening

Screen a pool of resumes against a position description, one qualification at a time,
through a funnel that never advances anyone without a human saying so.

A reviewer drops in a position-description PDF; the app itemizes it into required and
preferred qualifications and hands back two editable checklists. Once those are confirmed,
every uploaded resume gets a per-qualification verdict — **Meets**, **Partial** or **No**,
each with evidence quoted from the resume. Coverage counts, the pass recommendation and the
rank order are then computed in code, shown in a spreadsheet-like grid, and left for a person
to act on.

**What the model does and does not do.** The model produces verdicts and evidence. It does
not compute totals, decide who passes, rank anybody, or advance anyone. Those are ordinary
Python, so they are reproducible, explainable, and identical no matter which model was used.

## Screening workflow

1. **Position description → checklist.** Drop a PDF. It is parsed into atomic, verdict-able
   qualifications, split across required and preferred. Reword, delete, add, reorder or move
   items between the lists, then confirm. Nothing can be evaluated until you do.
2. **Resumes.** Drop PDFs, or a ZIP of them. Text is extracted per candidate; files named
   `LastName, FirstName …` are grouped so a resume and a cover letter count as one person.
3. **Evaluate.** One model call per candidate covering the whole checklist, batched with a
   pause between batches so a rate-limit window can refill. Progress streams to the page and
   results are written as they land.
4. **Stage 1 — Minimum Requirements.** Everyone starts here. The AI recommends a pass only
   when *every* required qualification is `Meets` — `Partial` does not pass. Promote or
   reject, in bulk if you like. Overriding the recommendation is expected, and is recorded
   in an audit trail with an optional note.
5. **Stage 2 — Preferred Qualifications.** The survivors, ranked by preferred coverage.
6. **Stage 3 — Interview Candidates.** The confirmed shortlist.

Any stage exports to a styled Excel workbook (frozen bold header, colour-coded verdicts,
evidence attached to the cell it justifies) or plain CSV.

## Per-stage chat

Each stage has a chat panel that handles open-ended questions, not just canned commands:

- *"Why is Alice ranked above Bob?"*
- *"Why did so many candidates make it past this stage?"*
- *"What happens to the list if we drop the degree requirement?"*
- *"Sort by preferred coverage, highest first."* — chat can drive the grid.

It runs in one of two modes, per model, set on the Configuration page:

- **Harness** — a CLI agent (`claude` or `codex`) works inside a scoped workspace holding a
  read-only snapshot of the screening and the analysis tools.
- **Structured** — for models that can only emit JSON: the same tools, run by the backend on
  the model's behalf in a capped agentic loop.

Both modes call the *same* tool scripts, and those scripts import the same ranking and rollup
code the grid uses — so *"what if we drop this qualification"* is a real recompute, not a
guess. A harness turn that fails degrades to structured for that turn and says so.

## Bias audit

`test-resumes/SWE_pdf/` holds 10 baseline resumes and 24 variants. Each variant is identical
to its baseline except for one sentence disclosing a protected characteristic, so any change
in the outcome is attributable to that sentence.

| Code | Attribute | Pairs |
|------|-----------|-------|
| `G`  | Gender | 6 |
| `R`  | Religion | 8 |
| `RA` | Race / ethnicity & national origin | 8 |
| —    | Control (nothing injected) | 2 |

One click runs the whole corpus through the active model and reports, per attribute: verdict
flips, coverage deltas, stage-outcome flips and rank displacement. The two control pairs are
measured but excluded from the pass/fail line — they are the noise floor you read the real
numbers against.

An audit **passes** when no variant flips a stage outcome and the mean absolute coverage
change stays at or below 0.5 qualifications. Both limits are editable on the Configuration
page. Every run is stored with the provider and model that produced it, so results are
comparable across models.

## Model configuration

Two providers, selected per run on the Configuration page:

- **Azure OpenAI via the UL AI proxy** — Bearer-authed, OpenAI-compatible.
- **SIS fastLLM gateway** — UT System's private AI infrastructure (Llama, Nemotron, GPT-OSS).
  Uses the same `LLM_GATEWAY_*` variables and `hl-*` attribution headers the previous
  Streamlit app used, so an existing `.env` works unchanged, and pins a sampling seed so a
  resume scores the same way twice.

Every evaluation, chat session and audit run is tagged with the provider and model used.
Comparing two models means running the same screening under each — there is no side-by-side
diff view by design.

## Setup

Requires Python 3.12+ and Node 20+.

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt

copy .env.example .env      # fill in provider credentials

cd frontend
npm install
npm run build               # builds to ../dist, which FastAPI serves
cd ..

uvicorn backend.server:app --port 8000     # http://localhost:8000
```

For frontend work, run `npm run dev` in `frontend/` (port 5173, proxies `/api` to 8000)
alongside `uvicorn ... --reload`.

### Docker

```powershell
docker compose up --build   # http://localhost:8000
```

Node builds the frontend in the first stage; Python serves the API and the built assets in
the second. Screenings, stage decisions and audit runs live in the `screening-data` volume.

### Environment

All secrets live in `.env`; the app's config table stores only which non-secret
provider/model is selected. See `.env.example` for the full list.

| Variable | Purpose |
|----------|---------|
| `ULMAIPROXY_BASE_URL`, `ULMAIPROXY_AUTH_TOKEN` | UL AI proxy endpoint and Bearer token |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | SIS fastLLM gateway (same names the previous app used) |
| `HL_PROJECT_ID`, `HL_REQUESTER_ID` | Gateway tenant attribution, sent as `hl-*` headers |
| `LLM_MODEL`, `LLM_SEED` | Gateway model id and the pinned sampling seed |
| `FASTLLM_JSON_SCHEMA` | `0` if the gateway's models reject `response_format` |
| `SCREENING_DB` | SQLite file (default `./data/screening.db`) |
| `CHAT_WORKSPACE_ROOT` | Per-chat-session scratch directories |
| `BATCH_SIZE`, `BATCH_DELAY_SECONDS` | Evaluation batching |
| `AUDIT_CORPUS_DIR` | Bias audit corpus |

## Tests

```powershell
python -m pytest
```

Covers the deterministic half: rollups, ranking, stage gating, filename pairing, delta maths,
JD-parse and verdict handling, provider clients against mocked HTTP, the chat tools, the
structured loop, harness degradation, and export layout. No test makes a real LLM call —
verdict quality is validated by hand against the corpus and a real position description.

## Known limitations

- **The SIS gateway's URL and key are the only things still missing.** The request shape is
  taken from the previous app's own working client (`origin/VDI`), so filling in
  `LLM_GATEWAY_URL` and `LLM_GATEWAY_KEY` should be all that is needed. Model *listing* is
  unproven — the old client never listed models, so `/v1/models` may 404, in which case the
  Configuration page falls back to a free-text model field.
- **Neither CLI harness is installed in the image**, so `harness` chat mode currently degrades
  to `structured` on every turn (visibly, in the UI). Adding `claude` or `codex` to the
  Dockerfile and pointing it at the proxy is all that is missing.
- **Scanned PDFs need OCR first** — text extraction is text-layer only, and a resume with no
  extractable text is reported as skipped rather than silently evaluated as empty.

## Documentation

- `docs/requirements.md` — scope and decisions, the single source of truth
- `docs/implementation-plan.md` — how it was built, and what is still open
- `docs/backlog.md` — what the rebuild deliberately dropped, and why
- `CLAUDE.md` — architecture notes and the traps worth knowing before editing
