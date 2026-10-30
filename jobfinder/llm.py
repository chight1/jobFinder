"""Talks to the local model and validates what it returns.

The model only reads postings and fills in facts. It never scores.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobfinder import db
from jobfinder.config import Config
from jobfinder.text import to_text

log = logging.getLogger(__name__)

# extract-2: trimming keeps all but filler sections, and unstated numbers are cleared.
EXTRACT_PROMPT_VERSION = "extract-2"

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The model server is down or failing: stop asking it for this run."""


class RequestRejected(Exception):
    """The server refused this one request (HTTP 4xx), e.g. a prompt longer than its context."""


class InvalidOutput(Exception):
    """The model's reply failed validation twice."""


class GenerationAborted(Exception):
    """The server cut the model off on this request ("token repeat limit reached": it looped). The job's
    text is the trouble, not the server, so the job fails and the queue goes on."""


# --- facts schema ----------------------------------------------------------------


class Overlap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_zone: str = Field(description="IANA name or abbreviation, e.g. America/New_York, PST, UTC+1")
    start_hour: float = Field(ge=0, le=24)
    end_hour: float = Field(ge=0, le=24)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_to_japan_resident: str
    japanese: str
    employment: str


class Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_to_japan_resident: Literal["yes", "no", "unclear"]
    japanese: Literal["required", "plus", "not_mentioned"]
    employment: Literal["payroll", "employer_of_record", "contract", "freelance", "dispatch", "unclear"]
    work_mode: Literal["remote", "hybrid", "onsite"]
    office_city: str | None
    office_days_per_week: float | None
    overlap: Overlap | None
    seniority: Literal["junior", "mid", "senior", "staff_plus", "unclear"]
    years_required: float | None
    stack: list[str]
    primary_language: str | None
    pay_min: float | None
    pay_max: float | None
    pay_currency: str | None = Field(description="ISO 4217 code, e.g. JPY, USD")
    pay_period: Literal["year", "month", "hour"] | None
    overtime: Literal["none_mentioned", "some", "heavy"]
    on_call: Literal["yes", "no", "not_mentioned"]
    is_software_job: Literal["yes", "no"]
    evidence: Evidence


EXTRACT_SYSTEM = """\
You read one job posting and record facts about it as a single JSON object.
Use only what the posting says. When it does not say, answer "unclear", "not_mentioned" or null
as the field allows. Never guess numbers.

Fields:
- open_to_japan_resident: "yes" if a person living in Japan can take the job (based in Japan, or remote
  and open to Japan, Asia, APAC or worldwide). "no" if it is limited to other countries or regions,
  or needs relocation or work authorization outside Japan. Otherwise "unclear".
- japanese: "required" if Japanese ability is mandatory (business level, JLPT N1/N2, native);
  "plus" if it is preferred or nice to have; otherwise "not_mentioned".
- employment: "payroll" (an employee of the hiring company), "employer_of_record" (an employee
  through a service such as Deel or Remote.com), "contract", "freelance", "dispatch" (employed,
  then placed at a client's site, also called haken or SES), or "unclear".
- work_mode: "remote", "hybrid" or "onsite". office_city: the office city for hybrid or onsite work,
  else null. office_days_per_week: office days asked for hybrid work, else null.
- overlap: required working hours shared with a team, in the time zone the posting gives, as hours
  from 0 to 24; null if not stated.
- seniority and years_required: the level and the minimum years of experience asked.
- stack: the technologies named. primary_language: the main programming language of the role.
- pay_min, pay_max, pay_currency, pay_period: the listed pay as plain numbers, the currency code,
  and "year", "month" or "hour"; null when no pay is listed.
- overtime: "heavy" for long or frequent overtime, "some" if overtime is mentioned, else "none_mentioned".
- on_call: on-call or pager duty.
- is_software_job: "yes" if the work is mainly building, testing or running software.
- evidence: a short exact quote from the posting behind each of open_to_japan_resident, japanese
  and employment; "" when the answer is unclear or not mentioned.

Reply with the JSON object only."""


# --- client ---------------------------------------------------------------------


