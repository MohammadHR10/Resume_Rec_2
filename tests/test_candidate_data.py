"""Applicant research: exact sources, arbitrary subsets and complete group counts."""
import json

import pytest

from backend import db
from backend.chat import app_tools, candidate_data as data, session, structured
from backend.pipeline import store_failure
from test_chat import screening, ScriptedProvider  # noqa: F401


@pytest.fixture
def research(screening):
    texts = {
        "Alice": "Alice resume. Eight years of Python. Built distributed services on Azure. C++ experience.",
        "Bob": "Bob resume. Four years building Python APIs. Worked with SQL Server on premises.",
        "Carol": "Carol resume. Eighteen months of PYTHON experience. Distributed services internship on AWS.",
    }
    for name, text in texts.items():
        db.execute("UPDATE candidate SET resume_text=?,source_files=? WHERE name=?",
                   (text, json.dumps([f"{name}-resume.pdf", f"{name}-cover-letter.pdf"]), name))
    return data.CandidateData(screening)


def condition(field, value, op="eq", **kw):
    return {"field": field, "op": op, "value": value, **kw}


def find(research, **kw):
    return research.run("find_candidates", kw)


def read(research, name, **kw):
    cid = next(r["id"] for r in research.rows if r["name"] == name)
    return research.run("read_candidate", {"candidate_id": cid, **kw})


def analyze(research, **kw):
    return research.run("analyze_candidates", kw)


def test_combines_all_any_and_none_for_a_job_relevant_subset(research):
    result = find(research, where={
        "all": [condition("resume", "python", "contains"), condition("preferred_met", 1, "gte")],
        "any": [condition("resume", "Azure", "contains"), condition("resume", "AWS", "contains")],
        "none": [condition("recommendation", "Pass")],
    })
    assert result["matched"] == 1
    assert result["candidates"][0]["name"] == "Carol"
    assert result["candidates"][0]["rank"] == 3  # Filtering must not renumber ranks.


def test_filters_individual_verdicts_and_read_returns_exact_stored_assessment(research):
    result = find(research, where={"all": [condition("verdict", "Partial", qualification="R2")]})
    assert [r["name"] for r in result["candidates"]] == ["Carol"]
    detail = read(research, "Carol", qualifications=["R2"])
    verdict = detail["verdicts"][0]
    assert verdict["qualification"]["text"] == "Three years of professional Python experience"
    assert verdict["verdict"] == "Partial"
    assert verdict["evidence"] == "Carol evidence 1"
    assert verdict["reasoning"] == ""  # No explanation was recorded; never fabricate one.
    assert detail["candidate"]["model"] == "test-model"


def test_filters_written_reasoning_and_missing_evidence(screening, research):
    carol = next(r for r in research.rows if r["name"] == "Carol")
    qid = research.qualification("R2")["id"]
    carol["verdicts"][qid]["reasoning"] = "Duration is below the three-year threshold."
    db.execute("UPDATE evaluation SET verdicts=? WHERE candidate_id=?", (json.dumps(carol["verdicts"]), carol["id"]))
    current = data.CandidateData(screening)
    result = find(current, where={"all": [condition("reasoning", "duration", "contains", qualification="R2")]})
    assert [r["name"] for r in result["candidates"]] == ["Carol"]
    assert find(current, where={"all": [{"field": "reasoning", "qualification": "R1", "op": "is_empty"}]})["matched"] == 3


def test_paginates_candidates_without_hiding_total_or_changing_selection(research):
    first = find(research, limit=1, sort_by="name", descending=True)
    second = find(research, limit=2, offset=first["next_offset"], sort_by="name", descending=True)
    assert first["total"] == second["total"] == 3
    assert [r["name"] for r in first["candidates"] + second["candidates"]] == ["Carol", "Bob", "Alice"]
    assert second["next_offset"] is None
    assert first["snapshot_id"] == second["snapshot_id"]


def test_duplicate_names_and_tied_ranks_remain_distinct(screening, research):
    alice, bob = (next(r for r in research.rows if r["name"] == name) for name in ("Alice", "Bob"))
    db.execute("UPDATE candidate SET name='Alice' WHERE id=?", (bob["id"],))
    db.execute("UPDATE evaluation SET preferred_met=1 WHERE candidate_id=?", (bob["id"],))
    current = data.CandidateData(screening)
    result = find(current, where={"all": [condition("name", "Alice"), condition("rank", 1)]})
    assert {r["id"] for r in result["candidates"]} == {alice["id"], bob["id"]}
    assert current.run("read_candidate", {"candidate_id": bob["id"], "section": "resume"})["text"].startswith("Bob resume.")


def test_resume_pages_reassemble_verbatim_source_and_identify_combined_files(research):
    first = read(research, "Alice", section="resume", text_limit=21)
    second = read(research, "Alice", section="resume", text_offset=first["next_text_offset"])
    original = next(r["resume"] for r in research.rows if r["name"] == "Alice")
    assert first["text"] + second["text"] == original
    assert second["next_text_offset"] is None
    assert len(first["candidate"]["source_files"]) == 2
    assert "boundaries were not retained" in first["source_note"]


