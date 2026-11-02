from __future__ import annotations

import pytest

from jobfinder import score

PAY_CFG = {"full_usd": 120_000, "zero_usd": 40_000, "at_floor": 0.7, "unknown": 0.4}


@pytest.mark.parametrize(
    ("usd", "part"),
    [(150_000, 1.0), (120_000, 1.0), (95_000, 0.85), (70_000, 0.7), (55_000, 0.35), (40_000, 0.0), (None, 0.4)],
)
def test_pay_part(usd, part, profile):
    assert score._pay_part(usd, profile, PAY_CFG) == pytest.approx(part)


def test_annual_usd_conversions():
    rates = {"USD": 1.0, "JPY": 0.0067}
    assert score.annual_usd(500_000, None, "JPY", "month", rates) == pytest.approx(500_000 * 12 * 0.0067)
    assert score.annual_usd(50, 70, "USD", "hour", rates) == pytest.approx(60 * 2000)
    assert score.annual_usd(None, None, "USD", "year", rates) is None
    assert score.annual_usd(100, 200, "XYZ", "year", rates) is None


@pytest.mark.parametrize(
    ("overlap", "part"),
    [
        ({"time_zone": "UTC+9", "start_hour": 9, "end_hour": 18}, 1.0),  # 09:00-18:00 JST
        ({"time_zone": "UTC", "start_hour": 11, "end_hour": 14}, 0.5),  # 20:00-23:00 JST
        ({"time_zone": "UTC-5", "start_hour": 9, "end_hour": 17}, 0.1),  # 23:00-07:00 JST
        ({"time_zone": "Nowhere/Mars", "start_hour": 9, "end_hour": 17}, 0.6),
        (None, 0.6),
    ],
)
def test_balance_part(overlap, part, cfg):
    assert score._balance_part({"overlap": overlap}, cfg.score["balance"]) == pytest.approx(part)


def test_balance_multipliers(cfg):
    facts = {"overlap": {"time_zone": "JST", "start_hour": 10, "end_hour": 18}, "overtime": "heavy", "on_call": "yes"}
    assert score._balance_part(facts, cfg.score["balance"]) == pytest.approx(1.0 * 0.5 * 0.8)


def test_overlap_split_at_midnight():
    intervals = score.overlap_in_jst({"time_zone": "UTC", "start_hour": 13, "end_hour": 17})
    assert intervals == [(22.0, 24.0), (0.0, 2.0)]


@pytest.mark.parametrize(
    ("facts", "part"),
    [
        ({"primary_language": "JavaScript"}, 1.0),
        ({"primary_language": "JS"}, 1.0),
        ({"primary_language": "Java"}, 0.8),
        ({"primary_language": "Python"}, 0.5),
        ({"primary_language": "Rust"}, 0.3),
        ({"primary_language": None}, 0.6),
    ],
)
def test_stack_part(facts, part, cfg):
    assert score._stack_part(facts, cfg.profile, cfg.score["stack"]) == pytest.approx(part)


@pytest.mark.parametrize(
    ("facts", "part"),
    [
        ({"years_required": 3}, 1.0),
        ({"years_required": 5}, 1.0),
        ({"years_required": 7}, 0.6),
        ({"years_required": 20}, 0.0),
        ({"seniority": "staff_plus", "years_required": 2}, 0.2),
        ({}, 0.7),
    ],
)
def test_seniority_part(facts, part, cfg):
    assert score._seniority_part(facts, cfg.profile, cfg.score["seniority"]) == pytest.approx(part)


@pytest.mark.parametrize(
    ("facts", "part"),
    [
        ({"work_mode": "remote", "open_to_japan_resident": "yes"}, 1.0),
        ({"work_mode": "onsite", "office_city": "Osaka"}, 1.0),
        ({"work_mode": "hybrid", "office_city": "Tokyo", "office_days_per_week": 2}, 0.5),
        ({"work_mode": "hybrid", "office_city": "Shibuya", "office_days_per_week": 3}, 0.3),
        ({"work_mode": "hybrid", "office_city": "Tokyo"}, 0.3),
        ({"work_mode": "hybrid", "office_city": None}, 0.6),
    ],
)
def test_location_part(facts, part, cfg, gaz):
    assert score._location_part(facts, cfg.profile, gaz, cfg.score["location"]) == pytest.approx(part)


UNICORN = {
    "open_to_japan_resident": "yes",
    "japanese": "plus",
    "employment": "payroll",
    "work_mode": "remote",
    "office_city": None,
    "office_days_per_week": None,
    "overlap": {"time_zone": "UTC+9", "start_hour": 10, "end_hour": 19},
    "seniority": "senior",
    "years_required": 5,
    "stack": ["React", "TypeScript"],
    "primary_language": "JavaScript",
    "pay_min": 110_000,
    "pay_max": 130_000,
    "pay_currency": "USD",
    "pay_period": "year",
    "overtime": "none_mentioned",
    "on_call": "no",
    "is_software_job": "yes",
}


def test_score_job_perfect_fit(cfg, gaz):
    scored = score.score_job(UNICORN, cfg, gaz)
    assert scored.score == 100.0
    assert scored.badges == ["unicorn"]
    assert scored.reason_line == (
        "remote · 110,000–130,000 USD/year ($120k) · JavaScript (level 3) · 5 yrs asked / 5 · 10:00–19:00 JST"
    )


def test_score_job_badges(cfg, gaz):
    facts = UNICORN | {"employment": "dispatch", "open_to_japan_resident": "unclear"}
    assert score.score_job(facts, cfg, gaz).badges == ["check", "dispatch"]
    assert "check" in score.score_job({}, cfg, gaz, stage="extract_failed").badges


def test_score_weights_follow_config(cfg, gaz):
    facts = UNICORN | {"pay_min": None, "pay_max": None}
    expected = 100 * (0.40 * 0.4 + 0.20 + 0.20 + 0.10 + 0.10)
    assert score.score_job(facts, cfg, gaz).score == pytest.approx(expected, abs=0.05)
