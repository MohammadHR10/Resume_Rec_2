# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Streamlit app that evaluates PDF resumes against a job description using the Mistral AI chat API, plus a second tab that classifies cover letters as AI-generated vs. human-written. Results are validated with Pydantic and exported as styled Excel reports.

**A full rebuild is planned** (FastAPI + TypeScript staged-screening app modeled on the sibling `ResumeAI` repo). Read `docs/requirements.md` (scope and decisions — single source of truth) and `docs/implementation-plan.md` (phased build) before doing anything. The Streamlit code described below gets **deleted outright** in the rebuild's Phase 0 (git history on `main` is the archive), so don't invest in it.

## Commands

```powershell
# Setup
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# Run the app (primary entry point)
streamlit run app.py            # http://localhost:8501

# Docker
docker compose up --build       # dev, port 8501
docker compose -f docker-compose.prod.yaml up   # prod, port 80 + optional nginx profile
```

Requires a `.env` file in the repo root with `mistral_api=<key>` (note the lowercase, non-standard variable name — loaded in `mistral_client.py`).

There is no test framework or linter configured. `test_api.py` is a manual script that hits a locally running API with a real PDF; it is not runnable via pytest.

## Architecture

Almost everything lives in `app.py` (~1550 lines), which is both the Streamlit script and the home of the core domain logic. It renders two tabs:

1. **Resume Analysis** (`with tab1:`) — the main flow:
   - User enters job details, optional custom evaluation fields (string/boolean), and custom scoring criteria definitions (kept in `st.session_state`).
   - PDFs are collected from individual uploads and/or a ZIP archive (`process_uploaded_files`), text extracted via `pdf_extract.py` (pdfminer.six).
   - Optional anonymization: `anonymize_text` / `extract_personal_info` redact names, contact info, schools, etc. before the text is sent to the AI; a separate Excel mapping report links placeholders back to originals.
   - A Pydantic model is built **dynamically per run** with `build_dynamic_model(custom_fields)` — `BASE_FIELDS` (scores 1–5, explanations, recommendation of `Recommended | Consider | Pass`) plus `<field>`, `<field>_score`, `<field>_explanation` for each custom field, with `extra: "forbid"`.
   - The prompt (`build_eval_prompt` + `schema_text`, both defined inside the tab1 block) instructs Mistral to return JSON matching that schema. The raw response goes through `clean_json_output` (smart quotes, trailing commas, control chars) before validation.
   - Validated evaluations are rendered and exported to Excel (`create_excel_report`, styled via openpyxl, sized with `adjust_sheet_dimensions`).

2. **Cover Letter Analysis** (`with tab2:`) — logic lives in `cover_letter_analyzer.py`: `analyze_cover_letter_with_ai` returns an AI-generated-probability classification validated against `CoverLetterAnalysis`, with its own JSON cleaner and Excel report builder.

Supporting modules:
- `mistral_client.py` — single `call_mistral(prompt)` function; plain `requests` POST to `mistral-small-latest`, no SDK despite `mistralai` being in requirements.
- `pdf_extract.py` — `extract_text_from_pdf(file_like)` wrapper around pdfminer.

## Known landmines

- **`main.py` (FastAPI) is stale/broken.** It does `from app import Evaluation, build_eval_prompt`, but `Evaluation` no longer exists in `app.py` (replaced by the dynamic `build_dynamic_model`), and importing `app` would execute the whole Streamlit script anyway. `test_api.py` likewise targets endpoints (`/health`, `/batch_recommend`) that `main.py` never defined. The README describes this API as working; treat the Streamlit app as the only functioning interface unless the task is to fix the API.
- `enhanced_client.py` is empty; `fix_indentation.py` is a one-off repair script — neither is part of the app.
- The AI response parsing is regex-based JSON extraction, not structured output; changes to `BASE_FIELDS` or the prompt schema must be kept in sync with `schema_text`/`build_eval_prompt`, or validation will fail at runtime.
- The Dockerfile only runs Streamlit (port 8501); nothing serves the FastAPI app in containers.