class LLM:
    def __init__(self, cfg: Config, conn: sqlite3.Connection) -> None:
        settings = cfg.llm
        if not settings["model"]:
            raise LLMError("config.toml [llm] model is empty")
        self._url = settings["base_url"].rstrip("/") + "/chat/completions"
        self._model = settings["model"]
        self._structured = bool(settings["structured_output"])
        self._client = httpx.Client(timeout=float(settings["timeout_s"]))
        self._retry_waits_s = [float(s) for s in settings.get("retry_waits_s", [30, 120, 300])]
        self._conn = conn

    def close(self) -> None:
        self._client.close()

    def chat_json(self, kind: str, job_id: int | None, messages: list[dict[str, str]], schema: type[T]) -> T:
        """One call, validated. An invalid reply gets one retry with the error added to the prompt."""
        response_format = None
        if self._structured:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True},
            }
        messages = list(messages)
        for attempt in (1, 2):
            reply = self._call(kind, job_id, messages, response_format)
            try:
                return schema.model_validate_json(_json_object(reply))
            except (ValidationError, ValueError) as exc:
                self._mark_invalid(exc)
                if attempt == 2:
                    raise InvalidOutput(str(exc)[:500]) from exc
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": f"That reply was invalid:\n{exc}\nReply again with only the corrected JSON object."},
                ]
        raise AssertionError("unreachable")

    def chat_text(self, kind: str, job_id: int | None, messages: list[dict[str, str]]) -> str:
        return self._call(kind, job_id, messages, None).strip()

    def _call(
        self, kind: str, job_id: int | None, messages: list[dict[str, str]], response_format: dict | None
    ) -> str:
        """One request, retried after waits when the server fails. A crashed engine restarts on the
        next request, so a single failure costs a short wait, not the night."""
        body: dict = {"model": self._model, "messages": messages, "temperature": 0}
        if response_format:
            body["response_format"] = response_format
        attempts = len(self._retry_waits_s) + 1
        aborted = False
        for attempt in range(1, attempts + 1):
            try:
                return self._post(kind, job_id, body)
            except GenerationAborted as exc:
                if aborted:
                    raise
                aborted = True  # a loop is not certain to repeat: one more try straight away
                log.warning("model looped on this request, trying once more: %s", exc)
            except LLMError as exc:
                if attempt == attempts:
                    raise
                wait = self._retry_waits_s[attempt - 1]
                log.warning("model call failed (attempt %d of %d), retrying in %gs: %s", attempt, attempts, wait, exc)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _post(self, kind: str, job_id: int | None, body: dict) -> str:
        started = time.monotonic()
        try:
            resp = self._client.post(self._url, json=body)
        except httpx.TransportError as exc:
            message = f"model server unreachable at {self._url}: {exc!r}"
            self._log(kind, job_id, time.monotonic() - started, error=message)
            raise LLMError(message) from exc
        seconds = time.monotonic() - started
        if resp.status_code != 200:
            message = f"model server answered HTTP {resp.status_code}: {resp.text[:300]}"
            self._log(kind, job_id, seconds, error=message)
            if 400 <= resp.status_code < 500:
                raise RequestRejected(message)
            if "prediction aborted" in resp.text:
                raise GenerationAborted(message)
            raise LLMError(message)
        self._log(kind, job_id, seconds)
        return resp.json()["choices"][0]["message"].get("content") or ""

    def _log(self, kind: str, job_id: int | None, seconds: float, *, error: str | None = None) -> None:
        self._last_log_id = self._conn.execute(
            "INSERT INTO llm_log (kind, job_id, seconds, ok, at, error) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, job_id, seconds, int(error is None), db.utc_now(), error),
        ).lastrowid

    def _mark_invalid(self, exc: Exception) -> None:
        """The last call answered, but not with what was asked for: log it as a failure."""
        reason = " ".join(str(exc).split())[:300]
        self._conn.execute(
            "UPDATE llm_log SET ok = 0, error = ? WHERE id = ?", (f"invalid reply: {reason}", self._last_log_id)
        )


def _json_object(reply: str) -> str:
    """The JSON object inside a reply, ignoring code fences or text around it."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    return reply[start : end + 1]


# --- input trimming ----------------------------------------------------------------

# Sections that never help judge a job: company history, culture, equal-opportunity and privacy text.
# Everything else is kept, so a posting whose headings are unusual still reaches the model whole.
_DROP_HEADING = re.compile(
    r"^about (us|the company)\b|who we are|our (story|mission|culture|values|company|history)|"
    r"\bculture\b|\bvalues\b|equal (employment )?opportunit|\beeo\b|diversity|inclusion|privacy|"
    r"accommodation|why join|life at\b",
    re.IGNORECASE,
)
_MAX_HEADING_WORDS = 8
_MAX_SHORT_HEADING_WORDS = 5
_BULLET = re.compile(r"^\s*([-*•·]|\d+[.)])\s")
MIN_KEPT_SHARE = 0.4  # trimming that keeps less than this share of the posting sends it whole instead


def _heading(line: str) -> str | None:
    """The heading text if this line is a heading, else None. A bullet point is never a heading."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    if not stripped or _BULLET.match(line) or len(stripped.split()) > _MAX_HEADING_WORDS:
        return None
    if stripped.endswith(":") or (stripped.isupper() and any(c.isalpha() for c in stripped)):
        return stripped.rstrip(":").strip()
    # A short capitalized line on its own, such as a bold "Salary" or "About Canonical", starts a section.
    # Reading a stray short line as a heading is harmless: only filler headings cause anything to be dropped.
    words = stripped.split()
    if len(words) <= _MAX_SHORT_HEADING_WORDS and stripped[0].isupper() and not stripped.endswith((".", ",", ";", "!", "?")):
        return stripped
    return None


