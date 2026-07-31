"""Rollups, ranking, grouping and verdict parsing — the code the model does not touch."""

from __future__ import annotations

import pytest

from backend import pipeline
from backend.llm.base import LLMError


class FakeProvider:
    """Returns a canned structured_extract payload and records the prompt."""

    label = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""
        self.schema = None

    def structured_extract(self, prompt, schema, *, model=None, system=None, schema_name="Response"):
        self.prompt = prompt
        self.schema = schema
        return self.payload


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_candidate_key_groups_ats_style_resume_and_cover_letter():
    assert pipeline.candidate_key("Reed, Martin Resume.pdf") == "Reed, Martin"
    assert pipeline.candidate_key("Reed, Martin CL Proj Manage UL.pdf") == "Reed, Martin"


def test_candidate_key_handles_a_multi_word_surname():
    """The comma marks where the surname ends, and it is not always on the
    first token — keying on token[0] split these people one row per document."""
    assert (
        pipeline.candidate_key("Abdul Kadhar, Nashwa Resume.pdf")
        == pipeline.candidate_key("Abdul Kadhar, Nashwa CL ML UL.pdf")
        == "Abdul Kadhar, Nashwa"
    )
    assert (
        pipeline.candidate_key("Van Der Berg, Anna Resume.pdf") == "Van Der Berg, Anna"
    )


def test_candidate_key_drops_middle_names_and_document_labels():
    assert (
        pipeline.candidate_key("Lee, Jordan James Resume.pdf")
        == pipeline.candidate_key("Lee, Jordan CL.pdf")
        == "Lee, Jordan"
    )


def test_a_zip_of_resumes_and_cover_letters_groups_one_row_per_person():
    files = [
        f"/tmp/{name}"
        for person in ("Abdul Kadhar, Nashwa", "Agbaetuo, Chinwendu", "Amin, Bhargav")
        for name in (f"{person} Resume.pdf", f"{person} CL ML UL.pdf")
    ]
    groups = pipeline.group_files(files)
    assert len(groups) == 3, "each person is one candidate, not one per document"
    assert all(len(paths) == 2 for paths in groups.values())


def test_candidate_key_keeps_whole_stem_when_there_is_no_comma_convention():
    # Truncating these to two tokens would collide every audit-corpus variant.
    assert pipeline.candidate_key("Resume_31_(BG1_R1)Ayaan_Rahman.pdf") == "Resume_31_(BG1_R1)Ayaan_Rahman"
    assert pipeline.candidate_key("Resume_1_Ayaan_Rahman.pdf") != pipeline.candidate_key(
        "Resume_2_Jordan_Lee.pdf"
    )


def test_display_name_strips_numbering_and_audit_codes():
    assert pipeline.display_name("Resume_31_(BG1_R1)Ayaan_Rahman.pdf") == "Ayaan Rahman"
    assert pipeline.display_name("Resume_4(senior)_Zayan_Rahman.pdf") == "Zayan Rahman"


def test_group_files_puts_one_candidates_documents_together():
    groups = pipeline.group_files(
        ["/tmp/Reed, Martin Resume.pdf", "/tmp/Reed, Martin CL.pdf", "/tmp/Resume_9_Miles.pdf"]
    )
    assert len(groups) == 2
    assert len(groups["Reed, Martin"]) == 2


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

def _quals():
    return [
        {"id": "r1", "text": "Degree", "kind": "required"},
        {"id": "r2", "text": "Python", "kind": "required"},
        {"id": "p1", "text": "Distributed systems", "kind": "preferred"},
    ]


def test_rollups_count_only_meets():
    verdicts = {
        "r1": {"verdict": "Meets"},
        "r2": {"verdict": "Partial"},
        "p1": {"verdict": "Meets"},
    }
    rollups = pipeline.compute_rollups(verdicts, _quals())
    assert rollups == {
        "required_met": 1,
        "required_total": 2,
        "preferred_met": 1,
        "preferred_total": 1,
        "ai_pass": False,
    }


def test_partial_does_not_pass_stage_one():
    verdicts = {"r1": {"verdict": "Meets"}, "r2": {"verdict": "Partial"}}
    assert pipeline.compute_rollups(verdicts, _quals())["ai_pass"] is False


def test_all_required_meets_passes():
    verdicts = {"r1": {"verdict": "Meets"}, "r2": {"verdict": "Meets"}}
    assert pipeline.compute_rollups(verdicts, _quals())["ai_pass"] is True


