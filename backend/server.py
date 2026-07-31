"""FastAPI application: screening API + static frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, db, export, jd_parse
from .chat import session as chat_session
from .extraction import (
    extract_text_from_bytes,
    extract_text_from_docx,
    extract_text_from_pdf,
    looks_binary,
)
from .llm import registry
from .llm.base import LLMError
from .pipeline import (
    JOBS,
    candidate_key,
    display_name,
    load_qualifications,
    load_rows,
    new_job,
    run_evaluation,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    logger.info("Database ready at %s", db.DB_PATH)
    yield


app = FastAPI(title="Staged Resume Screening", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_STAGES = ("1", "2", "3", "rejected")
NEXT_STAGE = {"1": "2", "2": "3"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _screening_or_404(screening_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM screening WHERE id=?", (screening_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Screening not found")
    return row


def _qual_payload(screening_id: str) -> list[dict[str, Any]]:
    quals = load_qualifications(screening_id)
    counters = {"required": 0, "preferred": 0}
    out = []
    for qual in quals:
        counters[qual["kind"]] += 1
        prefix = "R" if qual["kind"] == "required" else "P"
        out.append({**qual, "label": f"{prefix}{counters[qual['kind']]}"})
    return out


#: What the JD dropzone accepts, and how each type is read.
DOCUMENT_READERS = {
    ".pdf": lambda data: extract_text_from_bytes(data),
    ".docx": lambda data: extract_text_from_docx(data),
    ".txt": lambda data: data.decode("utf-8", errors="replace"),
    ".md": lambda data: data.decode("utf-8", errors="replace"),
}


def _extract_document(filename: str, contents: bytes) -> str:
    """Read an uploaded position description, or explain why it cannot be read.

    Routing on the extension rather than falling back to a UTF-8 decode: the
    fallback silently turned a .docx into mojibake, handed that to the model,
    and produced an empty checklist that looked like a successful parse.
    """
    suffix = os.path.splitext(filename)[1].lower()
    reader = DOCUMENT_READERS.get(suffix)
    if reader is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{filename or 'That file'} is a {suffix or 'unrecognised'} file. "
                f"Upload one of: {', '.join(sorted(DOCUMENT_READERS))}."
                + (
                    " Save the .doc as .docx or export it to PDF first."
                    if suffix == ".doc"
                    else ""
                )
            ),
        )

    text = reader(contents)
    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                f"No text could be extracted from {filename}. "
                + (
                    "Scanned PDFs hold images rather than text and need OCR first."
                    if suffix == ".pdf"
                    else "The file may be empty or corrupt."
                )
            ),
        )
    if looks_binary(text):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{filename} does not appear to contain readable text — it may be a "
                f"different format than its {suffix} extension suggests."
            ),
        )
    return text


def _stage_counts(screening_id: str) -> dict[str, int]:
    counts = {stage: 0 for stage in VALID_STAGES}
    for row in db.query(
        "SELECT COALESCE(s.stage, '1') AS stage, COUNT(*) AS n FROM candidate c "
        "LEFT JOIN stage_state s ON s.candidate_id = c.id WHERE c.screening_id=? GROUP BY 1",
        (screening_id,),
    ):
        counts[str(row["stage"])] = row["n"]
    return counts


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConnectionUpdate(BaseModel):
    provider: str
    baseUrl: str | None = None
    # None means "leave the stored token alone" — the UI submits the form
    # without re-typing it. An empty string clears it.
    apiKey: str | None = None
    projectId: str | None = None
    requesterId: str | None = None


class ConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    chatMode: str | None = None
    harnessCli: str | None = None
    auditThresholds: dict[str, float] | None = None
    connection: ConnectionUpdate | None = None


def _connection_cards() -> list[dict[str, Any]]:
    """Per-provider connection state for the config page.

    The token is never returned — only whether one is present, a four-character
    hint so an operator can tell which one it is, and where it came from.
    """
    cards = []
    for name in registry.PROVIDERS:
        stored = db.get_connection(name)
        provider = registry.get_provider(name)
        cards.append(
            {
                "provider": name,
                "label": registry.PROVIDER_LABELS[name],
                "baseUrl": provider.base_url,
                "baseUrlSource": _source(stored["base_url"], provider.base_url),
                "hasKey": bool(provider.api_key),
                "keyHint": db.mask_secret(provider.api_key),
                "keySource": _source(stored["api_key"], provider.api_key),
                "projectId": getattr(provider, "project_id", ""),
                "requesterId": getattr(provider, "requester_id", ""),
                "supportsAttribution": hasattr(provider, "project_id"),
                "configured": provider.is_configured(),
            }
        )
    return cards


def _source(stored: str, effective: str) -> str:
    if stored:
        return "config"
    return "environment" if effective else "unset"


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    from .chat.adapters import available_adapters

    cfg = db.get_config()
    _, provider_name, model = registry.active()
    return {
        "providers": registry.describe_all(),
        "connections": _connection_cards(),
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "chatMode": registry.chat_mode(provider_name, model),
        "chatModes": cfg.get("chat_modes") or {},
        "harnessCli": cfg.get("harness_cli") or chat_session.DEFAULT_HARNESS.get(provider_name, ""),
        "auditThresholds": cfg.get("audit_thresholds"),
        "adapters": available_adapters(),
    }


@app.put("/api/config")
def update_config(body: ConfigUpdate) -> dict[str, Any]:
    cfg = db.get_config()
    updates: dict[str, Any] = {}

    if body.provider is not None:
        if body.provider not in registry.PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider}")
        updates["provider"] = body.provider
    if body.model is not None:
        updates["model"] = body.model
    if body.chatMode is not None:
        if body.chatMode not in ("harness", "structured"):
            raise HTTPException(status_code=400, detail="chatMode must be harness or structured")
        modes = dict(cfg.get("chat_modes") or {})
        provider = updates.get("provider", cfg.get("provider"))
        model = updates.get("model", cfg.get("model"))
        modes[f"{provider}:{model}"] = body.chatMode
        updates["chat_modes"] = modes
    if body.harnessCli is not None:
        updates["harness_cli"] = body.harnessCli
    if body.auditThresholds is not None:
        updates["audit_thresholds"] = {
            **(cfg.get("audit_thresholds") or {}),
            **body.auditThresholds,
        }

    if updates:
        db.set_config(updates)

    if body.connection is not None:
        if body.connection.provider not in registry.PROVIDERS:
            raise HTTPException(
                status_code=400, detail=f"Unknown provider: {body.connection.provider}"
            )
        db.set_connection(
            body.connection.provider,
            {
                "base_url": body.connection.baseUrl,
                "api_key": body.connection.apiKey,
                "project_id": body.connection.projectId,
                "requester_id": body.connection.requesterId,
            },
        )
        logger.info(
            "Updated stored connection for %s (token %s)",
            body.connection.provider,
            "changed" if body.connection.apiKey is not None else "unchanged",
        )

    return get_config()


@app.get("/api/config/models")
async def list_models(provider: str | None = None) -> dict[str, Any]:
    cfg = db.get_config()
    name = provider or cfg.get("provider")
    if name not in registry.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {name}")
    client = registry.get_provider(name)
    models = await asyncio.get_event_loop().run_in_executor(None, client.list_models)
    return {"provider": name, "configured": client.is_configured(), "models": models}


@app.post("/api/config/test")
async def test_provider(provider: str | None = None) -> dict[str, Any]:
    cfg = db.get_config()
    name = provider or cfg.get("provider")
    if name not in registry.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {name}")
    client = registry.get_provider(name, cfg.get("model") or "")
    health = await asyncio.get_event_loop().run_in_executor(None, client.health)
    return {"provider": name, **health}


# ---------------------------------------------------------------------------
# Screenings
# ---------------------------------------------------------------------------

class ScreeningCreate(BaseModel):
    jobTitle: str = ""


@app.post("/api/screenings")
def create_screening(body: ScreeningCreate) -> dict[str, Any]:
    screening_id = db.new_id()
    db.execute(
        "INSERT INTO screening (id, job_title, created_at) VALUES (?,?,?)",
        (screening_id, body.jobTitle.strip(), db.now()),
    )
    return {"id": screening_id}


@app.get("/api/screenings")
def list_screenings() -> list[dict[str, Any]]:
    return db.query(
        "SELECT s.id, s.job_title, s.status, s.provider, s.model, s.quals_confirmed, s.created_at, "
        "(SELECT COUNT(*) FROM candidate c WHERE c.screening_id = s.id) AS candidates, "
        "(SELECT COUNT(*) FROM qualification q WHERE q.screening_id = s.id) AS qualifications "
        "FROM screening s WHERE s.kind='screening' ORDER BY s.created_at DESC, s.rowid DESC"
    )


@app.get("/api/screenings/{screening_id}")
def get_screening(screening_id: str) -> dict[str, Any]:
    screening = _screening_or_404(screening_id)
    return {
        "screening": {
            "id": screening["id"],
            "jobTitle": screening["job_title"],
            "jdFilename": screening["jd_filename"],
            "hasJd": bool(screening["jd_text"]),
            "status": screening["status"],
            "qualsConfirmed": bool(screening["quals_confirmed"]),
            "provider": screening["provider"],
            "model": screening["model"],
            "createdAt": screening["created_at"],
        },
        "qualifications": _qual_payload(screening_id),
        "stageCounts": _stage_counts(screening_id),
    }


@app.delete("/api/screenings/{screening_id}")
def delete_screening(screening_id: str) -> dict[str, bool]:
    _screening_or_404(screening_id)
    db.execute("DELETE FROM screening WHERE id=?", (screening_id,))
    return {"deleted": True}


@app.post("/api/screenings/{screening_id}/parse-jd")
async def parse_jd(screening_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _screening_or_404(screening_id)
    contents = await file.read()
    text = _extract_document(file.filename or "", contents)

    provider, provider_name, model = registry.active()
    if not model:
        raise HTTPException(
            status_code=400, detail="No model is selected — choose one on the Configuration page."
        )

    try:
        parsed = await asyncio.get_event_loop().run_in_executor(
            None, lambda: jd_parse.parse_qualifications(provider, text, model=model)
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    db.execute(
        "UPDATE screening SET jd_text=?, jd_filename=?, job_title=COALESCE(NULLIF(job_title,''), ?), "
        "quals_confirmed=0, provider=?, model=? WHERE id=?",
        (
            text,
            file.filename or "",
            parsed["job_title"],
            provider_name,
            model,
            screening_id,
        ),
    )
    _replace_qualifications(
        screening_id,
        [{"text": t, "kind": "required"} for t in parsed["required"]]
        + [{"text": t, "kind": "preferred"} for t in parsed["preferred"]],
    )

    qualifications = _qual_payload(screening_id)
    return {
        "jobTitle": parsed["job_title"],
        "qualifications": qualifications,
        "provider": provider_name,
        "model": model,
        # A parse that found nothing is not a successful parse. Saying so here
        # is what stops the UI reporting a win over an empty checklist.
        "warning": (
            ""
            if qualifications
            else "The document was read, but no qualifications could be identified in it. "
            "Check that it contains a qualifications or requirements section, or add the "
            "items by hand below."
        ),
    }


class QualificationIn(BaseModel):
    text: str
    kind: str


class QualificationsUpdate(BaseModel):
    qualifications: list[QualificationIn]
    confirmed: bool | None = None


def _replace_qualifications(screening_id: str, quals: list[dict[str, str]]) -> None:
    """Rewrite the whole list.

    The editable checklist is reordered, retyped and regrouped freely, so
    diffing it would buy nothing but bugs. Verdicts key off qualification ids,
    which is why replacing the list also clears the evaluations that referenced
    the old ones — see ``update_qualifications``.
    """
    db.execute("DELETE FROM qualification WHERE screening_id=?", (screening_id,))
    rows = []
    position = {"required": 0, "preferred": 0}
    for qual in quals:
        kind = qual["kind"] if qual["kind"] in ("required", "preferred") else "required"
        text = (qual["text"] or "").strip()
        if not text:
            continue
        rows.append((db.new_id(), screening_id, text, kind, position[kind]))
        position[kind] += 1
    if rows:
        db.execute_many(
            "INSERT INTO qualification (id, screening_id, text, kind, position) VALUES (?,?,?,?,?)",
            rows,
        )


@app.get("/api/screenings/{screening_id}/qualifications")
def get_qualifications(screening_id: str) -> dict[str, Any]:
    screening = _screening_or_404(screening_id)
    return {
        "qualifications": _qual_payload(screening_id),
        "confirmed": bool(screening["quals_confirmed"]),
    }


@app.put("/api/screenings/{screening_id}/qualifications")
def update_qualifications(screening_id: str, body: QualificationsUpdate) -> dict[str, Any]:
    _screening_or_404(screening_id)

    existing = {q["text"]: q["kind"] for q in load_qualifications(screening_id)}
    incoming = {q.text.strip(): q.kind for q in body.qualifications if q.text.strip()}
    changed = existing != incoming

    _replace_qualifications(
        screening_id, [{"text": q.text, "kind": q.kind} for q in body.qualifications]
    )

    if changed:
        # Verdicts are per-qualification-id; a changed checklist makes the old
        # ones unreadable, so they go rather than being silently mismapped.
        removed = db.query(
            "SELECT COUNT(*) AS n FROM evaluation WHERE screening_id=?", (screening_id,)
        )
        db.execute("DELETE FROM evaluation WHERE screening_id=?", (screening_id,))
        if removed and removed[0]["n"]:
            db.execute(
                "UPDATE screening SET status='draft' WHERE id=?", (screening_id,)
            )
            logger.info(
                "Cleared %s evaluation(s) for %s after the qualification list changed",
                removed[0]["n"],
                screening_id,
            )

    confirmed = 1 if body.confirmed else 0
    db.execute("UPDATE screening SET quals_confirmed=? WHERE id=?", (confirmed, screening_id))
    return {"qualifications": _qual_payload(screening_id), "confirmed": bool(confirmed)}


# ---------------------------------------------------------------------------
# Candidates and evaluation
# ---------------------------------------------------------------------------

def _collect_pdfs(tmp_dir: str, filename: str, contents: bytes) -> list[str]:
    """Write an upload to disk, flattening any ZIP into its member PDFs."""
    dest = os.path.join(tmp_dir, os.path.basename(filename))
    with open(dest, "wb") as handle:
        handle.write(contents)

    if not filename.lower().endswith(".zip"):
        return [dest] if filename.lower().endswith(".pdf") else []

    paths: list[str] = []
    try:
        with zipfile.ZipFile(dest) as archive:
            for entry in archive.namelist():
                normalized = entry.replace("\\", "/")
                name = normalized.split("/")[-1]
                if not name or name.lower().startswith("__macosx") or not name.lower().endswith(".pdf"):
                    continue
                member_path = os.path.join(tmp_dir, name)
                with open(member_path, "wb") as handle:
                    handle.write(archive.read(entry))
                paths.append(member_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"{filename} is not a readable ZIP") from exc
    finally:
        os.remove(dest)
    return paths


@app.post("/api/screenings/{screening_id}/candidates")
async def upload_candidates(
    screening_id: str,
    files: list[UploadFile] = File(...),
    replace: str = Form("true"),
) -> dict[str, Any]:
    _screening_or_404(screening_id)
    tmp_dir = tempfile.mkdtemp(prefix="resumes_")
    try:
        paths: list[str] = []
        for upload in files:
            paths.extend(_collect_pdfs(tmp_dir, upload.filename or "upload", await upload.read()))
        if not paths:
            raise HTTPException(status_code=400, detail="No PDF files were found in that upload")

        if replace.lower() in ("1", "true", "yes"):
            db.execute("DELETE FROM candidate WHERE screening_id=?", (screening_id,))

        groups: dict[str, list[str]] = {}
        for path in paths:
            groups.setdefault(candidate_key(os.path.basename(path)), []).append(path)

        loop = asyncio.get_event_loop()
        added, skipped = 0, []
        for key, group in sorted(groups.items()):
            parts = []
            for path in sorted(group):
                text = await loop.run_in_executor(None, extract_text_from_pdf, path)
                if text.strip():
                    parts.append(text)
            combined = "\n\n".join(parts)
            if not combined.strip():
                skipped.append(key)
                continue
            db.execute(
                "INSERT INTO candidate (id, screening_id, name, source_files, resume_text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    db.new_id(),
                    screening_id,
                    display_name(sorted(group)[0]),
                    json.dumps([os.path.basename(p) for p in sorted(group)]),
                    combined,
                    db.now(),
                ),
            )
            added += 1

        return {
            "added": added,
            "skipped": skipped,
            "total": db.query_one(
                "SELECT COUNT(*) AS n FROM candidate WHERE screening_id=?", (screening_id,)
            )["n"],
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/screenings/{screening_id}/evaluate")
async def start_evaluation(screening_id: str) -> dict[str, Any]:
    screening = _screening_or_404(screening_id)
    if not screening["quals_confirmed"]:
        raise HTTPException(
            status_code=400,
            detail="Confirm the qualification checklist before starting an evaluation",
        )

    quals = load_qualifications(screening_id)
    if not quals:
        raise HTTPException(status_code=400, detail="This screening has no qualifications")

    candidates = db.query(
        "SELECT id, name, resume_text FROM candidate WHERE screening_id=? ORDER BY name",
        (screening_id,),
    )
    if not candidates:
        raise HTTPException(status_code=400, detail="Upload at least one resume first")

    provider, provider_name, model = registry.active()
    if not model:
        raise HTTPException(
            status_code=400, detail="No model is selected — choose one on the Configuration page."
        )
    if not provider.is_configured():
        raise HTTPException(status_code=400, detail=f"{provider.label} is not configured")

    # Everyone starts in stage 1; nothing advances without a human.
    db.execute("DELETE FROM stage_state WHERE screening_id=?", (screening_id,))
    db.execute_many(
        "INSERT INTO stage_state (candidate_id, screening_id, stage, updated_at) VALUES (?,?,?,?)",
        [(c["id"], screening_id, "1", db.now()) for c in candidates],
    )
    db.execute("UPDATE screening SET status='evaluating' WHERE id=?", (screening_id,))

    job_id = uuid.uuid4().hex[:12]
    new_job(job_id, len(candidates))
    asyncio.create_task(
        run_evaluation(job_id, screening_id, candidates, quals, provider, provider_name, model)
    )
    return {"jobId": job_id, "candidates": len(candidates), "provider": provider_name, "model": model}


@app.get("/api/jobs/{job_id}/progress")
def job_progress(job_id: str, since: int = Query(default=0, ge=0)) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "events": job["progress"][since:],
        "status": job["status"],
        "error": job.get("error"),
        "done": job.get("done", 0),
        "total": job.get("total", 0),
    }


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@app.get("/api/screenings/{screening_id}/stages/{stage}")
def get_stage(screening_id: str, stage: str) -> dict[str, Any]:
    _screening_or_404(screening_id)
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=404, detail="Unknown stage")
    rows = [row for row in load_rows(screening_id) if str(row["stage"]) == stage]
    return {
        "stage": stage,
        "qualifications": _qual_payload(screening_id),
        "candidates": rows,
        "stageCounts": _stage_counts(screening_id),
    }


class StageAction(BaseModel):
    candidateIds: list[str]
    action: str
    note: str = ""


@app.post("/api/screenings/{screening_id}/stages/{stage}/actions")
def stage_action(screening_id: str, stage: str, body: StageAction) -> dict[str, Any]:
    _screening_or_404(screening_id)
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=404, detail="Unknown stage")
    if body.action not in ("promote", "reject", "restore"):
        raise HTTPException(status_code=400, detail="action must be promote, reject or restore")

    moved = []
    for candidate_id in body.candidateIds:
        current = db.query_one(
            "SELECT COALESCE(s.stage,'1') AS stage, e.ai_pass FROM candidate c "
            "LEFT JOIN stage_state s ON s.candidate_id=c.id "
            "LEFT JOIN evaluation e ON e.candidate_id=c.id "
            "WHERE c.id=? AND c.screening_id=?",
            (candidate_id, screening_id),
        )
        if not current:
            continue
        from_stage = str(current["stage"])

        if body.action == "promote":
            to_stage = NEXT_STAGE.get(from_stage)
            if not to_stage:
                continue  # already in the final stage, or rejected
        elif body.action == "reject":
            to_stage = "rejected"
        else:
            to_stage = "1"

        # An override is the human disagreeing with the AI, and it is the thing
        # the audit trail exists to record: promoting a candidate the model
        # failed, or rejecting one it passed.
        ai_pass = bool(current["ai_pass"])
        override = (body.action == "promote" and from_stage == "1" and not ai_pass) or (
            body.action == "reject" and from_stage == "1" and ai_pass
        )

        db.execute(
            "INSERT INTO stage_state (candidate_id, screening_id, stage, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET stage=excluded.stage, updated_at=excluded.updated_at",
            (candidate_id, screening_id, to_stage, db.now()),
        )
        db.execute(
            "INSERT INTO stage_action (id, screening_id, candidate_id, from_stage, to_stage, action, "
            "override, actor, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_id(),
                screening_id,
                candidate_id,
                from_stage,
                to_stage,
                body.action,
                1 if override else 0,
                "user",
                body.note,
                db.now(),
            ),
        )
        moved.append({"candidateId": candidate_id, "from": from_stage, "to": to_stage, "override": override})

    return {"moved": moved, "stageCounts": _stage_counts(screening_id)}


@app.get("/api/screenings/{screening_id}/audit-trail")
def audit_trail(screening_id: str) -> list[dict[str, Any]]:
    _screening_or_404(screening_id)
    return db.query(
        "SELECT a.id, a.candidate_id, c.name, a.from_stage, a.to_stage, a.action, a.override, "
        "a.actor, a.note, a.created_at FROM stage_action a "
        "JOIN candidate c ON c.id = a.candidate_id WHERE a.screening_id=? "
        "ORDER BY a.created_at DESC, a.rowid DESC",
        (screening_id,),
    )


@app.get("/api/screenings/{screening_id}/stages/{stage}/export")
def export_stage(screening_id: str, stage: str, format: str = "xlsx") -> Response:
    screening = _screening_or_404(screening_id)
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=404, detail="Unknown stage")

    quals = load_qualifications(screening_id)
    rows = [row for row in load_rows(screening_id) if str(row["stage"]) == stage]
    slug = (screening["job_title"] or "screening").replace(" ", "_")[:40]

    if format == "csv":
        return Response(
            content=export.to_csv(screening, stage, quals, rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{slug}_stage{stage}.csv"'},
        )
    return Response(
        content=export.to_excel(screening, stage, quals, rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{slug}_stage{stage}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    question: str


@app.get("/api/screenings/{screening_id}/chat/{stage}")
def get_chat(screening_id: str, stage: str) -> dict[str, Any]:
    _screening_or_404(screening_id)
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=404, detail="Unknown stage")
    session = chat_session.get_or_create_session(screening_id, stage)
    _, provider_name, model = registry.active()
    return {
        "sessionId": session["id"],
        "mode": registry.chat_mode(provider_name, model),
        "provider": provider_name,
        "model": model,
        "messages": chat_session.transcript(session["id"]),
    }


@app.post("/api/screenings/{screening_id}/chat/{stage}")
async def post_chat(screening_id: str, stage: str, body: ChatMessage) -> dict[str, Any]:
    _screening_or_404(screening_id)
    if stage not in VALID_STAGES:
        raise HTTPException(status_code=404, detail="Unknown stage")
    session = chat_session.get_or_create_session(screening_id, stage)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, chat_session.ask, session["id"], body.question
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"sessionId": session["id"], **result}


@app.get("/api/chat/{session_id}/actions")
def chat_actions(session_id: str, since: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return {"actions": chat_session.pending_actions(session_id, since)}


# ---------------------------------------------------------------------------
# Bias audit
# ---------------------------------------------------------------------------

class AuditStart(BaseModel):
    screeningId: str
    # The audit page is where models get benchmarked against each other, so it
    # picks its own provider/model rather than inheriting whatever happens to
    # be active. Omitted means "use the active one".
    provider: str | None = None
    model: str | None = None
    corpus: str | None = None


@app.get("/api/audits/corpus")
def audit_corpus() -> dict[str, Any]:
    return {"root": str(audit.CORPUS_ROOT), "corpora": audit.describe_corpora()}


@app.post("/api/audits/corpus/{name}/screening")
async def screening_from_corpus(name: str) -> dict[str, Any]:
    """Build a screening out of a corpus, with no upload step.

    A corpus ships the position description its resumes were written against,
    so making someone drag those same files into a browser to get started is
    busywork that can also go wrong. This reads both straight off disk.
    """
    try:
        corpus_dir = audit.resolve_corpus(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    description = corpus_dir / "position-description.pdf"
    if not description.exists():
        raise HTTPException(
            status_code=400,
            detail=f"The '{name}' corpus has no position-description.pdf to build a screening from.",
        )

    provider, provider_name, model = registry.active()
    if not model:
        raise HTTPException(
            status_code=400, detail="No model is selected — choose one on the Configuration page."
        )

    text = extract_text_from_pdf(str(description))
    if not text.strip():
        raise HTTPException(status_code=400, detail="The position description had no readable text.")

    try:
        parsed = await asyncio.get_event_loop().run_in_executor(
            None, lambda: jd_parse.parse_qualifications(provider, text, model=model)
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    screening_id = db.new_id()
    db.execute(
        "INSERT INTO screening (id, job_title, jd_text, jd_filename, status, quals_confirmed, "
        "provider, model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            screening_id,
            parsed["job_title"] or f"{name} corpus",
            text,
            description.name,
            "draft",
            0,
            provider_name,
            model,
            db.now(),
        ),
    )
    _replace_qualifications(
        screening_id,
        [{"text": t, "kind": "required"} for t in parsed["required"]]
        + [{"text": t, "kind": "preferred"} for t in parsed["preferred"]],
    )

    loop = asyncio.get_event_loop()
    added = 0
    for path in audit.list_corpus(corpus_dir):
        resume_text = await loop.run_in_executor(None, extract_text_from_pdf, path)
        if not resume_text.strip():
            continue
        db.execute(
            "INSERT INTO candidate (id, screening_id, name, source_files, resume_text, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                db.new_id(),
                screening_id,
                display_name(path),
                json.dumps([os.path.basename(path)]),
                resume_text,
                db.now(),
            ),
        )
        added += 1

    return {
        "screeningId": screening_id,
        "jobTitle": parsed["job_title"],
        "qualifications": _qual_payload(screening_id),
        "candidates": added,
        "provider": provider_name,
        "model": model,
    }


@app.post("/api/audits")
async def start_audit(body: AuditStart) -> dict[str, Any]:
    screening = _screening_or_404(body.screeningId)
    quals = load_qualifications(body.screeningId)
    if not quals:
        raise HTTPException(
            status_code=400, detail="Pick a screening that has a confirmed qualification list"
        )

    if body.provider or body.model:
        provider_name = body.provider or db.get_config().get("provider")
        if provider_name not in registry.PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")
        model = body.model or ""
        provider = registry.get_provider(provider_name, model)
    else:
        provider, provider_name, model = registry.active()

    if not model:
        raise HTTPException(status_code=400, detail="Choose a model to run the audit with.")
    if not provider.is_configured():
        raise HTTPException(status_code=400, detail=f"{provider.label} is not configured")

    try:
        corpus_dir = audit.resolve_corpus(body.corpus)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not audit.load_pairs(corpus_dir)[0]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The '{corpus_dir.name}' corpus has no baseline/variant pairs, so a bias audit "
                "would have nothing to compare. Generate protected-class variants for it first."
            ),
        )

    audit_id = db.new_id()
    db.execute(
        "INSERT INTO audit_run (id, corpus, provider, model, status, created_at) VALUES (?,?,?,?,?,?)",
        (audit_id, corpus_dir.name, provider_name, model, "running", db.now()),
    )

    job_id = uuid.uuid4().hex[:12]
    new_job(job_id)
    asyncio.create_task(
        audit.run_audit(
            job_id, audit_id, screening["id"], provider, provider_name, model, corpus_dir
        )
    )
    return {
        "jobId": job_id,
        "auditId": audit_id,
        "provider": provider_name,
        "model": model,
        "corpus": corpus_dir.name,
    }


@app.get("/api/audits")
def list_audits() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT id, provider, model, status, passed, summary, created_at FROM audit_run "
        "ORDER BY created_at DESC, rowid DESC LIMIT 50"
    )
    for row in rows:
        summary = json.loads(row.pop("summary") or "{}")
        row["passed"] = None if row["passed"] is None else bool(row["passed"])
        row["stageFlips"] = summary.get("total_stage_flips")
        row["meanCoverageDelta"] = summary.get("mean_coverage_delta")
        row["pairs"] = summary.get("pairs_measured")
    return rows


@app.get("/api/audits/{audit_id}")
def get_audit(audit_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM audit_run WHERE id=?", (audit_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Audit run not found")
    done = row["status"] == "done"
    comparison = audit.build_comparison(row) if done else {}
    grouped = audit.build_grouped(row) if done else {}
    return {
        "id": row["id"],
        "provider": row["provider"],
        "model": row["model"],
        "corpus": row["corpus"],
        "status": row["status"],
        "error": row["error"],
        "createdAt": row["created_at"],
        "qualifications": comparison.get("qualifications", []),
        "comparisons": comparison.get("comparisons", []),
        "counts": comparison.get("summary", {}),
        "levels": grouped.get("levels", []),
    }


@app.delete("/api/audits/{audit_id}")
def delete_audit(audit_id: str) -> dict[str, Any]:
    """Delete a run and the hidden screening it created.

    The screening holding the audit's scored corpus is invisible in the UI, so
    without this it would accumulate a copy of the corpus per run with no way
    to reach it.
    """
    row = db.query_one("SELECT screening_id FROM audit_run WHERE id=?", (audit_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Audit run not found")

    if row["screening_id"]:
        db.execute(
            "DELETE FROM screening WHERE id=? AND kind='audit'", (row["screening_id"],)
        )
    db.execute("DELETE FROM audit_run WHERE id=?", (audit_id,))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Static frontend (production build)
# ---------------------------------------------------------------------------

@app.exception_handler(LLMError)
def _llm_error_handler(_request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


dist_dir = Path(__file__).resolve().parent.parent / "dist"
if dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")