def _is_filler(heading: str, company: str) -> bool:
    about_company = bool(company) and heading.lower() == f"about {company.lower()}"
    return bool(_DROP_HEADING.search(heading)) or about_company


def split_sections(text: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        found = _heading(line)
        if found is None:
            body.append(line)
            continue
        sections.append((heading, "\n".join(body).strip()))
        heading, body = found, []
    sections.append((heading, "\n".join(body).strip()))
    return [(h, b) for h, b in sections if h or b]


def trim_description(description: str | None, cap: int, company: str = "") -> str:
    """Drop company history, culture, equal-opportunity and privacy sections; keep everything else."""
    text = to_text(description)
    kept = [
        f"## {h}\n{b}" if h else b
        for h, b in split_sections(text)
        if not (h and _is_filler(h, company))
    ]
    trimmed = "\n\n".join(kept)
    if len(trimmed) < MIN_KEPT_SHARE * len(text):
        trimmed = text  # dropping most of a posting means a heading was misread
    return trimmed[:cap]


def load_posting(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    """A job's primary sighting as the plain posting the model reads."""
    row = conn.execute(
        "SELECT s.title, s.company_raw, s.location_raw, s.source_fields, s.description "
        "FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    return {
        "title": row["title"],
        "company": row["company_raw"],
        "location": row["location_raw"],
        "source_fields": db.loads(row["source_fields"], {}),
        "description": row["description"],
    }


def format_posting(posting: dict[str, Any], cap: int) -> str:
    """Title, company, location and source fields untouched, then the trimmed description."""
    fields = {k: v for k, v in (posting.get("source_fields") or {}).items() if v not in (None, "", [], {})}
    header = [
        f"Title: {posting['title']}",
        f"Company: {posting.get('company') or 'not given'}",
        f"Location: {posting.get('location') or 'not given'}",
    ]
    if fields:
        header.append(f"Source fields: {json.dumps(fields, ensure_ascii=False)}")
    body = trim_description(posting.get("description"), cap, posting.get("company") or "")
    return "\n".join(header) + "\n\n" + body


def posting_for_prompt(conn: sqlite3.Connection, job_id: int, cap: int) -> str:
    return format_posting(load_posting(conn, job_id), cap)


# --- extraction -----------------------------------------------------------------------


def extract_posting(llm: LLM, cfg: Config, posting: dict[str, Any], job_id: int | None = None) -> Facts:
    text = format_posting(posting, cap=int(cfg.llm["input_cap_chars"]))
    messages = [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": text}]
    return drop_unsupported_numbers(llm.chat_json("extract", job_id, messages, Facts), text)


# --- checking the model's numbers against the posting ------------------------------------

_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|K|千|万|m|M|million|mil)?(?![a-zA-Z])")
_MULTIPLIERS = {"k": 1e3, "千": 1e3, "万": 1e4, "m": 1e6, "million": 1e6, "mil": 1e6}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def numbers_in(text: str) -> list[float]:
    """Every number written in the text, with "120k", "120,000", "800万" and "three" read as values."""
    values = []
    for digits, suffix in _NUMBER.findall(text):
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        values.append(value * _MULTIPLIERS.get((suffix or "").lower(), 1.0))
    values += [n for word, n in _NUMBER_WORDS.items() if re.search(rf"\b{word}\b", text, re.IGNORECASE)]
    return values


def _written(value: float | None, numbers: list[float]) -> bool:
    return value is not None and any(abs(value - n) <= 0.01 * max(abs(value), 1.0) for n in numbers)


def drop_unsupported_numbers(facts: Facts, posting_text: str) -> Facts:
    """Clear any pay, years or office-days figure that the posting never states.

    Small models fill numbers in from nothing; a figure that appears nowhere in the text they
    read is a guess, and guessed pay would put a job at the top of the list.
    """
    numbers = numbers_in(posting_text)
    update: dict[str, Any] = {}
    if not (_written(facts.pay_min, numbers) or _written(facts.pay_max, numbers)):
        update |= {"pay_min": None, "pay_max": None, "pay_currency": None, "pay_period": None}
    if facts.years_required is not None and not _written(facts.years_required, numbers):
        update["years_required"] = None
    if facts.office_days_per_week is not None and not _written(facts.office_days_per_week, numbers):
        update["office_days_per_week"] = None
    return facts.model_copy(update=update) if update else facts


def extract(conn: sqlite3.Connection, llm: LLM, cfg: Config, job_id: int) -> Facts:
    return extract_posting(llm, cfg, load_posting(conn, job_id), job_id)