def test_search_reads_beyond_first_page_and_preserves_unicode_offsets(screening, research):
    cid = next(r["id"] for r in research.rows if r["name"] == "Alice")
    source = "Résumé İ. " + "Background. " * 1200 + "Used C++ and PYTHON for a production API."
    db.execute("UPDATE candidate SET resume_text=? WHERE id=?", (source, cid))
    current = data.CandidateData(screening)
    first = read(current, "Alice", section="resume", terms=["C++", "python"], limit=1)
    second = read(current, "Alice", section="resume", terms=["C++", "python"], offset=first["next_offset"], limit=1)
    assert first["total"] == second["total"] == 2
    for passage in first["passages"] + second["passages"]:
        assert passage["quote"] == source[passage["start"]:passage["end"]]
        assert passage["match_start"] > 12000
    assert read(current, "Alice", section="resume", terms=[".*"])["total"] == 0  # Literal, not executable regex.


def test_long_reasoning_has_explicit_truncation_and_a_lossless_continuation(screening, research):
    row = next(r for r in research.rows if r["name"] == "Carol")
    explanation = "Recorded explanation. " * 150
    row["verdicts"][research.qualification("R2")["id"]]["reasoning"] = explanation
    db.execute("UPDATE evaluation SET verdicts=? WHERE candidate_id=?", (json.dumps(row["verdicts"]), row["id"]))
    current = data.CandidateData(screening)
    entry = read(current, "Carol", qualifications=["R2"])["verdicts"][0]
    assert entry["truncated_fields"] == ["reasoning"]
    tail = read(current, "Carol", section="reasoning", qualifications=["R2"], text_offset=1200)
    assert entry["reasoning"] + tail["text"] == explanation


def test_qualification_kinds_and_pages_preserve_full_breakdown_counts(research):
    required = read(research, "Alice", qualifications=["required"], limit=1)
    assert required["total"] == 2 and required["next_offset"] == 1
    preferred = read(research, "Alice", qualifications=["preferred"])
    assert preferred["verdicts"][0]["qualification"]["label"] == "P1"
    totals = analyze(research, qualifications=["R2"], member_limit=1)
    assert totals["overall"]["count"] == 3
    assert totals["groups"][0]["members_returned"] == 1 and totals["groups"][0]["members_truncated"]
    assert totals["overall"]["qualifications"][0]["counts"] == {"Meets": 2, "Partial": 1, "No": 0, "Missing": 0}
    assert totals["overall"]["qualifications"][0]["meets_rate"] == pytest.approx(2 / 3)


def test_overlapping_cohorts_compute_union_and_comparison_without_double_counting(research):
    result = analyze(research, cohorts=[
        {"label": "Preferred met", "where": {"all": [condition("preferred_met", 1)]}},
        {"label": "All required met", "where": {"all": [condition("required_unmet", 0)]}},
    ], limit=1, member_limit=0)
    assert result["union_count"] == 3
    assert result["overlap_count"] == 1 and result["unassigned_count"] == 0
    assert result["pairwise_overlaps"][0]["count"] == 1
    assert result["group_page"]["next_offset"] == 1
    assert result["groups"][0]["count"] == 2
    diffs = result["comparisons"][0]["differences"]
    assert diffs["mean_required_met"] == -0.5
    assert diffs["mean_preferred_met"] == 0.5
    assert diffs["pass_rate_evaluated"] == -0.5


def test_individual_cohorts_compare_candidates_without_name_key_collisions(research):
    people = find(research, where={"any": [condition("name", "Alice"), condition("name", "Bob")]})["candidates"]
    result = analyze(research, cohorts=[{"label": r["id"], "where": {"all": [condition("id", r["id"])]}} for r in people], qualifications=["P1"])
    assert result["comparisons"][0]["differences"]["mean_required_met"] == 0
    assert result["comparisons"][0]["differences"]["mean_preferred_met"] == 1
    assert result["groups"][0]["qualifications"][0]["counts"]["Meets"] == 1
    assert result["groups"][1]["qualifications"][0]["counts"]["No"] == 1


def test_batch_read_keeps_sources_and_verdicts_attached_to_exact_candidates(research):
    ids = [r["id"] for r in research.rows]
    result = research.run("read_candidate", {"candidate_ids": ids, "qualifications": ["P1"]})
    assert [r["candidate"]["id"] for r in result["candidates"]] == ids
    assert [r["verdicts"][0]["verdict"] for r in result["candidates"]] == ["Meets", "No", "Meets"]
    with pytest.raises(ValueError):
        research.run("read_candidate", {"candidate_ids": [ids[0], "foreign-id"]})


