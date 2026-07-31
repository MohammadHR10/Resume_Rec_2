"""The analysis tools both chat modes share, plus mode selection and degradation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from backend import db
from backend.chat import session as chat_session
from backend.chat import structured, workspace
from backend.llm.base import LLMError


@pytest.fixture
def screening(screening_id, quals):
    """Three candidates with hand-set verdicts, so every assertion is exact."""
    verdict_sets = [
        ("Alice", {0: "Meets", 1: "Meets", 2: "Meets"}),
        ("Bob", {0: "Meets", 1: "Meets", 2: "No"}),
        ("Carol", {0: "Meets", 1: "Partial", 2: "Meets"}),
    ]
    for name, verdicts in verdict_sets:
        candidate_id = db.new_id()
        db.execute(
            "INSERT INTO candidate (id, screening_id, name, source_files, resume_text, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (candidate_id, screening_id, name, "[]", f"{name} resume", db.now()),
        )
        mapped = {
            quals[index]["id"]: {"verdict": verdict, "evidence": f"{name} evidence {index}"}
            for index, verdict in verdicts.items()
        }
        required_met = sum(
            1 for i, v in verdicts.items() if v == "Meets" and quals[i]["kind"] == "required"
        )
        preferred_met = sum(
            1 for i, v in verdicts.items() if v == "Meets" and quals[i]["kind"] == "preferred"
        )
        db.execute(
            "INSERT INTO evaluation (id, screening_id, candidate_id, verdicts, required_met, "
            "required_total, preferred_met, preferred_total, ai_pass, summary, provider, model, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_id(),
                screening_id,
                candidate_id,
                json.dumps(mapped),
                required_met,
                2,
                preferred_met,
                1,
                1 if required_met == 2 else 0,
                f"{name} summary",
                "ulproxy",
                "test-model",
                db.now(),
            ),
        )
        db.execute(
            "INSERT INTO stage_state (candidate_id, screening_id, stage, updated_at) VALUES (?,?,?,?)",
            (candidate_id, screening_id, "1", db.now()),
        )
    return screening_id


@pytest.fixture
def ws(screening):
    return workspace.build_workspace("test-session", screening, "1")


def run_tool(ws_path, tool: str, *args) -> dict:
    completed = subprocess.run(
        [sys.executable, str(ws_path / "tools" / tool), "--workspace", str(ws_path), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.stdout, completed.stderr
    return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def test_workspace_contains_the_snapshot_tools_and_brief(ws):
    assert (ws / "snapshot.json").exists()
    assert (ws / "CHAT.md").exists()
    assert (ws / "tools" / "whatif.py").exists()
    assert (ws / "tools" / "_bootstrap.py").exists()

    snapshot = json.loads((ws / "snapshot.json").read_text(encoding="utf-8"))
    assert [q["label"] for q in snapshot["qualifications"]] == ["R1", "R2", "P1"]
    assert len(snapshot["candidates"]) == 3


def test_the_workspace_database_carries_no_credentials(screening):
    """The workspace is handed to a CLI agent, so the config table — which can
    hold a provider's bearer token — must not travel with it."""
    db.set_connection("fastllm", {"base_url": "https://gw.example", "api_key": "secret-token-42"})
    path = workspace.build_workspace("secrets-session", screening, "1")

    copied = path / "screening.db"
    assert copied.exists(), "the workspace should still get a database"

    connection = sqlite3.connect(str(copied))
    try:
        assert connection.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 0
        # The audit trail it exists for is still there.
        assert connection.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 3
    finally:
        connection.close()

    assert b"secret-token-42" not in copied.read_bytes()
    # The real database still has it.
    assert db.get_connection("fastllm")["api_key"] == "secret-token-42"


def test_the_brief_states_the_rules_the_model_must_not_invent(ws):
    brief = (ws / "CHAT.md").read_text(encoding="utf-8")
    assert "Partial` does not pass" in brief
    assert "computed **in code**" in brief
    assert "R1" in brief


# ---------------------------------------------------------------------------
# query_candidates
# ---------------------------------------------------------------------------

def test_query_candidates_lists_everyone_by_default(ws):
    result = run_tool(ws, "query_candidates.py")
    assert result["matched"] == 3
    assert {c["name"] for c in result["candidates"]} == {"Alice", "Bob", "Carol"}


def test_query_candidates_filters_by_verdict_on_a_qualification(ws):
    result = run_tool(ws, "query_candidates.py", "--qual", "P1", "--verdict", "Meets", "--evidence")
    assert {c["name"] for c in result["candidates"]} == {"Alice", "Carol"}
    assert result["candidates"][0]["evidence"].endswith("evidence 2")