def test_missing_verdict_counts_as_unmet_not_as_a_smaller_denominator():
    rollups = pipeline.compute_rollups({"r1": {"verdict": "Meets"}}, _quals())
    assert rollups["required_total"] == 2
    assert rollups["required_met"] == 1
    assert rollups["ai_pass"] is False


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_ranking_orders_by_required_then_preferred_then_name():
    rows = [
        {"name": "Carol", "required_met": 2, "preferred_met": 0},
        {"name": "Alice", "required_met": 3, "preferred_met": 1},
        {"name": "Bob", "required_met": 3, "preferred_met": 2},
    ]
    ranked = pipeline.rank_candidates(rows)
    assert [r["name"] for r in ranked] == ["Bob", "Alice", "Carol"]
    assert [r["rank"] for r in ranked] == [1, 2, 3]


def test_tied_candidates_share_a_rank():
    rows = [
        {"name": "Alice", "required_met": 2, "preferred_met": 1},
        {"name": "Bob", "required_met": 2, "preferred_met": 1},
        {"name": "Carol", "required_met": 1, "preferred_met": 0},
    ]
    ranked = pipeline.rank_candidates(rows)
    assert [r["rank"] for r in ranked] == [1, 1, 3]


# ---------------------------------------------------------------------------
# Prompting and verdict parsing
# ---------------------------------------------------------------------------

def test_build_checklist_labels_required_and_preferred_separately():
    checklist, label_to_id, labels = pipeline.build_checklist(_quals())
    assert labels == ["R1", "R2", "P1"]
    assert label_to_id["R2"] == "r2"
    assert "[R1] Degree" in checklist
    assert "Preferred qualifications:" in checklist


def test_evaluate_resume_maps_labels_back_to_qualification_ids():
    provider = FakeProvider(
        {
            "candidate_name": "Ada Lovelace",
            "summary": "Backend engineer.",
            "verdicts": [
                {"qual_id": "R1", "verdict": "Meets", "evidence": "BS Computer Science"},
                {"qual_id": "R2", "verdict": "no", "evidence": ""},
                {"qual_id": "P1", "verdict": "Partial", "evidence": "Some Kafka"},
            ],
        }
    )
    result = pipeline.evaluate_resume(provider, "m", "resume text", _quals())
    assert result["candidate_name"] == "Ada Lovelace"
    assert result["verdicts"]["r1"]["verdict"] == "Meets"
    assert result["verdicts"]["r1"]["evidence"] == "BS Computer Science"
    # Case is normalized rather than dropped.
    assert result["verdicts"]["r2"]["verdict"] == "No"
    assert result["verdicts"]["p1"]["verdict"] == "Partial"


def test_evaluate_resume_drops_unknown_labels_and_bad_verdicts():
    provider = FakeProvider(
        {
            "candidate_name": "",
            "summary": "",
            "verdicts": [
                {"qual_id": "R9", "verdict": "Meets", "evidence": "invented"},
                {"qual_id": "R1", "verdict": "Excellent", "evidence": "not a verdict"},
                {"qual_id": "R2", "verdict": "Meets", "evidence": "ok"},
            ],
        }
    )
    result = pipeline.evaluate_resume(provider, "m", "resume", _quals())
    assert set(result["verdicts"]) == {"r2"}


def test_evaluate_resume_refuses_an_empty_checklist():
    with pytest.raises(LLMError):
        pipeline.evaluate_resume(FakeProvider({}), "m", "resume", [])


def test_evaluation_schema_constrains_ids_and_verdicts():
    schema = pipeline.evaluation_schema(["R1", "P1"])
    item = schema["properties"]["verdicts"]["items"]["properties"]
    assert item["qual_id"]["enum"] == ["R1", "P1"]
    assert item["verdict"]["enum"] == ["Meets", "Partial", "No"]
    # Reviewers override the AI at the gate, so they need its argument, not
    # just its conclusion and a quote.
    assert "reasoning" in item


def test_evaluate_resume_captures_the_reasoning_behind_each_verdict():
    provider = FakeProvider(
        {
            "candidate_name": "Ada",
            "summary": "",
            "verdicts": [
                {
                    "qual_id": "R2",
                    "verdict": "Partial",
                    "evidence": "2 years of Python",
                    "reasoning": "The resume shows two years against a three-year requirement.",
                }
            ],
        }
    )
    result = pipeline.evaluate_resume(provider, "m", "resume", _quals())
    assert result["verdicts"]["r2"]["reasoning"].startswith("The resume shows two years")


def test_a_verdict_without_reasoning_is_still_accepted():
    """Screenings scored before reasoning was captured must keep working."""
    provider = FakeProvider(
        {
            "candidate_name": "",
            "summary": "",
            "verdicts": [{"qual_id": "R1", "verdict": "Meets", "evidence": "BS CS"}],
        }
    )
    result = pipeline.evaluate_resume(provider, "m", "resume", _quals())
    assert result["verdicts"]["r1"]["reasoning"] == ""
