"""Tool boundary, human approval and app-control integration regressions."""
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, server
from backend.chat import app_tools, session, structured
from backend.llm.base import LLMError
from test_chat import ScriptedProvider, screening, ws  # noqa: F401 — shared fixtures


def call(tool, **args):
    return {"tool_calls": [{"tool": tool, "args_json": json.dumps(args)}]}


@pytest.fixture
def agent(screening):
    return app_tools.AppTools(session.get_or_create_session(screening, "1"))


@pytest.fixture
def client():
    with TestClient(server.app) as client:
        yield client


def resolve(client, agent, proposal, approve=True):
    return client.post(f"/api/chat/{agent.session['id']}/proposals/{proposal['proposalId']}", json={"approve": approve})


def candidate_id(name):
    return db.query_one("SELECT id FROM candidate WHERE name=?", (name,))["id"]


def test_plain_answers_are_rejected_in_favor_of_reply_tool(ws):
    provider = ScriptedProvider([{"answer": "I promoted everyone."}, call("reply", message="No changes were made.")])
    result = structured.run_turn(provider, "m", ws, "brief", [], "hello")
    assert result["answer"] == "No changes were made."
    assert "Only tool_calls" in provider.seen[1][-1]["content"]


@pytest.mark.parametrize("bad", [
    {"tool_calls": [{"tool": "evaluate_candidates", "args_json": "broken"}]},
    {"tool_calls": [{"tool": "evaluate_candidates", "args_json": "[]"}]},
    call("evaluate_candidates", command="rm -rf"),
    {"tool_calls": [call("evaluate_candidates")["tool_calls"][0], call("reply", message="Done")["tool_calls"][0]]},
    call("approve_proposal", id="invented"),
])
def test_malformed_or_mixed_calls_never_execute_app_actions(ws, bad):
    executed = []
    provider = ScriptedProvider([bad, call("reply", message="Corrected.")])
    result = structured.run_turn(provider, "m", ws, "brief", [], "hi", lambda *a: executed.append(a))
    assert result["answer"] == "Corrected."
    assert executed == []


def test_never_falls_back_to_free_prose_after_the_round_limit(ws):
    provider = ScriptedProvider([{"answer": "done"}] * structured.MAX_ROUNDS)
    with pytest.raises(LLMError, match="tool-call limit"):
        structured.run_turn(provider, "m", ws, "brief", [], "hi")


def test_analysis_args_cannot_override_workspace_or_execute_a_changed_script(ws):
    assert "error" in json.loads(structured.run_tool(ws, "whatif", {"remove_qual": ["--workspace", "C:/"]}))
    assert "error" in json.loads(structured.run_tool(ws, "stage_stats", {"stage": "1", "workspace": "C:/"}))
    (ws / "tools").mkdir(exist_ok=True)
    (ws / "tools" / "stage_stats.py").write_text('raise RuntimeError("Do not execute me")', encoding="utf-8")
    result = json.loads(structured.run_tool(ws, "stage_stats", {"stage": "1"}))
    assert result["candidates"] == 3


def test_move_waits_for_human_then_applies_once_and_records_override(agent, client):
    cid = candidate_id("Carol")
    proposal = json.loads(agent.run("stage_candidates", {"stage": "1", "candidateIds": [cid], "action": "promote", "note": "Reviewed experience"}))
    assert proposal["status"] == "awaiting_review"
    assert "Carol" in proposal["label"] and "1 AI overrides" in proposal["label"]
    assert db.query_one("SELECT stage FROM stage_state WHERE candidate_id=?", (cid,))["stage"] == "1"
    applied = resolve(client, agent, proposal)
    assert applied.status_code == 200, applied.text
    assert db.query_one("SELECT stage FROM stage_state WHERE candidate_id=?", (cid,))["stage"] == "2"
    trail = server.audit_trail(agent.session["screening_id"])
    assert len(trail) == 1 and trail[0]["override"] == 1 and trail[0]["note"] == "Reviewed experience"
    assert resolve(client, agent, proposal).status_code == 409
    assert db.query_one("SELECT stage FROM stage_state WHERE candidate_id=?", (cid,))["stage"] == "2"


