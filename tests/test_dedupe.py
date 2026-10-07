from __future__ import annotations

import pytest

from jobhunt import dedupe


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("HTTP://Jobs.Lever.co/acme/123/?utm_source=x&lever-source=y&a=1#apply", "https://jobs.lever.co/acme/123?a=1"),
        ("https://boards.greenhouse.io/acme/jobs/9?gh_src=abc", "https://boards.greenhouse.io/acme/jobs/9"),
        ("https://example.com/jobs/1/?ref=hn&source=x&utm_medium=y", "https://example.com/jobs/1"),
        ("https://example.com/Jobs/ABC", "https://example.com/Jobs/ABC"),  # path case is kept
    ],
)
def test_canonical_url(url, canonical):
    assert dedupe.canonical_url(url) == canonical


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Money Forward, Inc.", "money forward"),
        ("株式会社メルカリ", "メルカリ"),
        ("メルカリ(株)", "メルカリ"),
        ("Ｍｅｒｃａｒｉ Ｋ.Ｋ.", "mercari"),
        ("ACME Co., Ltd.", "acme"),
        ("Woven by Toyota GmbH", "woven by toyota"),
        ("", ""),
    ],
)
def test_company_norm(raw, norm):
    assert dedupe.company_norm(raw) == norm


def test_company_alias():
    assert dedupe.company_norm("Woven Planet", {"woven planet": "woven by toyota"}) == "woven by toyota"


def test_title_norm_drops_brackets_and_places(gaz):
    expected = "senior backend engineer"
    assert dedupe.title_norm("Sr. Backend Engineer (Remote)", gaz) == expected
    assert dedupe.title_norm("Senior Backend Engineer - Tokyo", gaz) == expected
    assert dedupe.title_norm("【Tokyo】Senior  Backend Engineer", gaz) == expected
    assert dedupe.title_norm("Jr Developer", gaz) == "junior developer"
    assert dedupe.title_norm("C++ Engineer", gaz) != dedupe.title_norm("C# Engineer", gaz)
    assert dedupe.title_norm("Engineer, Level 2", gaz) == "engineer level 2"


def test_city_norm(gaz):
    assert dedupe.city_norm("Shibuya, Tokyo, Japan", gaz) == "shibuya"
    assert dedupe.city_norm("London; Tokyo", gaz) == "tokyo"  # a Japanese city first
    assert dedupe.city_norm("Remote", gaz) == "remote"
    assert dedupe.city_norm("", gaz) == "remote"


def test_agency():
    agencies = ["Robert Walters", "Hays"]
    assert dedupe.is_agency("", agencies)
    assert dedupe.is_agency(dedupe.company_norm("Robert Walters Japan K.K."), agencies)
    assert not dedupe.is_agency("mercari", agencies)
    assert not dedupe.is_agency("haystack", agencies)  # whole words only


BODY = " ".join(f"word{i}" for i in range(200))


def test_fuzzy_merge_borderline_different():
    verdict, title_sim, desc_sim = dedupe.fuzzy_verdict("senior backend engineer", BODY,
                                                        "senior backend engineer", BODY)
    assert verdict == "merge" and title_sim == 1.0 and desc_sim == 1.0

    # 156 shared shingles of 216 in all: about 0.72
    half = " ".join(f"word{i}" for i in range(160)) + " " + " ".join(f"other{i}" for i in range(20))
    verdict, _, desc_sim = dedupe.fuzzy_verdict("backend engineer", BODY, "backend engineer", half)
    assert 0.5 <= desc_sim < 0.8 and verdict == "borderline"

    verdict, _, _ = dedupe.fuzzy_verdict("backend engineer", BODY, "designer", "something else entirely here")
    assert verdict == "different"


def test_shingles_short_text():
    assert dedupe.shingles("one two") == {"one two"}
    assert dedupe.shingles("") == set()
    assert len(dedupe.shingles("a b c d e f")) == 2


def test_agency_index_finds_near_copy():
    texts = {1: BODY, 2: " ".join(f"different{i}" for i in range(200))}
    index = dedupe.AgencyIndex(lambda: texts.items())
    match = index.best_match(BODY + " extra")
    assert match is not None and match[0] == 1
    assert index.best_match("nothing like the others at all " * 10) is None
