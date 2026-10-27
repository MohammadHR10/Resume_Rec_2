# Built-in agent controls

Every provider uses the same tool-only loop. The model emits `tool_calls` with a tool
name and a JSON argument object encoded as `args_json`. The backend validates the calls,
executes allowlisted handlers, and returns results. `reply({message})` is the only way
to produce a user-facing explanation. There are at most eight rounds and four calls per
round; `reply` must be called alone after the other tool results.

| Controls | Tools | Behavior |
| --- | --- | --- |
| Screening state and selected rows | `get_app_state`, `list_screenings` | Read current checklist, candidate IDs/stages, selection, filters, model and proposals |
| Applicant research | `find_candidates`, `read_candidate`, `analyze_candidates` | Combined filters, paginated sources and written verdict explanations, custom cohorts and deterministic comparisons |
| What-if analysis | `whatif` | Recompute coverage and rank with qualifications removed; no stored data changes |
| Grid | `grid_action`, `select_candidates`, `show_evidence` | Sort, filter, clear, select exact IDs and open qualification evidence |
| Navigation and layout | `navigate`, `set_panel` | Open pages/stages; expand or collapse checklist, resumes, history and chat; close evidence |
| Files | `export_stage`, `upload_documents` | Offer a download link or file chooser; the reviewer chooses and submits local files |
| Checklist | `save_qualifications` | Review the full ordered checklist and confirmation state; changed items invalidate evaluations |
| Evaluation | `evaluate_candidates`, `get_job` | Review evaluation start; poll progress. Evaluation resets all candidates to stage 1 |
| Candidate decisions | `stage_candidates`, `get_promotion_history` | Review exact candidates and source stage; promote/reject/restore through the same application handler as the grid |
| Screening lifecycle | `create_screening`, `delete_screening`, `load_corpus` | Review creation, deletion or benchmark import |
| Models | `list_models`, `test_provider`, `configure_model` | Discover/test configured providers; review active model changes |
| Bias audit | `list_corpora`, `list_audits`, `get_audit`, `start_audit`, `delete_audit`, `configure_audit` | Read results; review runs, deletions and threshold changes |
| Conversation | `reply` | Explain results, ask questions or describe pending review |

Tool argument models and descriptions live in `backend/chat/app_tools.py` and
`backend/chat/candidate_data.py`; script arguments live in `backend/chat/structured.py`.
See [applicant research examples](candidate-research.md). Tools invoke existing application functions,
so the human UI and assistant share validation and workflow rules.

## Human decisions

Writes create persistent `chat_proposal` rows. Chat displays the exact proposed values and
candidate names, source stage and override count. **Apply change** claims the proposal once,
checks its screening/settings fingerprint, and invokes the existing app operation.
**Cancel** records cancellation without modifying data. The model has no approval endpoint
tool. A stale proposal requires a new request. A failed turn can still leave a reviewable
proposal, which the chat reloads even when it displays an error.

UI actions are separate from writes. Stable `chat_action` IDs prevent duplicate application
through both chat responses and polling; action order preserves stage changes before grid
operations. Polling is stopped when the stage or page changes. Main-window selection is
stored by screening/stage in browser storage for the pop-out chat to read.

## Boundaries

- No arbitrary commands, code execution, SQL, filesystem access or arbitrary HTTP tool.
- Analysis subprocesses run fixed installed scripts, not copies a prior CLI could edit.
- A workspace holds only the current screening snapshot and brief; a database copy is no
  longer created. Legacy database copies for a session are removed when it next runs.
- Credential entry and endpoint changes stay on Configuration. The agent can navigate there
  and select/test models, but cannot read or set tokens or provider URLs.
- Local file paths are never model arguments. Upload controls require file selection and
  submission by the reviewer. Exports cover the entire stage, matching the existing export UI.
- Browser-controlled actions such as opening a pop-out window stay available through their
  existing buttons. The agent can collapse/reopen chat but does not launch new windows.
- Legacy CLI adapters remain in the repository for reference/tests; no chat runtime path
  invokes them, even with old `harness` configuration values.

Run `python -m pytest` and `cd frontend; npm run build`. Integration regressions in
`tests/test_app_tools.py` cover tool-only output, invalid calls, script isolation, approvals,
stale state, duplicate application, scope, selection and shared action IDs.

Verified on 2026-09-11: 225 tests passed and the frontend production build passed.
Live Playwright MCP checks with the configured provider exercised creation of a separate test
screening, checklist approval, the agent's file chooser, two-resume evaluation, selection and
evidence, promotion approval from the pop-out, main-window refresh, stage navigation followed
by sorting, a downloaded stage CSV, and deletion of the test screening. The original benchmark
candidate data was retained. Bias audit execution and the alternate provider were covered by
the deterministic/API tests rather than new live model runs.

Applicant research was also exercised through Playwright: the agent compared two Stage 1
R2 Partial applicants using their actual extracted text and stored reasoning, then compared
overlapping cohorts across the 48-applicant benchmark. The live response reported 29 AI Pass
applicants, 37 meeting at least one preferred qualification, 26 in both groups, and 8 in neither;
these counts were independently checked against the stored evaluations.