def test_query_candidates_finds_people_by_the_rank_shown_in_the_grid(ws):
    """Users refer to candidates by grid rank — "why did 3, 4 and 6 fail?" —
    and one call must answer for all of them."""
    result = run_tool(ws, "query_candidates.py", "--rank", "1,3")
    assert result["matched"] == 2
    assert {c["name"] for c in result["candidates"]} == {"Alice", "Carol"}


def test_explain_rank_accepts_a_rank_as_well_as_a_name(ws):
    result = run_tool(ws, "explain_rank.py", "1", "3")
    assert result["ranked_higher"] == "Alice"


def test_query_candidates_filters_on_the_ai_recommendation(ws):
    result = run_tool(ws, "query_candidates.py", "--ai-pass", "no")
    assert [c["name"] for c in result["candidates"]] == ["Carol"]


def test_query_candidates_reports_an_unknown_qualification(ws):
    result = run_tool(ws, "query_candidates.py", "--qual", "R9")
    assert "error" in result
    assert any("R1" in known for known in result["known"])


# ---------------------------------------------------------------------------
# explain_rank
# ---------------------------------------------------------------------------

def test_explain_rank_names_the_deciding_factor(ws):
    result = run_tool(ws, "explain_rank.py", "Alice", "Bob")
    assert result["ranked_higher"] == "Alice"
    assert "preferred coverage" in result["decided_by"]
    assert len(result["differing_qualifications"]) == 1
    assert result["differing_qualifications"][0]["qualification"] == "P1"


def test_explain_rank_prefers_required_coverage_over_preferred(ws):
    result = run_tool(ws, "explain_rank.py", "Carol", "Bob")
    assert result["ranked_higher"] == "Bob"
    assert result["decided_by"] == "required coverage"


def test_explain_rank_reports_an_ambiguous_name(ws):
    result = run_tool(ws, "explain_rank.py", "Nobody", "Bob")
    assert "could not uniquely identify" in result["error"]


# ---------------------------------------------------------------------------
# stage_stats
# ---------------------------------------------------------------------------

def test_stage_stats_explains_why_candidates_pass(ws):
    result = run_tool(ws, "stage_stats.py", "--stage", "1")
    assert result["candidates"] == 3
    assert result["ai_pass"] == 2
    assert result["most_missed"][0]["qualification"] == "R2"
    assert result["most_missed"][0]["failure_rate"] == round(1 / 3, 3)
    r1 = next(q for q in result["per_qualification"] if q["qualification"] == "R1")
    assert r1["meets"] == 3 and r1["failure_rate"] == 0.0


# ---------------------------------------------------------------------------
# whatif — the deterministic recompute
# ---------------------------------------------------------------------------

def test_whatif_removing_a_required_qual_newly_passes_the_candidate_it_blocked(ws):
    result = run_tool(ws, "whatif.py", "--remove-qual", "R2")
    assert result["ai_pass_before"] == 2
    assert result["ai_pass_after"] == 3
    assert [c["name"] for c in result["newly_passing"]] == ["Carol"]
    assert result["newly_failing"] == []
    assert result["remaining_required"] == 1


def test_whatif_removing_a_preferred_qual_reorders_the_ranking(ws):
    result = run_tool(ws, "whatif.py", "--remove-qual", "P1")
    # Alice and Bob were split by P1; without it they tie and share rank 1.
    ranks = {row["name"]: row["rank"] for row in result["ranking_after"]}
    assert ranks["Alice"] == ranks["Bob"] == 1
    assert any(change["name"] == "Bob" for change in result["rank_changes"])


def test_whatif_accepts_text_as_well_as_labels(ws):
    result = run_tool(ws, "whatif.py", "--remove-qual", "professional Python")
    assert result["removed"][0]["label"] == "R2"


def test_whatif_refuses_to_empty_the_checklist(ws):
    result = run_tool(ws, "whatif.py", "--remove-qual", "R1", "R2", "P1")
    assert "empty" in result["error"]


def test_whatif_reports_an_unresolvable_qualification(ws):
    result = run_tool(ws, "whatif.py", "--remove-qual", "R7")
    assert "could not resolve" in result["error"]


def test_whatif_changes_nothing_in_the_database(ws, screening):
    run_tool(ws, "whatif.py", "--remove-qual", "R2")
    assert db.query_one("SELECT COUNT(*) AS n FROM qualification WHERE screening_id=?", (screening,))["n"] == 3
    assert db.query_one("SELECT SUM(ai_pass) AS n FROM evaluation")["n"] == 2


