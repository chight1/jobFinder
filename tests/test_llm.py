from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobhunt import llm

POSTING = """
<h2>About us</h2><p>Founded in 1999, we love culture.</p>
<h2>The role</h2><p>Build payment APIs.</p>
<h2>Requirements</h2><ul><li>5 years of Java</li><li>English</li></ul>
<h2>Equal opportunity</h2><p>We welcome everyone.</p>
<p><strong>Salary</strong></p><p>¥8,000,000 – ¥10,000,000</p>
"""


def test_trim_keeps_needed_sections_only():
    trimmed = llm.trim_description(POSTING, cap=6000)
    assert "Build payment APIs." in trimmed
    assert "5 years of Java" in trimmed
    assert "¥8,000,000" in trimmed  # a bold line naming a kept section counts as a heading
    assert "Founded in 1999" not in trimmed
    assert "We welcome everyone" not in trimmed


def test_trim_without_headings_keeps_everything_up_to_cap():
    text = "We need an engineer. " * 500
    assert llm.trim_description(text, cap=100) == text.strip()[:100]


def test_json_object_ignores_fences():
    assert llm._json_object('```json\n{"a": 1}\n```') == '{"a": 1}'
    with pytest.raises(ValueError):
        llm._json_object("no json here")


def test_facts_schema_rejects_unknown_values():
    good = {
        "open_to_japan_resident": "yes", "japanese": "plus", "employment": "payroll", "work_mode": "remote",
        "office_city": None, "office_days_per_week": None, "overlap": None, "seniority": "mid",
        "years_required": 3, "stack": ["Java"], "primary_language": "Java", "pay_min": None, "pay_max": None,
        "pay_currency": None, "pay_period": None, "overtime": "none_mentioned", "on_call": "no",
        "is_software_job": "yes",
        "evidence": {"open_to_japan_resident": "Remote in APAC", "japanese": "Japanese a plus", "employment": "Full-time"},
    }
    assert llm.Facts.model_validate(good).employment == "payroll"
    with pytest.raises(ValidationError):
        llm.Facts.model_validate(good | {"employment": "permanent"})
    with pytest.raises(ValidationError):
        llm.Facts.model_validate(good | {"extra_field": 1})