def test_cancel_persists_without_changing_data(agent, client):
    proposal = json.loads(agent.run("delete_screening", {}))
    assert resolve(client, agent, proposal, False).json()["status"] == "cancelled"
    assert server.get_screening(agent.session["screening_id"])
    assert resolve(client, agent, proposal).status_code == 409


def test_stale_proposal_is_rejected_without_changing_candidates(agent, client):
    cid = candidate_id("Alice")
    proposal = json.loads(agent.run("stage_candidates", {"stage": "1", "candidateIds": [cid], "action": "promote"}))
    db.execute("UPDATE stage_state SET stage='2' WHERE candidate_id=?", (cid,))
    result = resolve(client, agent, proposal)
    assert result.status_code == 409
    assert db.query_one("SELECT stage FROM stage_state WHERE candidate_id=?", (cid,))["stage"] == "2"
    assert server.audit_trail(agent.session["screening_id"]) == []


def test_proposal_is_bound_to_session(agent, client):
    proposal = json.loads(agent.run("delete_screening", {}))
    other = session.get_or_create_session(agent.session["screening_id"], "2")
    response = client.post(f"/api/chat/{other['id']}/proposals/{proposal['proposalId']}", json={"approve": True})
    assert response.status_code == 404
    assert server.get_screening(agent.session["screening_id"])


@pytest.mark.parametrize("args", [
    {"stage": "2", "candidateIds": ["unknown"], "action": "promote"},
    {"stage": "1", "candidateIds": [], "action": "promote"},
    {"stage": "1", "candidateIds": ["unknown"], "action": "restore"},
])
def test_invalid_moves_create_no_proposal(agent, args):
    assert "error" in json.loads(agent.run("stage_candidates", args))
    assert app_tools.proposals(agent.session["id"]) == []


def test_repeated_proposal_creates_only_one_review_card(agent):
    first = json.loads(agent.run("create_screening", {"jobTitle": "Another position"}))
    second = json.loads(agent.run("create_screening", {"jobTitle": "Another position"}))
    assert first["proposalId"] == second["proposalId"]
    assert len(app_tools.proposals(agent.session["id"])) == 1


def test_state_and_model_changes_never_reveal_provider_tokens(agent, client):
    db.set_connection("ulproxy", {"api_key": "highly-secret-token"})
    assert "highly-secret-token" not in agent.run("get_app_state", {})
    assert "apiKey" not in agent.run("get_app_state", {})
    proposal = json.loads(agent.run("configure_model", {"provider": "fastllm", "model": "m"}))
    assert db.get_config()["provider"] != "fastllm"
    result = resolve(client, agent, proposal)
    assert result.status_code == 200
    assert db.get_config()["provider"] == "fastllm"
    assert "token" not in result.text


def test_unchanged_checklist_keeps_ids_and_verdicts(agent, client):
    sid = agent.session["screening_id"]
    before = server.get_qualifications(sid)
    proposal = json.loads(agent.run("save_qualifications", {
        "qualifications": [{"text": q["text"], "kind": q["kind"]} for q in before["qualifications"]], "confirmed": True}))
    assert resolve(client, agent, proposal).status_code == 200
    assert server.get_qualifications(sid) == before
    assert len(db.query("SELECT * FROM evaluation WHERE screening_id=?", (sid,))) == 3


def test_tool_actions_have_same_ids_in_response_and_polling(monkeypatch, agent):
    provider = ScriptedProvider([call("select_candidates", stage="1", candidateIds=[candidate_id("Bob")]), call("reply", message="Selection queued.")])
    monkeypatch.setattr(session.registry, "active", lambda: (provider, "ulproxy", "m"))
    result = session.ask(agent.session["id"], "select Bob")
    assert result["actions"] == session.pending_actions(agent.session["id"])
    assert result["actions"][0]["id"] > 0
    assert result["actions"][0]["args"]["candidateIds"] == [candidate_id("Bob")]


