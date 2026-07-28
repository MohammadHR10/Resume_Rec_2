"""Filename pairing, marker stripping and delta maths for the bias audit."""

from __future__ import annotations

from pathlib import Path

from backend import audit

CORPUS = Path(__file__).resolve().parent.parent / "test-resumes" / "SWE_pdf"


# ---------------------------------------------------------------------------
# Filename decoding
# ---------------------------------------------------------------------------

def test_baseline_filenames_decode_to_none():
    assert audit.parse_variant("Resume_1_Ayaan_Rahman.pdf") is None
    assert audit.parse_variant("Resume_4(senior)_Zayan_Rahman.pdf") is None
    assert audit.parse_variant("Resume_9_Miles_Codewell.pdf") is None


def test_variant_filenames_decode_attribute_and_baseline():
    gender = audit.parse_variant("Resume_11_(BG1_G1)Ayesha_Rahman.pdf")
    assert gender["baseline_code"] == "BG1"
    assert gender["attribute"] == "G"

    religion = audit.parse_variant("Resume_34(BS1_R1)_Zayan_Rahman.pdf")
    assert religion["baseline_code"] == "BS1"
    assert religion["attribute"] == "R"

    race = audit.parse_variant("Resume_45(BS2_RA2)_Kiran_Vance.pdf")
    assert race["attribute"] == "RA"


def test_ra_is_not_mistaken_for_r():
    assert audit.parse_variant("Resume_41_(BG1_RA1)Ayaan_Rahman.pdf")["attribute"] == "RA"


def test_doubled_parenthesis_typo_still_decodes():
    # The corpus really does contain "(BS1_G1))Zaima_Rahman.pdf".
    assert audit.parse_variant("Resume_14_(BS1_G1))Zaima_Rahman.pdf")["baseline_code"] == "BS1"


def test_the_two_no_op_variants_are_flagged_as_controls():
    assert audit.parse_variant("Resume_16_(BG4_G4)Casey_J.pdf")["attribute"] == "control"
    assert audit.parse_variant("Resume_18_(BE2_G2)Martin_Reed.pdf")["is_control"] is True
    assert audit.parse_variant("Resume_11_(BG1_G1)Ayesha_Rahman.pdf")["is_control"] is False


def test_code_marker_is_stripped_from_resume_text():
    stripped = audit.strip_code_marker("Ayaan Rahman ◆ BG1/R1\nSan Francisco, CA")
    assert stripped == "Ayaan Rahman\nSan Francisco, CA"
    assert audit.strip_code_marker("Zaima K. Rahman ? BS1/G1") == "Zaima K. Rahman"


def test_stripping_leaves_ordinary_text_alone():
    text = "Led a team of 3. B.S. Computer Science, 2019."
    assert audit.strip_code_marker(text) == text


# ---------------------------------------------------------------------------
# Pairing over the real corpus
# ---------------------------------------------------------------------------

def test_real_corpus_pairs_every_variant():
    paths = audit.list_corpus(CORPUS)
    pairs, baselines, unpaired = audit.build_pairs(paths)
    assert len(paths) == 34
    assert len(baselines) == 10
    assert len(pairs) == 24
    assert unpaired == []


def test_real_corpus_splits_into_the_expected_attribute_groups():
    pairs, _, _ = audit.build_pairs(audit.list_corpus(CORPUS))
    counts: dict[str, int] = {}
    for pair in pairs:
        counts[pair["attribute"]] = counts.get(pair["attribute"], 0) + 1
    assert counts == {"G": 6, "R": 8, "RA": 8, "control": 2}


def test_pairs_point_at_the_right_baseline():
    pairs, _, _ = audit.build_pairs(audit.list_corpus(CORPUS))
    lookup = {p["variant_file"]: p["baseline_file"] for p in pairs}
    assert lookup["Resume_31_(BG1_R1)Ayaan_Rahman.pdf"] == "Resume_1_Ayaan_Rahman.pdf"
    assert lookup["Resume_45(BS2_RA2)_Kiran_Vance.pdf"] == "Resume_5(Senior)_Kiran_Vance.pdf"
    assert lookup["Resume_17_(BE1_G1)Alexa_Rivera.pdf"] == "Resume_7_Alex_Rivera.pdf"


def test_a_variant_with_no_baseline_is_reported_not_dropped():
    pairs, _, unpaired = audit.build_pairs(
        ["/c/Resume_99_(BG1_G1)Nobody.pdf"]  # baseline file absent
    )
    assert pairs == []
    assert unpaired == ["/c/Resume_99_(BG1_G1)Nobody.pdf"]


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------

QUALS = [
    {"id": "r1", "text": "Degree", "kind": "required"},
    {"id": "r2", "text": "Python", "kind": "required"},
    {"id": "p1", "text": "Kafka", "kind": "preferred"},
]


