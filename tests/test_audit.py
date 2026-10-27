"""The audit corpus: its composition, its declared pairings, and its integrity."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import audit, db

SWE_II = Path(__file__).resolve().parent.parent / "test-resumes" / "swe_ii_corpus"


# ---------------------------------------------------------------------------
# Corpus composition
# ---------------------------------------------------------------------------

def test_the_position_description_is_not_treated_as_a_candidate():
    files = [Path(p).name for p in audit.list_corpus(SWE_II)]
    assert "position-description.pdf" not in files
    # 4 people x 3 levels x (baseline + gender + race + religion)
    assert len(files) == 48


def test_every_person_has_all_three_skill_levels():
    import json as _json

    manifest = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))
    by_person: dict[str, set] = {}
    for entry in manifest["resumes"]:
        by_person.setdefault(entry["person"], set()).add(entry["level"])
    assert len(by_person) == 4
    assert all(levels == {"senior", "junior", "unqualified"} for levels in by_person.values())


def test_the_skill_levels_have_the_coverage_they_claim():
    """The corpus is only trustworthy if its expected outcome is a fact about
    the documents rather than an opinion."""
    import json as _json

    levels = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))["levels"]

    assert levels["senior"]["required_met"] == levels["senior"]["required_total"]
    assert levels["senior"]["preferred_met"] == levels["senior"]["preferred_total"]
    assert levels["senior"]["expected_stage1"] == "pass"

    # Junior clears every required bar and little else — it must still pass,
    # because stage 1 gates on required qualifications alone.
    assert levels["junior"]["required_met"] == levels["junior"]["required_total"]
    assert levels["junior"]["preferred_met"] == 1
    assert levels["junior"]["expected_stage1"] == "pass"

    # Unqualified holds some of both, all of neither.
    assert 0 < levels["unqualified"]["required_met"] < levels["unqualified"]["required_total"]
    assert 0 < levels["unqualified"]["preferred_met"] < levels["unqualified"]["preferred_total"]
    assert levels["unqualified"]["expected_stage1"] == "fail"


def test_pairs_come_from_the_corpus_manifest():
    """A generated corpus states which variant came from which baseline; this
    module should not have to parse that back out of filenames."""
    pairs, baselines, unpaired = audit.load_pairs(SWE_II)
    assert len(baselines) == 12, "the unmarked resumes"
    assert len(pairs) == 36, "12 gender + 12 race + 12 religion"
    assert unpaired == []
    assert {p["attribute"] for p in pairs} == {"gender", "race", "religion"}


def test_every_skill_level_has_two_male_and_two_female_variants():
    pairs, _, _ = audit.load_pairs(SWE_II)
    by_level: dict[str, list[str]] = {}
    for pair in pairs:
        if pair["attribute"] != "gender":
            continue
        by_level.setdefault(pair["level"], []).append(pair["attribute_label"])
    assert set(by_level) == {"senior", "junior", "unqualified"}
    for level, labels in by_level.items():
        assert sorted(labels) == [
            "Gender — female", "Gender — female",
            "Gender — male", "Gender — male",
        ], level


def test_every_skill_level_covers_all_four_ethnicities():
    pairs, _, _ = audit.load_pairs(SWE_II)
    by_level: dict[str, list[str]] = {}
    for pair in pairs:
        if pair["attribute"] != "race":
            continue
        by_level.setdefault(pair["level"], []).append(pair["attribute_label"])
    assert set(by_level) == {"senior", "junior", "unqualified"}
    for level, labels in by_level.items():
        assert sorted(labels) == [
            "Race / Ethnicity — Black",
            "Race / Ethnicity — East Asian",
            "Race / Ethnicity — Hispanic",
            "Race / Ethnicity — South Asian",
        ], level


def test_a_race_variant_is_compared_against_the_same_gender():
    """Race is measured with gender held constant: the white-coded gendered
    resume is what a raced one is read against, not the they/them baseline.
    Otherwise race and gender move together and neither can be attributed."""
    import json as _json

    manifest = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))
    gender_of = {r["file"]: r["gender"] for r in manifest["resumes"]}

    race_pairs = [p for p in manifest["pairs"] if p["attribute"] == "race"]
    assert race_pairs
    for pair in race_pairs:
        assert "gender-" in pair["baseline"], "a race variant reads against the gendered resume"
        assert gender_of[pair["baseline"]] == gender_of[pair["variant"]], pair["variant"]


def test_every_skill_level_covers_all_four_religions():
    pairs, _, _ = audit.load_pairs(SWE_II)
    by_level: dict[str, list[str]] = {}
    for pair in pairs:
        if pair["attribute"] != "religion":
            continue
        by_level.setdefault(pair["level"], []).append(pair["attribute_label"])
    assert set(by_level) == {"senior", "junior", "unqualified"}
    for level, labels in by_level.items():
        assert sorted(labels) == [
            "Religion — Christian",
            "Religion — Hindu",
            "Religion — Jewish",
            "Religion — Muslim",
        ], level


def test_every_resume_carries_a_volunteer_line():
    """Religion rides on the affiliation, so the volunteering itself must be
    constant — otherwise 'this person volunteers' is read as the religion
    effect. The baseline's affiliation is secular, not absent."""
    from backend.extraction import extract_text_from_pdf

    for path in audit.list_corpus(SWE_II):
        text = extract_text_from_pdf(path)
        assert "COMMUNITY" in text, Path(path).name
        assert "food pantry" in text, Path(path).name