def test_groups_by_multiple_dimensions_and_verdicts_on_full_pool(research):
    result = analyze(research, group_by=[{"field": "stage"}, {"field": "verdict", "qualification": "P1"}], limit=1)
    assert result["matched"] == 3
    assert result["group_page"]["total"] == 2
    assert result["union_count"] == 3 and result["overlap_count"] == 0
    assert result["groups"][0]["dimensions"] == {"stage": "1", "verdict:P1": "Meets"}
    assert result["groups"][0]["count"] == 2


def test_missing_evaluations_and_errors_are_not_reported_as_negative_model_verdicts(screening, research):
    for cid in ("unevaluated", "error"):
        db.execute("INSERT INTO candidate (id,screening_id,name,resume_text,created_at) VALUES (?,?,?,?,?)", (cid, screening, cid, "Resume", db.now()))
    store_failure(screening, "error", "Evaluation could not complete", research.qualifications, "ulproxy", "test-model")
    current = data.CandidateData(screening)
    result = analyze(current, qualifications=["R2"])
    assert result["overall"]["evaluation_status"] == {"Evaluated": 3, "Error": 1, "Not evaluated": 1}
    assert result["overall"]["recommendations"] == {"Pass": 2, "Fail": 1, "Error": 1, "Not evaluated": 1}
    assert result["overall"]["coverage_denominator"] == 3
    assert result["overall"]["qualifications"][0]["counts"]["Missing"] == 2
    assert result["overall"]["qualifications"][0]["denominator"] == 5


def test_empty_cohort_reports_null_rates_and_zero_counts(research):
    result = analyze(research, cohorts=[{"label": "No matches", "where": {"all": [condition("stage", "3")]}}])
    assert result["groups"][0]["count"] == 0
    assert result["groups"][0]["mean_required_met"] is None
    assert result["groups"][0]["qualifications"][0]["meets_rate"] is None
    assert result["unassigned_count"] == 3


@pytest.mark.parametrize("bad", [
    condition("required_met", "2", "gte"), condition("resume", "", "contains"),
    condition("verdict", "Meets"), condition("stage", "4"),
    condition("recommendation", "yes"), condition("verdict", "Maybe", qualification="R1"),
    condition("name", "Alice", "gte"), condition("rank", True),
    {"field": "sql", "value": "SELECT * FROM config"},
    {"field": "resume", "op": "regex", "value": ".*"},
])
def test_invalid_filters_are_errors_not_silently_ignored(bad, research):
    with pytest.raises(ValueError):
        find(research, where={"all": [bad]})


def test_unknown_qualification_fails_even_when_other_filters_match_nobody(research):
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        find(research, where={"all": [condition("stage", "3"), condition("verdict", "No", qualification="R999")]})


def test_no_cross_screening_document_access_or_credential_fields(research):
    with pytest.raises(ValueError, match="unknown in this screening"):
        research.run("read_candidate", {"candidate_id": "foreign-id", "section": "resume"})
    with pytest.raises(ValueError):
        find(research, where={"all": [condition("api_key", "secret")]})
    with pytest.raises(ValueError):
        research.run("read_candidate", {"candidate_id": research.rows[0]["id"], "section": "resume", "screening_id": "foreign"})


def test_research_calls_remain_read_only_and_share_one_turn_snapshot(screening, research):
    agent = app_tools.AppTools(session.get_or_create_session(screening, "1"))
    before = db.query("SELECT * FROM evaluation ORDER BY id")
    first = json.loads(agent.run("find_candidates", {}))
    changed_id = first["candidates"][0]["id"]
    original = db.query_one("SELECT resume_text FROM candidate WHERE id=?", (changed_id,))["resume_text"]
    db.execute("UPDATE candidate SET resume_text='Changed between calls' WHERE id=?", (changed_id,))
    detail = json.loads(agent.run("read_candidate", {"candidate_id": changed_id, "section": "resume"}))
    assert detail["text"] == original
    assert detail["snapshot_id"] == first["snapshot_id"]
    assert db.query("SELECT * FROM evaluation ORDER BY id") == before
    assert app_tools.proposals(agent.session["id"]) == [] and agent.actions == []


def test_agent_dispatches_research_results_before_reply(monkeypatch, screening, research):
    provider = ScriptedProvider([
        {"tool_calls": [{"tool": "find_candidates", "args_json": json.dumps({"where": {"all": [condition("verdict", "Partial", qualification="R2")]}})}]},
        {"tool_calls": [{"tool": "reply", "args_json": '{"message":"Carol received Partial on R2."}'}]},
    ])
    monkeypatch.setattr(session.registry, "active", lambda: (provider, "ulproxy", "m"))
    chat = session.get_or_create_session(screening, "1")
    result = session.ask(chat["id"], "Who got Partial on R2?")
    assert result["answer"] == "Carol received Partial on R2."
    assert "Carol" in provider.seen[1][-1]["content"] and '"matched": 1' in provider.seen[1][-1]["content"]
    assert set(structured.TOOLS) & {"query_candidates", "explain_rank", "stage_stats"} == set()
