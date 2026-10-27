from __future__ import annotations

import pytest

from app.criteria import DEFAULT_YAML, CriteriaError, parse
from app.matching import STRONG_MATCH, fingerprint, normalise_company, normalise_title, score_job, score_profile

CRITERIA = parse(DEFAULT_YAML)


# ---------- de-duplication ----------


def test_same_job_on_two_boards_shares_a_fingerprint():
    a = fingerprint("Junior Python Developer", "Acme Software Ltd", "Leeds")
    b = fingerprint("Junior Python Developer (Hybrid)", "ACME SOFTWARE LIMITED", "Leeds, West Yorkshire")
    assert a == b


def test_different_jobs_get_different_fingerprints():
    base = fingerprint("Junior Python Developer", "Acme", "Leeds")
    assert fingerprint("Junior Java Developer", "Acme", "Leeds") != base
    assert fingerprint("Junior Python Developer", "Other Co", "Leeds") != base
    assert fingerprint("Junior Python Developer", "Acme", "York") != base


def test_normalisers():
    assert normalise_company("The Acme Group (UK) Ltd.") == "acme"
    assert normalise_title("C# / .NET Developer [Remote]") == "c# net developer"


# ---------- scoring ----------


def test_good_fit_scores_strongly_with_a_plain_reason():
    m = score_job(CRITERIA, "Junior Python Developer", "Python, Git and SQL on Linux.", 26000, 30000)
    assert m.score >= STRONG_MATCH
    assert m.profile == "Junior developer"
    assert m.reason.startswith("Fits Junior developer:")
    assert "python" in m.reason and "£30,000" in m.reason


def test_dealbreaker_in_title_scores_zero():
    m = score_job(CRITERIA, "Senior Python Developer", "Python", 60000, None)
    assert m.score == 0
    assert "senior" in m.reason


def test_unrelated_job_scores_low():
    assert score_job(CRITERIA, "Warehouse Operative", "Picking and packing.", None, None).score < 20


def test_must_have_missing_halves_score():
    crit = parse(DEFAULT_YAML)
    apprenticeships = crit.profiles[0]
    title, desc = "Software Developer Apprenticeship", "software developer python coding"
    # "apprentice" (the default must-have) matches "Apprenticeship".
    with_word = score_profile(apprenticeships, crit, title, desc, None, None)
    assert "Doesn't mention" not in with_word.reason
    apprenticeships.must_have = ["level 4"]
    without = score_profile(apprenticeships, crit, title, desc, None, None)
    assert without.score == round(with_word.score / 2) or abs(without.score - with_word.score / 2) <= 1
    assert "level 4" in without.reason


def test_salary_floor():
    crit = parse(DEFAULT_YAML.replace("salary_floor: 0", "salary_floor: 25000"))
    args = ("Junior Python Developer", "python git sql")
    above = score_job(crit, *args, 26000, 30000).score
    unknown = score_job(crit, *args, None, None).score
    below = score_job(crit, *args, 18000, 20000)
    assert above > unknown > below.score
    assert "under your £25,000 floor" in below.reason


def test_keywords_match_whole_words_only():
    # "git" shouldn't match inside "digital"
    m = score_job(CRITERIA, "Junior Developer", "digital marketing", None, None)
    assert "git" not in m.reason


# ---------- criteria file ----------


def test_default_criteria_parse():
    assert [p.name for p in CRITERIA.profiles] == [
        "Software apprenticeships",
        "Junior developer",
        "Electronics or repair technician",
    ]
    assert CRITERIA.calm_mode_on and CRITERIA.daily_cap == 3


@pytest.mark.parametrize(
    "text, message",
    [
        ("profiles: [", "couldn't be read"),
        ("location: Leeds", "at least one profile"),
        ("profiles:\n  - searches: [x]", "needs a name"),
        ("profiles:\n  - name: A", "at least one search"),
        ("profiles:\n  - name: A\n    searches: dev", "should be a list"),
        ("salary_floor: lots\nprofiles:\n  - name: A\n    searches: [x]", "whole number"),
        ("fetch_every_minutes: 1\nprofiles:\n  - name: A\n    searches: [x]", "at least 15"),
    ],
)
def test_bad_criteria_explain_themselves(text, message):
    with pytest.raises(CriteriaError, match=message):
        parse(text)