def test_failed_turn_keeps_proposal_available_for_review(monkeypatch, agent, client):
    provider = ScriptedProvider([call("create_screening", jobTitle="Review me")] + [{"answer": "bad"}] * structured.MAX_ROUNDS)
    monkeypatch.setattr(session.registry, "active", lambda: (provider, "ulproxy", "m"))
    with pytest.raises(LLMError):
        session.ask(agent.session["id"], "new screening")
    payload = client.get(f"/api/screenings/{agent.session['screening_id']}/chat/1").json()
    assert len(payload["proposals"]) == 1
    assert payload["proposals"][0]["status"] == "pending"


def test_cli_mode_cannot_be_reenabled_from_configuration(client):
    assert client.put("/api/config", json={"chatMode": "harness"}).status_code == 400


def test_stale_bulk_selection_does_not_partially_move_anyone(agent, client):
    alice, bob = candidate_id("Alice"), candidate_id("Bob")
    db.execute("UPDATE stage_state SET stage='2' WHERE candidate_id=?", (bob,))
    result = client.post(f"/api/screenings/{agent.session['screening_id']}/stages/1/actions",
                         json={"candidateIds": [alice, bob], "action": "promote"})
    assert result.status_code == 409
    assert db.query_one("SELECT stage FROM stage_state WHERE candidate_id=?", (alice,))["stage"] == "1"
    assert server.audit_trail(agent.session["screening_id"]) == []


def test_app_state_exposes_real_view_selection(agent):
    agent.view = {"selectedCandidateIds": [candidate_id("Bob")], "visibleCandidateIds": [candidate_id("Bob")]}
    state = json.loads(agent.run("get_app_state", {}))
    assert state["view"]["selectedCandidateIds"] == [candidate_id("Bob")]


def test_invalid_stage_action_cannot_be_proposed_for_a_real_candidate(agent):
    result = json.loads(agent.run("stage_candidates", {"stage": "1", "candidateIds": [candidate_id("Alice")], "action": "restore"}))
    assert "error" in result
    assert app_tools.proposals(agent.session["id"]) == []


def test_navigation_and_grid_actions_preserve_tool_call_order(monkeypatch, agent):
    provider = ScriptedProvider([call("navigate", page="screening", stage="2"),
                                 call("grid_action", sort="name:desc"), call("reply", message="Queued.")])
    monkeypatch.setattr(session.registry, "active", lambda: (provider, "ulproxy", "m"))
    result = session.ask(agent.session["id"], "open stage 2 and sort")
    assert result["actions"][0]["tool"] == "navigate"
    assert result["actions"][1]["type"] == "grid"
    assert result["actions"][0]["id"] < result["actions"][1]["id"]


def test_approving_in_one_stage_refreshes_other_open_stages(agent, client):
    other = session.get_or_create_session(agent.session["screening_id"], "2")
    proposal = json.loads(agent.run("stage_candidates", {"stage": "1", "candidateIds": [candidate_id("Alice")], "action": "promote"}))
    assert resolve(client, agent, proposal).status_code == 200
    for sid in (agent.session["id"], other["id"]):
        assert session.pending_actions(sid)[-1]["tool"] == "refresh"


def test_overlapping_turns_are_rejected_and_lock_is_released(monkeypatch, agent):
    monkeypatch.setattr(session.registry, "active", lambda: (object(), "ulproxy", "m"))
    def turn(*_args, **_kwargs):
        with pytest.raises(ValueError, match="already running"):
            session.ask(agent.session["id"], "second question")
        raise LLMError("test failure")
    monkeypatch.setattr(structured, "run_turn", turn)
    with pytest.raises(LLMError):
        session.ask(agent.session["id"], "first question")
    monkeypatch.setattr(structured, "run_turn", lambda *_a, **_kw: {"answer": "Recovered", "trace": []})
    assert session.ask(agent.session["id"], "retry")["answer"] == "Recovered"