def test_the_volunteer_role_is_not_technical():
    """A volunteer *coding* role would be evidence toward the qualifications
    being scored, and would move verdicts for reasons unrelated to religion."""
    from backend.extraction import extract_text_from_pdf

    text = extract_text_from_pdf(str(SWE_II / "Jordan_Avery_senior.pdf"))
    community = text[text.find("COMMUNITY"):]
    for technical in ("Python", "API", "website", "developer", "code", "database"):
        assert technical.lower() not in community.lower()


def test_a_religion_variant_keeps_the_white_coded_name():
    """Religion is isolated from ethnicity: only the congregation changes, so
    a religion effect cannot be a name effect wearing a different hat."""
    import json as _json

    manifest = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))
    name_of = {r["file"]: r["name"] for r in manifest["resumes"]}
    race_of = {r["file"]: r["race"] for r in manifest["resumes"]}

    religion_pairs = [p for p in manifest["pairs"] if p["attribute"] == "religion"]
    assert religion_pairs
    for pair in religion_pairs:
        assert name_of[pair["baseline"]] == name_of[pair["variant"]], pair["variant"]
        assert race_of[pair["variant"]] == "white"


def test_no_race_variant_carries_a_religion_signal():
    """Arabic and Muslim-coded names signal religion as strongly as ethnicity,
    and religion is a separate pass."""
    import json as _json

    manifest = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))
    names = " ".join(r["name"] for r in manifest["resumes"] if r.get("role") == "race").lower()
    for collides in ("mohammed", "muhammad", "fatima", "aisha", "omar", "ahmed"):
        assert collides not in names


def test_a_gender_variant_changes_only_a_handful_of_lines():
    """A male and a female resume must be the same document otherwise.

    Counting the differing lines is enough to catch a template change that
    leaked into one and not the other; the lines themselves are checked by eye
    when the corpus is regenerated.
    """
    import difflib
    import json as _json

    from backend.extraction import extract_text_from_pdf

    manifest = _json.loads((SWE_II / "corpus.json").read_text(encoding="utf-8"))

    for pair in manifest["pairs"]:
        baseline = extract_text_from_pdf(str(SWE_II / pair["baseline"])).splitlines()
        variant = extract_text_from_pdf(str(SWE_II / pair["variant"])).splitlines()
        assert len(baseline) == len(variant), pair["variant"]

        changed = sum(
            1
            for line in difflib.unified_diff(baseline, variant, lineterm="", n=0)
            if line.startswith("+") and not line.startswith("+++")
        )
        # Name line, contact line, and the sentences carrying a pronoun — each
        # of which rewraps across two or three lines when a word length
        # changes. Anything much beyond that means something else moved.
        assert changed <= 8, f"{pair['variant']}: {changed} lines differ"


def test_the_baseline_pronouns_are_grammatical():
    """Singular 'they' takes plural verb forms. A baseline reading "They owns"
    is badly written, and that is a difference the audit would misattribute."""
    from backend.extraction import extract_text_from_pdf

    for path in audit.list_corpus(SWE_II):
        text = " ".join(extract_text_from_pdf(path).split())
        for broken in ("They owns", "they owns", "They is", "they is", "they mentors"):
            assert broken not in text, f"{Path(path).name}: {broken}"


def test_corpus_discovery_finds_the_corpus():
    found = {c["name"]: c for c in audit.describe_corpora()}
    assert "swe_ii_corpus" in found
    assert found["swe_ii_corpus"]["skillLevels"] == {"senior": 4, "junior": 4, "unqualified": 4}
    assert found["swe_ii_corpus"]["pairs"] == 36
    assert found["swe_ii_corpus"]["positionDescription"] == "position-description.pdf"


def test_resolve_corpus_refuses_a_path_outside_the_root():
    assert audit.resolve_corpus("swe_ii_corpus").name == "swe_ii_corpus"
    for bad in ("../backend", "..", "nope"):
        with pytest.raises(ValueError):
            audit.resolve_corpus(bad)


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
# Side-by-side comparison
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


