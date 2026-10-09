from __future__ import annotations

import pytest

from jobhunt import rules


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Tokyo, Japan", "japan"),
        ("東京都渋谷区", "japan"),
        ("Osaka", "japan"),
        ("Remote in Japan", "japan"),
        ("Hybrid - Shibuya", "japan"),
        ("Tokyo; London", "japan"),
        ("APAC", "asia"),
        ("Remote - Asia-Pacific", "asia"),
        ("Worldwide", "anywhere"),
        ("Remote (US or anywhere)", "anywhere"),
        ("Remote", "remote"),
        ("Remote - US", "outside"),
        ("London, UK", "outside"),
        ("San Francisco, CA", "outside"),
        ("EMEA", "outside"),
        ("Berlin, Germany", "outside"),
        ("Remote, open to candidates in any country", "remote"),
        ("AI Engineer | REMOTE", "remote"),  # "AI" is a role here, not Anguilla
        ("Osaka, JP", "japan"),
        ("Anywhere in Germany", "outside"),
        ("Remote, anywhere in the US", "outside"),
        ("Anywhere in the world", "anywhere"),
        ("Anywhere in Japan", "japan"),
        ("Internationally located (not in the US, CA, UK, NZ, or AU)", "unknown"),
        ("Remote - Europe, excluding Germany", "outside"),
        ("Remote, outside IR35, UK", "outside"),
        ("", "unknown"),
        ("Head office", "unknown"),
    ],
)
def test_location(location, expected, gaz):
    assert rules.classify_location(location, gaz) == expected


@pytest.mark.parametrize(
    ("title", "removed"),
    [
        ("Account Executive", True),
        ("Marketing Manager", True),
        ("Technical Recruiter", True),
        ("Sales Engineer", False),
        ("Senior Backend Engineer", False),
        ("Full-Stack Developer", False),
        ("SRE", False),
        ("Product Designer", False),  # unclear: the model judges it
    ],
)
def test_title_rule(title, removed, gaz):
    removal = rules.code_rules(title=title, location="", description_text="", fields={}, gaz=gaz)
    assert (removal is not None and removal.rule == "not software") is removed


def test_japanese_posting_removed(gaz):
    text = "私たちはソフトウェアエンジニアを募集しています。業務内容は開発です。" * 3 + " React TypeScript"
    removal = rules.code_rules(title="Engineer", location="", description_text=text, fields={}, gaz=gaz)
    assert removal and removal.rule == "Japanese posting"


def test_english_posting_mentioning_japanese_kept(gaz):
    text = "We build payments software. Japanese (JLPT N2) is a plus. 日本語 welcome. " * 5
    assert rules.code_rules(title="Engineer", location="", description_text=text, fields={}, gaz=gaz) is None


@pytest.mark.parametrize(
    ("fields", "title", "removed"),
    [
        ({"employment_type": "Contract"}, "Engineer", True),
        ({"employment_type": "Freelance"}, "Engineer", True),
        ({"employment_type": "Temporary"}, "Engineer", True),
        ({"employment_type": "Full-time"}, "Engineer", False),
        ({"employment_type": "Full-time, Contract"}, "Engineer", False),  # not a clear case
        ({}, "Contract React Developer", True),
        ({}, "[Freelance] Java Engineer", True),
        ({}, "Engineer (contract to hire)", False),
    ],
)
def test_contract_rule(fields, title, removed, gaz):
    removal = rules.code_rules(title=title, location="", description_text="", fields=fields, gaz=gaz)
    assert (removal is not None and removal.rule == "contract") is removed


def test_location_removal_carries_the_text_as_quote(gaz):
    removal = rules.code_rules(title="Engineer", location="Remote - US", description_text="", fields={}, gaz=gaz)
    assert removal == rules.Removal("location", "Remote - US")


def test_prescore_weights(gaz, profile):
    rates = {"USD": 1.0}
    best = rules.prescore(
        title="Senior React Engineer", location="Tokyo", posted_at=None,
        fields={"pay_min": 90_000, "pay_max": 110_000, "pay_currency": "USD", "pay_period": "year"},
        gaz=gaz, profile=profile, rates=rates,
    )
    assert best == 3 * 1 + 2 * 1 + 1  # location, title, pay; no posted date
    bare_remote = rules.prescore(
        title="Engineer", location="Remote", posted_at=None, fields={}, gaz=gaz, profile=profile, rates=rates
    )
    assert bare_remote == 3 * 0.5 + 2 * 0.5


FACTS = {
    "open_to_japan_resident": "yes",
    "japanese": "not_mentioned",
    "employment": "payroll",
    "work_mode": "remote",
    "office_city": None,
    "is_software_job": "yes",
    "evidence": {"open_to_japan_resident": "Remote within APAC", "japanese": "", "employment": "Full-time"},
}


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({}, None),
        ({"open_to_japan_resident": "no"}, "Not open to Japan"),
        ({"open_to_japan_resident": "unclear"}, None),
        ({"japanese": "required"}, "Requires Japanese"),
        ({"japanese": "plus"}, None),
        ({"employment": "contract"}, "Not payroll"),
        ({"employment": "dispatch"}, None),
        ({"employment": "unclear"}, None),
        ({"work_mode": "onsite", "office_city": "Berlin"}, "Can't commute"),
        ({"work_mode": "onsite", "office_city": "Tokyo"}, "Can't commute"),
        ({"work_mode": "hybrid", "office_city": "Tokyo"}, None),
        ({"work_mode": "hybrid", "office_city": "Shibuya"}, None),
        ({"work_mode": "onsite", "office_city": "Osaka"}, None),
        ({"work_mode": "onsite", "office_city": None}, None),
        ({"is_software_job": "no"}, "Not software"),
    ],
)
def test_fact_rules(change, rule, gaz, profile):
    removal = rules.fact_rules(FACTS | change, title="Engineer", profile=profile, gaz=gaz)
    assert (removal.rule if removal else None) == rule


def test_restored_rule_never_removes_again(gaz, profile):
    facts = FACTS | {"japanese": "required"}
    assert rules.fact_rules(facts, title="Engineer", profile=profile, gaz=gaz, restored=["Requires Japanese"]) is None


def test_unclear_facts(gaz):
    assert rules.unclear_facts(FACTS | {"employment": "unclear"})
    assert rules.unclear_facts(FACTS | {"work_mode": "hybrid", "office_city": None})
    assert not rules.unclear_facts(FACTS)