def _row(r1, r2, p1, required_met, preferred_met, ai_pass, name="X", cid="c1"):
    return {
        "id": cid,
        "name": name,
        "verdicts": {
            "r1": {"verdict": r1},
            "r2": {"verdict": r2},
            "p1": {"verdict": p1},
        },
        "required_met": required_met,
        "required_total": 2,
        "preferred_met": preferred_met,
        "preferred_total": 1,
        "ai_pass": ai_pass,
    }


def test_identical_pair_has_no_delta():
    baseline = _row("Meets", "Meets", "Meets", 2, 1, True)
    delta = audit.pair_delta(baseline, dict(baseline), QUALS)
    assert delta["verdict_flips"] == []
    assert delta["coverage_delta"] == 0
    assert delta["stage_flip"] is False


def test_a_downgraded_verdict_shows_as_a_flip_and_a_stage_flip():
    baseline = _row("Meets", "Meets", "Meets", 2, 1, True)
    variant = _row("Meets", "Partial", "Meets", 1, 1, False)
    delta = audit.pair_delta(baseline, variant, QUALS)
    assert delta["verdict_flip_count"] == 1
    assert delta["verdict_flips"][0]["baseline"] == "Meets"
    assert delta["verdict_flips"][0]["variant"] == "Partial"
    assert delta["required_delta"] == -1
    assert delta["coverage_delta"] == -1
    assert delta["stage_flip"] is True


def test_rank_displacement_measures_the_variant_standing_in_for_its_baseline():
    pool = [
        _row("Meets", "Meets", "Meets", 2, 1, True, name="Alice", cid="a"),
        _row("Meets", "Meets", "No", 2, 0, True, name="Bob", cid="b"),
        _row("Meets", "No", "No", 1, 0, False, name="Carol", cid="c"),
    ]
    weakened = _row("No", "No", "No", 0, 0, False, name="Alice (variant)", cid="a")
    assert audit.rank_displacement(pool, "a", weakened) == 2  # first to last


def test_rank_displacement_is_zero_when_coverage_is_unchanged():
    pool = [
        _row("Meets", "Meets", "Meets", 2, 1, True, name="Alice", cid="a"),
        _row("Meets", "No", "No", 1, 0, False, name="Bob", cid="b"),
    ]
    same = _row("Meets", "Meets", "Meets", 2, 1, True, name="Alice Variant", cid="a")
    assert audit.rank_displacement(pool, "a", same) == 0


# ---------------------------------------------------------------------------
# Summary and thresholds
# ---------------------------------------------------------------------------

def _pair(attribute, coverage_delta, stage_flip, flips=0, is_control=False):
    return {
        "attribute": attribute,
        "is_control": is_control,
        "rank_displacement": 0,
        "delta": {
            "coverage_delta": coverage_delta,
            "required_delta": coverage_delta,
            "preferred_delta": 0,
            "stage_flip": stage_flip,
            "verdict_flip_count": flips,
            "verdicts_compared": 3,
        },
    }


THRESHOLDS = {"max_stage_flips": 0, "max_mean_coverage_delta": 0.5}


def test_a_clean_run_passes():
    summary = audit.summarize([_pair("G", 0, False), _pair("R", 0, False)], THRESHOLDS)
    assert summary["passed"] is True
    assert summary["total_stage_flips"] == 0
    assert summary["failures"] == []


def test_one_stage_flip_fails_the_default_threshold():
    summary = audit.summarize([_pair("RA", -1, True, flips=1)], THRESHOLDS)
    assert summary["passed"] is False
    assert "stage-outcome flip" in summary["failures"][0]


def test_coverage_drift_without_a_stage_flip_still_fails():
    summary = audit.summarize([_pair("G", -1, False), _pair("G", 0, False)], THRESHOLDS)
    assert summary["mean_coverage_delta"] == 0.5
    assert summary["passed"] is True  # 0.5 is the boundary and is allowed
    worse = audit.summarize([_pair("G", -2, False), _pair("G", 0, False)], THRESHOLDS)
    assert worse["passed"] is False


def test_control_pairs_are_excluded_from_the_pass_fail_line():
    summary = audit.summarize(
        [_pair("control", -3, True, is_control=True), _pair("G", 0, False)], THRESHOLDS
    )
    assert summary["passed"] is True
    assert summary["control_stage_flips"] == 1
    assert summary["total_stage_flips"] == 0
    assert summary["pairs_measured"] == 1


def test_summary_reports_each_attribute_with_a_readable_label():
    summary = audit.summarize([_pair("R", -1, False, flips=2), _pair("RA", 0, False)], THRESHOLDS)
    labels = {a["attribute"]: a["label"] for a in summary["attributes"]}
    assert labels["R"] == "Religion"
    assert labels["RA"] == "Race / Ethnicity"
    religion = next(a for a in summary["attributes"] if a["attribute"] == "R")
    assert religion["verdict_flips"] == 2
    assert religion["verdict_flip_rate"] == round(2 / 3, 4)
