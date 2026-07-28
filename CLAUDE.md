# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

A **staged resume screening app**: FastAPI + SQLite backend, Vite/TypeScript/Bootstrap
frontend with AG Grid. A hiring reviewer drops in a position-description PDF, edits the
qualification checklist the LLM extracted from it, uploads resumes, and then walks
candidates through a three-stage human-gated funnel. A per-stage chat answers open-ended
questions about the results, and a built-in bias audit measures whether disclosing a
protected characteristic changes any outcome.

`docs/requirements.md` is the single source of truth for scope; `docs/implementation-plan.md`
records how it was built and what is still open; `docs/backlog.md` lists what was
deliberately dropped from the Streamlit-era app.

**The division of labour is the whole design.** The model produces exactly one thing: a
per-qualification verdict (`Meets` / `Partial` / `No`) with evidence quoted from the resume.
Every number a human acts on — coverage rollups, the stage-1 pass recommendation, the rank
order, what-if analysis — is computed in Python. Do not move any of that into a prompt.

## Commands

```powershell
# Setup
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env      # then fill in the provider credentials

# Backend (serves ../dist when it exists)
uvicorn backend.server:app --reload --port 8000

# Frontend dev server (proxies /api to :8000)
cd frontend && npm install && npm run dev     # http://localhost:5173

# Frontend production build -> ./dist, served by FastAPI at :8000
cd frontend && npm run build

# Tests
python -m pytest                # all deterministic logic; no LLM calls

# Docker (multi-stage: Node builds the frontend, Python serves it)
docker compose up --build       # http://localhost:8000
```

## Architecture

### Backend (`backend/`)

- **`server.py`** — every API route, plus the static mount for `dist/`. Thin: it validates,
  delegates, and shapes responses. Background work (`evaluate`, `audits`) is an
  `asyncio.create_task` writing progress into `pipeline.JOBS` for the polling endpoint.
- **`db.py`** — raw `sqlite3`, connection per unit of work, schema in one `SCHEMA` string.
  Tables: `screening`, `qualification`, `candidate`, `evaluation`, `stage_state`,
  `stage_action`, `config`, `chat_session`, `chat_message`, `chat_action`, `audit_run`.
  Config is a key/value table holding JSON, including the `connections` key, which **may
  hold a provider bearer token** entered on the configuration page. A stored value overrides
  the environment. The token is write-only over the API — `GET /api/config` returns only
  `hasKey` and a masked hint — and `chat/workspace.py` strips the `config` table from the
  database copy handed to a chat harness. If you add another consumer of the database file,
  check whether it needs the same treatment.
- **`llm/`** — `base.py` holds the provider interface (`structured_extract`, `chat`) and the
  shared OpenAI-compatible client with retry/backoff, strict-schema coercion, and an
  automatic demotion to prompt-instructed JSON when a gateway rejects `response_format`.
  `ulproxy.py` and `fastllm.py` are configuration only. `registry.py` resolves the active
  provider from stored config on every call, so a config change takes effect without a restart.
- **`jd_parse.py`** — position description → itemized qualifications. One atomic,
  verdict-able item per entry; compound bullets get split.
- **`pipeline.py`** — the core. `evaluate_resume` (one LLM call per candidate),
  `compute_rollups`, `rank_candidates`, batching with progress, and `load_rows` (the one
  query everything reads a screening through).
- **`chat/`** — `session.py` picks the mode and degrades a failed harness turn to structured
  *for that turn*; `workspace.py` builds the per-session scratch dir (snapshot, brief, copied
  tools, action queue); `structured.py` is the in-code agentic loop; `adapters/` are the CLI
  seams; `tools/` are standalone scripts run identically by both modes.
- **`audit.py`** — corpus pairing, delta computation, per-attribute aggregation, pass/fail.
- **`export.py`** — styled Excel and CSV per stage grid.

### Frontend (`frontend/src/`)

Vanilla TypeScript, no framework. `main.ts` is a hash router plus the screening workflow;
`api.ts` is the only place that talks HTTP; each view is one module (`configPage`, `jdIntake`,
`stageGrid`, `chatPanel`, `auditPage`). `tsconfig.json` sets `erasableSyntaxOnly`, so
**constructor parameter properties do not compile** — declare fields explicitly.

## Things worth knowing before you change something

- **AG Grid v34 themes via the Theming API, not CSS.** The header's fill and weight come from
  `screeningTheme` in `stageGrid.ts`. A stylesheet rule targeting `.ag-header` loses to the
  generated styles; change the theme params instead.
- **Editing a confirmed checklist deletes that screening's evaluations.** Verdicts key off
  qualification ids, so a changed list makes them unreadable. `update_qualifications` clears
  them deliberately rather than mismapping them — do not "fix" this by trying to match on text.
- **A failed evaluation is stored, not skipped.** `store_failure` writes a row with zero
  coverage and an error message, so nobody silently vanishes from a hiring grid.
- **A missing verdict counts as unmet**, never as a smaller denominator. A model that skips
  an item must not make that candidate look better than one that answered it.
- **`candidate_key` has two modes.** `LastName, FirstName …` filenames group a resume and a
  cover letter together; everything else uses the whole stem, because truncating names like
  `Resume_31_(BG1_R1)Ayaan_Rahman` would collide every audit variant.
- **Chat tools import the real ranking code.** The copied tools in a workspace find
  `backend/` through a generated `tools/_bootstrap.py`. That is what makes what-if a true
  recompute rather than a second implementation that can drift.
- **Grid actions travel by file.** `grid_action.py` appends to `actions.jsonl` in the
  workspace; the backend drains it into `chat_action` after each turn. No credentials needed
  inside the workspace, and it works identically in both chat modes.
- **The audit strips the corpus's construction marker** (`◆ BG1/R1`) before evaluating.
  Without that, every variant carries one identical artifact no baseline has.

## Currently open

- **SIS fastLLM gateway**: the request contract is taken from the previous app's working
  client on `origin/VDI` (`LLM_GATEWAY_URL`/`LLM_GATEWAY_KEY`, `hl-project-id` and
  `hl-requester-id` headers, pinned `seed`, models named like `GPT 120b`). Only the URL and
  key values are still missing. Model listing is unproven — the old client never listed, so
  `/v1/models` may 404 and the config page falls back to a free-text model field.
- **Neither CLI harness is installed in the image.** `chat_mode: harness` therefore degrades
  to structured on every turn (surfaced in the UI). Adding `claude` or `codex` to the
  Dockerfile plus proxy credentials is all that is missing.
