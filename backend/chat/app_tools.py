"""Explicit app capabilities. Models can propose writes; only the reviewer applies them.

Read and write handlers reuse the same application functions as the HTTP UI.
There is deliberately no HTTP, SQL, filesystem, shell or arbitrary-code tool.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import db
from . import candidate_data


Stage = Literal["1", "2", "3", "rejected"]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Args):
    pass


class Reply(Args):
    message: str = Field(min_length=1, max_length=24000)


class Provider(Args):
    provider: Literal["ulproxy", "fastllm"]


class Model(Provider):
    model: str = Field(min_length=1, max_length=200)


class StageArgs(Args):
    stage: Stage


class Candidates(StageArgs):
    candidateIds: list[str] = Field(min_length=1, max_length=500)


class Move(Candidates):
    action: Literal["promote", "reject", "restore"]
    note: str = Field(default="", max_length=2000)


class Selection(StageArgs):
    candidateIds: list[str] = Field(max_length=500)


class Evidence(StageArgs):
    candidateId: str
    qualification: str


class Export(StageArgs):
    format: Literal["xlsx", "csv"]


class Navigate(Args):
    page: Literal["screenings", "screening", "config", "audit"]
    screeningId: str | None = None
    stage: Stage = "1"


class Panel(Args):
    panel: Literal["checklist", "resumes", "history", "chat", "evidence"]
    open: bool


class Qualification(Args):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["required", "preferred"]


class Checklist(Args):
    qualifications: list[Qualification] = Field(min_length=1, max_length=100)
    confirmed: bool


class Title(Args):
    jobTitle: str = Field(max_length=300)


class AuditId(Args):
    auditId: str


class Corpus(Args):
    corpus: str


class Audit(Args):
    provider: Literal["ulproxy", "fastllm"] | None = None
    model: str | None = None
    corpus: str | None = None


class Thresholds(Args):
    max_stage_flips: int = Field(ge=0)
    max_mean_coverage_delta: float = Field(ge=0)


class Upload(Args):
    kind: Literal["position_description", "resumes"]


class Job(Args):
    jobId: str


# name: (argument model, effect, user-facing label, description)
TOOLS = {
    "reply": (Reply, "reply", "Reply", "Finish the turn with a grounded explanation or question. Call alone, after other tool results."),
    "get_app_state": (Empty, "read", "Screening state", "Current screening, checklist, candidate IDs, stages, model selection, and pending changes."),
    "list_screenings": (Empty, "read", "List screenings", "List available screenings and their IDs."),
    "get_promotion_history": (Empty, "read", "Promotion history", "Read the current screening's human decisions and override notes."),
    "list_models": (Provider, "read", "List models", "List models offered by a configured provider."),
    "test_provider": (Provider, "read", "Test connection", "Test a stored provider connection without revealing credentials."),
    "list_audits": (Empty, "read", "Audit history", "List bias audit runs."),
    "get_audit": (AuditId, "read", "Audit result", "Read a bias audit result."),
    "list_corpora": (Empty, "read", "List corpora", "List available benchmark corpora."),
    "get_job": (Job, "read", "Job progress", "Check evaluation or audit progress by job ID."),
    "navigate": (Navigate, "ui", "Navigate", "Open an app page or screening stage. Omitted screeningId means this screening."),
    "select_candidates": (Selection, "ui", "Select candidates", "Select exact candidate IDs in a stage; an empty list clears selection."),
    "show_evidence": (Evidence, "ui", "Open evidence", "Open a candidate's qualification evidence. Use qualification label R1/P1 or ID."),
    "export_stage": (Export, "ui", "Export stage", "Offer an Excel or CSV download for the whole stage, including filtered-out rows."),
    "set_panel": (Panel, "ui", "Show or hide panel", "Expand/collapse checklist, resumes, promotion history, chat; close evidence."),
    "upload_documents": (Upload, "ui", "Choose files", "Offer a file chooser. The reviewer selects local files; never supply a path. Resume uploads replace the candidate set."),
    "stage_candidates": (Move, "write", "Move candidates", "Propose promote/reject/restore for exact IDs in a stage. Requires reviewer approval; never advances automatically."),
    "save_qualifications": (Checklist, "write", "Save qualification checklist", "Propose the complete ordered checklist, adding/editing/removing/reordering/moving items. Changed items clear evaluations. Requires reviewer approval."),
    "evaluate_candidates": (Empty, "write", "Evaluate candidates", "Propose evaluation with the current model. Resets candidates to stage 1; requires a confirmed checklist."),
    "create_screening": (Title, "write", "Create screening", "Create a new screening after reviewer approval."),
    "delete_screening": (Empty, "write", "Delete this screening", "Propose deleting this screening and all candidates, evaluations and chat."),
    "configure_model": (Model, "write", "Change active model", "Propose provider/model selection for future evaluation and chat turns."),
    "configure_audit": (Thresholds, "write", "Change audit thresholds", "Propose limits for stage flips and mean coverage delta."),
    "start_audit": (Audit, "write", "Run bias audit", "Propose a bias audit using this screening's checklist and optional provider/model/corpus."),
    "delete_audit": (AuditId, "write", "Delete audit run", "Propose deleting a stored audit run."),
    "load_corpus": (Corpus, "write", "Create screening from corpus", "Propose importing a benchmark corpus as a new screening."),
}
TOOLS.update(candidate_data.TOOL_SPECS)


def descriptions() -> dict[str, dict[str, Any]]:
    return {
        name: {"description": f"{desc} Arguments: {json.dumps(model.model_json_schema())}"}
        for name, (model, _, _, desc) in TOOLS.items()
    }


def proposals(session_id: str) -> list[dict]:
    return [
        {**r, "args": json.loads(r["args"]), "result": json.loads(r["result"])}
        for r in db.query("SELECT id, tool, label, args, status, result FROM chat_proposal WHERE session_id=? ORDER BY rowid", (session_id,))
    ]


def fingerprint(screening_id: str, tool: str, args: dict) -> str:
    """Invalidate pending writes when the data the reviewer saw has changed."""
    from .. import server
    state: Any = {
        "screening": server.get_screening(screening_id),
        "candidates": server.load_rows(screening_id),
        "config": {k: v for k, v in db.get_config().items() if k != "connections"},
    }
    if tool == "delete_audit":
        state["audit"] = server.get_audit(args["auditId"])
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def validate_context(screening_id: str, tool: str, args: dict) -> None:
    from .. import server
    server._screening_or_404(screening_id)
    if tool in ("stage_candidates", "select_candidates", "show_evidence"):
        rows = server.get_stage(screening_id, args["stage"])["candidates"]
        known = {row["id"] for row in rows}
        requested = args.get("candidateIds", [args.get("candidateId")])
        if len(requested) != len(set(requested)) or any(i not in known for i in requested):
            raise ValueError("Every candidate ID must be unique and belong to the specified stage of this screening.")
    if tool == "stage_candidates":
        valid = {"1": {"promote", "reject"}, "2": {"promote", "reject"}, "3": {"reject"}, "rejected": {"restore"}}
        if args["action"] not in valid[args["stage"]]:
            raise ValueError("That action is unavailable in this stage.")
    if tool == "navigate" and args["screeningId"]:
        server._screening_or_404(args["screeningId"])
    if tool == "show_evidence":
        match = next((q for q in server._qual_payload(screening_id) if args["qualification"] in (q["id"], q["label"])), None)
        if not match:
            raise ValueError("Unknown qualification.")
        args["qualification"] = match["id"]
    if tool == "save_qualifications" and any(not q["text"].strip() for q in args["qualifications"]):
        raise ValueError("Qualification text cannot be blank.")
    if tool == "set_panel" and args["panel"] == "evidence" and args["open"]:
        raise ValueError("Use show_evidence with a candidate and qualification to open evidence.")


class AppTools:
    def __init__(self, session: dict, view: dict | None = None):
        self.session = session
        self.view = view
        self.actions: list[dict] = []
        self.candidate_data: candidate_data.CandidateData | None = None

    def run(self, tool: str, args: dict) -> str:
        from .. import server
        try:
            model, effect, label, _ = TOOLS[tool]
            args = model.model_validate(args).model_dump()
            sid = self.session["screening_id"]
            validate_context(sid, tool, args)
            if tool in candidate_data.TOOL_SPECS:
                if self.candidate_data is None:
                    self.candidate_data = candidate_data.CandidateData(sid)
                result = self.candidate_data.run(tool, args)
            elif effect == "write":
                if tool == "stage_candidates":
                    people = {r["id"]: r for r in server.get_stage(sid, args["stage"])["candidates"]}
                    names = ", ".join(f"{people[i]['name']} (rank {people[i]['rank']}, ID {i})" for i in args["candidateIds"])
                    overrides = sum(args["stage"] == "1" and
                                    ((args["action"] == "promote" and not people[i]["ai_pass"]) or
                                     (args["action"] == "reject" and people[i]["ai_pass"]))
                                    for i in args["candidateIds"])
                    label = f"{args['action'].capitalize()}: {names} (stage {args['stage']}; {overrides} AI overrides)"
                elif tool == "save_qualifications":
                    label += " — changed checklist items clear existing evaluations"
                elif tool == "evaluate_candidates":
                    label += " — replaces evaluations and resets every candidate to stage 1"
                elif tool == "delete_screening":
                    label += " — permanently removes its candidates, evaluations and chat"
                existing = db.query_one("SELECT id FROM chat_proposal WHERE session_id=? AND tool=? AND args=? AND status='pending'",
                                        (self.session["id"], tool, json.dumps(args)))
                if existing:
                    return json.dumps({"status": "awaiting_review", "proposalId": existing["id"], "note": "This proposal already awaits review."})
                proposal_id = db.new_id()
                db.execute(
                    "INSERT INTO chat_proposal (id,session_id,tool,label,args,fingerprint,status,result) VALUES (?,?,?,?,?,?,'pending','{}')",
                    (proposal_id, self.session["id"], tool, label, json.dumps(args), fingerprint(sid, tool, args)),
                )
                result = {"status": "awaiting_review", "proposalId": proposal_id, "label": label, "args": args,
                          "note": "Nothing has changed yet. The reviewer must apply or cancel this proposal in chat."}
            elif effect == "ui":
                action = {"type": "app", "tool": tool, "args": args, "screeningId": sid}
                self.actions.append(action)
                result = {"status": "queued", "action": action, "note": "The browser will handle this after the turn. Do not claim completion before then."}
            elif tool == "get_app_state":
                cfg = server.get_config()
                result = {**server.get_screening(sid), "stage": self.session["stage"],
                          "candidates": [{k: r[k] for k in ("id", "name", "stage", "rank", "ai_pass")} for r in server.load_rows(sid)],
                          "config": {k: cfg[k] for k in ("provider", "model", "auditThresholds")},
                          "proposals": proposals(self.session["id"]), "view": self.view}
            else:
                handlers = {
                    "list_screenings": lambda: server.list_screenings(),
                    "get_promotion_history": lambda: server.audit_trail(sid),
                    "list_models": lambda: server.list_models(args["provider"]),
                    "test_provider": lambda: server.test_provider(args["provider"]),
                    "list_audits": lambda: server.list_audits(),
                    "get_audit": lambda: server.get_audit(args["auditId"]),
                    "list_corpora": lambda: {"corpora": server.audit_corpus()["corpora"]},
                    "get_job": lambda: server.job_progress(args["jobId"], 0),
                }
                result = handlers[tool]()
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, KeyError, HTTPException) as exc:
            return json.dumps({"error": str(exc.detail) if isinstance(exc, HTTPException) else str(exc)})


async def resolve(session_id: str, proposal_id: str, approve: bool) -> dict:
    """Human-only endpoint: claim once before executing, including across tabs."""
    from .. import server
    session = db.query_one("SELECT * FROM chat_session WHERE id=?", (session_id,))
    row = db.query_one("SELECT * FROM chat_proposal WHERE id=? AND session_id=?", (proposal_id, session_id))
    if not session or not row:
        raise HTTPException(404, "Proposed change not found")
    args = json.loads(row["args"])
    with db.cursor() as cur:
        cur.execute("UPDATE chat_proposal SET status=? WHERE id=? AND status='pending'", ("applying" if approve else "cancelled", proposal_id))
        if cur.rowcount != 1:
            raise HTTPException(409, "This proposal has already been handled. Refresh the chat.")
    if not approve:
        return {"status": "cancelled"}
    sid, tool = session["screening_id"], row["tool"]
    try:
        if fingerprint(sid, tool, args) != row["fingerprint"]:
            raise HTTPException(409, "The screening or settings changed. Ask the assistant for a fresh proposal.")
        validate_context(sid, tool, args)
        handlers = {
            "stage_candidates": lambda: server.stage_action(sid, args["stage"], server.StageAction(**{k: args[k] for k in ("candidateIds", "action", "note")})),
            "save_qualifications": lambda: server.update_qualifications(sid, server.QualificationsUpdate(**args)),
            "evaluate_candidates": lambda: server.start_evaluation(sid),
            "create_screening": lambda: server.create_screening(server.ScreeningCreate(**args)),
            "delete_screening": lambda: server.delete_screening(sid),
            "configure_model": lambda: server.update_config(server.ConfigUpdate(**args)),
            "configure_audit": lambda: server.update_config(server.ConfigUpdate(auditThresholds=args)),
            "start_audit": lambda: server.start_audit(server.AuditStart(screeningId=sid, **args)),
            "delete_audit": lambda: server.delete_audit(args["auditId"]),
            "load_corpus": lambda: server.screening_from_corpus(args["corpus"]),
        }
        result = handlers[tool]()
        if inspect.isawaitable(result):
            result = await result
        # Store a safe result; configuration responses contain masked connection metadata.
        if tool.startswith("configure_"):
            result = {"provider": result["provider"], "model": result["model"], "auditThresholds": result["auditThresholds"]}
        db.execute("UPDATE chat_proposal SET status='applied',result=? WHERE id=?", (json.dumps(result), proposal_id))
        if tool != "delete_screening":
            server.chat_session._record(session_id, "assistant", f"Reviewer applied: {row['label']}.", "tools")
            for other in db.query("SELECT id FROM chat_session WHERE screening_id=?", (sid,)):
                server.chat_session._queue_actions(other["id"], [{
                    "type": "app", "tool": "refresh", "screeningId": sid,
                    "args": {"jobId": result.get("jobId")},
                }])
        return {"status": "applied", "tool": tool, "result": result}
    except Exception as exc:
        db.execute("UPDATE chat_proposal SET status='failed',result=? WHERE id=?", (json.dumps({"error": str(exc.detail) if isinstance(exc, HTTPException) else str(exc)}), proposal_id))
        raise
