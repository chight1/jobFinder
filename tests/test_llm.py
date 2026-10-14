from __future__ import annotations

import json

import httpx
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


GOOD_FACTS = {
    "open_to_japan_resident": "yes", "japanese": "plus", "employment": "payroll", "work_mode": "remote",
    "office_city": None, "office_days_per_week": None, "overlap": None, "seniority": "mid",
    "years_required": 3, "stack": ["Java"], "primary_language": "Java", "pay_min": None, "pay_max": None,
    "pay_currency": None, "pay_period": None, "overtime": "none_mentioned", "on_call": "no",
    "is_software_job": "yes",
    "evidence": {"open_to_japan_resident": "Remote in APAC", "japanese": "Japanese a plus", "employment": "Full-time"},
}

CANONICAL_LIKE = """
<p>We are hiring an MLOps Field Engineer. It is not a software development role.</p>
<p>Location: Most of our colleagues work from home and we can accommodate candidates from almost any country.</p>
<h2>What your day will look like</h2><ul><li>Architect Kubernetes solutions</li><li>Travel up to 30%</li></ul>
<h2>What we are looking for in you</h2><ul><li>Intermediate Python</li><li>Practical Linux</li></ul>
<h2>What we offer colleagues</h2><ul><li>Annual compensation review</li><li>Recognition rewards</li></ul>
<h2>About Canonical</h2><p>Canonical publishes Ubuntu. Canonical is an equal opportunity employer.</p>
"""


def test_trim_keeps_unusual_headings_and_drops_company_filler():
    trimmed = llm.trim_description(CANONICAL_LIKE, cap=6000, company="Canonical")
    for kept in ("not a software development role", "almost any country", "Travel up to 30%",
                 "Intermediate Python", "Annual compensation review"):
        assert kept in trimmed
    assert "publishes Ubuntu" not in trimmed  # "About Canonical" is filler


def test_a_bullet_is_never_a_heading():
    assert llm._heading("- Annual compensation review") is None
    assert llm._heading("Salary") == "Salary"


def test_trimming_that_drops_most_of_a_posting_sends_it_whole():
    text = "Our values\n" + "We build payment systems in Go and need a backend engineer. " * 30
    assert llm.trim_description(text, cap=10_000) == llm.to_text(text)[:10_000]


def test_numbers_in_reads_common_formats():
    values = llm.numbers_in("Pay $120k–$150,000, or 800万円; three years; 2 days in office")
    for expected in (120_000, 150_000, 8_000_000, 3, 2):
        assert expected in values


def test_guessed_numbers_are_cleared():
    facts = llm.Facts.model_validate(GOOD_FACTS | {
        "pay_min": 120000, "pay_max": 180000, "pay_currency": "USD", "pay_period": "year",
        "years_required": 3, "office_days_per_week": 2,
    })
    checked = llm.drop_unsupported_numbers(facts, "Remote role. Python and Linux. 2 days a month in office.")
    assert (checked.pay_min, checked.pay_max, checked.pay_currency, checked.years_required) == (None, None, None, None)
    assert checked.office_days_per_week == 2  # "2" is written, so it stays

    stated = llm.drop_unsupported_numbers(facts, "Salary $120,000 - $180,000. 3+ years of Python.")
    assert (stated.pay_min, stated.years_required) == (120000, 3)


def test_trim_without_headings_keeps_everything_up_to_cap():
    text = "We need an engineer. " * 500
    assert llm.trim_description(text, cap=100) == text.strip()[:100]


def test_json_object_ignores_fences():
    assert llm._json_object('```json\n{"a": 1}\n```') == '{"a": 1}'
    with pytest.raises(ValueError):
        llm._json_object("no json here")


def test_an_invalid_reply_is_logged_as_one(cfg, conn):
    replies = iter(["Sorry, I can't continue with that.", json.dumps(GOOD_FACTS)])

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    client = llm.LLM(cfg, conn)
    client._client = httpx.Client(transport=httpx.MockTransport(server))
    assert client.chat_json("extract", 7, [{"role": "user", "content": "facts?"}], llm.Facts).employment == "payroll"
    calls = conn.execute("SELECT ok, error FROM llm_log ORDER BY id").fetchall()
    assert [call["ok"] for call in calls] == [0, 1]
    assert calls[0]["error"].startswith("invalid reply: no JSON object")


_LOOPED = {"error": {"message": "prediction aborted, token repeat limit reached", "type": "api_error"}}


def test_a_looped_reply_is_tried_once_more_without_waiting(cfg, conn, monkeypatch):
    replies = iter([httpx.Response(500, json=_LOOPED),
                    httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD_FACTS)}}]})])
    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("no wait for a loop"))
    client = llm.LLM(cfg, conn)
    client._client = httpx.Client(transport=httpx.MockTransport(lambda request: next(replies)))
    assert client.chat_json("extract", 7, [{"role": "user", "content": "facts?"}], llm.Facts).employment == "payroll"


def test_a_job_that_keeps_looping_fails_alone(cfg, conn, monkeypatch):
    calls = []

    def server(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json=_LOOPED)

    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("no wait for a loop"))
    client = llm.LLM(cfg, conn)
    client._client = httpx.Client(transport=httpx.MockTransport(server))
    with pytest.raises(llm.GenerationAborted):  # not LLMError, which would stop the worker
        client.chat_json("extract", 7, [{"role": "user", "content": "facts?"}], llm.Facts)
    assert len(calls) == 2


def test_facts_schema_rejects_unknown_values():
    assert llm.Facts.model_validate(GOOD_FACTS).employment == "payroll"
    with pytest.raises(ValidationError):
        llm.Facts.model_validate(GOOD_FACTS | {"employment": "permanent"})
    with pytest.raises(ValidationError):
        llm.Facts.model_validate(GOOD_FACTS | {"extra_field": 1})