# ---------------------------------------------------------------------------
# grid_action
# ---------------------------------------------------------------------------

def test_grid_action_queues_a_sort_the_frontend_can_apply(ws):
    result = run_tool(ws, "grid_action.py", "--sort", "required:desc")
    assert result["queued"]["sort"] == {"column": "required", "direction": "desc"}
    queued = workspace.drain_actions("test-session")
    assert queued[0]["sort"]["column"] == "required"
    assert workspace.drain_actions("test-session") == []


def test_grid_action_resolves_a_qualification_label_to_its_column(ws):
    result = run_tool(ws, "grid_action.py", "--filter", "R1=Meets")
    snapshot = json.loads((ws / "snapshot.json").read_text(encoding="utf-8"))
    expected = f"qual:{snapshot['qualifications'][0]['id']}"
    assert result["queued"]["filters"][0] == {"column": expected, "value": "Meets"}


def test_grid_action_rejects_an_unknown_column(ws):
    result = run_tool(ws, "grid_action.py", "--sort", "vibes")
    assert "unknown sort column" in result["error"]


def test_grid_action_needs_something_to_do(ws):
    assert "nothing to do" in run_tool(ws, "grid_action.py")["error"]


# ---------------------------------------------------------------------------
# The structured loop
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """Replays a list of turn payloads; records the messages it was given."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def chat(self, messages, *, schema=None, model=None, system=None, schema_name="Response"):
        self.seen.append(messages)
        return self.turns.pop(0) if self.turns else {"tool_calls": [], "answer": "done"}


def test_structured_loop_runs_the_named_tool_and_feeds_the_result_back(ws):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"tool": "whatif", "args_json": '{"remove_qual": ["R2"]}'}], "answer": ""},
            {"tool_calls": [], "answer": "Carol would newly pass."},
        ]
    )
    result = structured.run_turn(provider, "m", ws, "brief", [], "what if we drop R2?")
    assert result["answer"] == "Carol would newly pass."
    assert result["trace"] == [{"round": 1, "tool": "whatif", "args": {"remove_qual": ["R2"]}}]
    # The second turn saw the real tool output, not a summary of it.
    fed_back = provider.seen[1][-1]["content"]
    assert "newly_passing" in fed_back and "Carol" in fed_back


def test_structured_loop_reports_an_unknown_tool_instead_of_crashing(ws):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"tool": "delete_everything", "args_json": "{}"}], "answer": ""},
            {"tool_calls": [], "answer": "I cannot do that."},
        ]
    )
    result = structured.run_turn(provider, "m", ws, "brief", [], "drop the database")
    assert result["answer"] == "I cannot do that."
    assert "unknown tool" in provider.seen[1][-1]["content"]


def test_structured_loop_recovers_from_one_unreadable_turn(ws):
    """A malformed turn costs a round, not the user's question."""

    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, schema=None, model=None, system=None, schema_name="Response"):
            self.calls += 1
            self.last = messages
            if self.calls == 1:
                raise LLMError("the model's reply was not valid JSON")
            return {"tool_calls": [], "answer": "Recovered."}

    provider = FlakyProvider()
    result = structured.run_turn(provider, "m", ws, "brief", [], "why did 3 fail?")
    assert result["answer"] == "Recovered."
    assert "could not be read" in provider.last[-1]["content"]


def test_structured_loop_gives_up_cleanly_when_every_turn_is_unreadable(ws):
    class BrokenProvider:
        def chat(self, *_args, **_kwargs):
            raise LLMError("the model's reply was not valid JSON")

    with pytest.raises(LLMError):
        structured.run_turn(BrokenProvider(), "m", ws, "brief", [], "why did 3 fail?")


def test_structured_loop_nudges_a_model_that_says_nothing(ws):
    provider = ScriptedProvider([{"tool_calls": [], "answer": ""}, {"tool_calls": [], "answer": "Here."}])
    assert structured.run_turn(provider, "m", ws, "brief", [], "hi")["answer"] == "Here."


def test_build_command_maps_every_argument_kind(ws):
    command = structured.build_command(ws, "query_candidates", {"stage": "1", "evidence": True, "limit": 5})
    assert "--stage" in command and "1" in command
    assert "--evidence" in command
    assert command[command.index("--limit") + 1] == "5"

    positional = structured.build_command(ws, "explain_rank", {"a": "Alice", "b": "Bob"})
    assert positional[-2:] == ["Alice", "Bob"]

    repeated = structured.build_command(ws, "grid_action", {"filter": ["R1=Meets", "R2=No"]})
    assert repeated.count("--filter") == 2


def test_build_command_ignores_arguments_a_tool_does_not_take(ws):
    command = structured.build_command(ws, "stage_stats", {"stage": "2", "sudo": "yes"})
    assert "sudo" not in command and "yes" not in command


# ---------------------------------------------------------------------------
# Mode selection and degradation
# ---------------------------------------------------------------------------

def test_the_adapters_spawn_a_resolved_path_not_a_bare_name(monkeypatch):
    """On Windows an npm-installed CLI is a .CMD shim: shutil.which finds it
    through PATHEXT, but spawning the bare name fails with "cannot find the
    file specified" because CreateProcess does not apply PATHEXT itself."""
    from backend.chat.adapters import claude_cli, codex_cli

    for module, name, resolved in (
        (codex_cli, "codex", r"C:\npm\codex.CMD"),
        (claude_cli, "claude", r"C:\bin\claude.EXE"),
    ):
        monkeypatch.setattr(module.shutil, "which", lambda n, r=resolved: r)
        spawned: dict = {}

        def fake_run(command, **kwargs):
            spawned["argv0"] = command[0]
            raise OSError("stopped before spawning")

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            module.run_turn(workspace=".", question="hi", model="m")
        assert spawned["argv0"] == resolved, f"{name} was spawned by bare name"


def test_an_adapter_that_is_not_installed_says_so(monkeypatch):
    from backend.chat.adapters import codex_cli

    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: None)
    assert codex_cli.available() is False
    with pytest.raises(RuntimeError, match="not installed or not on PATH"):
        codex_cli.run_turn(workspace=".", question="hi")


def test_a_failed_harness_turn_degrades_to_structured(monkeypatch, screening):
    provider = ScriptedProvider([{"tool_calls": [], "answer": "Structured answered instead."}])
    monkeypatch.setattr(chat_session.registry, "active", lambda: (provider, "ulproxy", "m"))
    monkeypatch.setattr(chat_session.registry, "chat_mode", lambda *_a: "harness")

    class BrokenAdapter:
        @staticmethod
        def run_turn(**_kwargs):
            raise RuntimeError("claude CLI is not installed in this container")

    monkeypatch.setattr(chat_session, "get_adapter", lambda _name: BrokenAdapter)

    session = chat_session.get_or_create_session(screening, "1")
    result = chat_session.ask(session["id"], "why is Alice first?")

    assert result["mode"] == "structured"
    assert "not installed" in result["degradedFrom"]
    assert result["answer"] == "Structured answered instead."


def test_a_successful_harness_turn_keeps_its_session_id(monkeypatch, screening):
    monkeypatch.setattr(chat_session.registry, "active", lambda: (object(), "ulproxy", "m"))
    monkeypatch.setattr(chat_session.registry, "chat_mode", lambda *_a: "harness")

    class Adapter:
        @staticmethod
        def run_turn(**kwargs):
            assert kwargs["workspace"]
            assert "Qualifications" in kwargs["brief"]
            return {"text": "Alice meets everything.", "harness_sid": "thread-123"}

    monkeypatch.setattr(chat_session, "get_adapter", lambda _name: Adapter)

    session = chat_session.get_or_create_session(screening, "1")
    result = chat_session.ask(session["id"], "why is Alice first?")

    assert result["mode"] == "harness"
    assert result["degradedFrom"] == ""
    stored = db.query_one("SELECT harness_sid FROM chat_session WHERE id=?", (session["id"],))
    assert stored["harness_sid"] == "thread-123"


def test_grid_actions_queued_by_a_turn_reach_the_action_endpoint(monkeypatch, screening):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"tool": "grid_action", "args_json": '{"sort": "required:desc"}'}], "answer": ""},
            {"tool_calls": [], "answer": "Sorted by required coverage."},
        ]
    )
    monkeypatch.setattr(chat_session.registry, "active", lambda: (provider, "ulproxy", "m"))
    monkeypatch.setattr(chat_session.registry, "chat_mode", lambda *_a: "structured")

    session = chat_session.get_or_create_session(screening, "1")
    result = chat_session.ask(session["id"], "sort by required")

    assert result["actions"][0]["sort"]["column"] == "required"
    pending = chat_session.pending_actions(session["id"])
    assert pending[0]["sort"]["direction"] == "desc"
    assert chat_session.pending_actions(session["id"], since=pending[0]["id"]) == []


def test_an_unknown_stage_is_refused():
    with pytest.raises(ValueError):
        chat_session.get_or_create_session("whatever", "9")
