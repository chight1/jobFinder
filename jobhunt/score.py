"""Formula scoring of kept jobs, and the comparator that orders the top of the list.

The score is code over the job's facts and profile.toml, so a weight change in
config.toml re-sorts every job in seconds without a model call.
"""

from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from jobhunt import db
from jobhunt.config import Config, Profile

if TYPE_CHECKING:
    from jobhunt.llm import LLM
    from jobhunt.rules import Gazetteer

JST = ZoneInfo("Asia/Tokyo")
COMPARE_PROMPT_VERSION = "compare-1"

# --- pay ------------------------------------------------------------------------

_PERIOD_TO_YEAR = {"year": 1, "month": 12, "hour": 2000}


def annual_usd(
    pay_min: Any, pay_max: Any, currency: Any, period: Any, rates: dict[str, float]
) -> float | None:
    """Yearly US dollars for a listed pay range: the midpoint, converted. None when not listed."""
    amounts = [float(v) for v in (pay_min, pay_max) if isinstance(v, (int, float)) and v > 0]
    if not amounts:
        return None
    rate = rates.get(str(currency or "").upper())
    per_year = _PERIOD_TO_YEAR.get(str(period or "year").lower())
    if rate is None or per_year is None:
        return None
    return sum(amounts) / len(amounts) * per_year * rate


def _pay_part(usd: float | None, profile: Profile, cfg: dict[str, Any]) -> float:
    if usd is None:
        return cfg["unknown"]
    full, floor, zero, at_floor = cfg["full_usd"], profile.pay_floor_usd, cfg["zero_usd"], cfg["at_floor"]
    if usd >= full:
        return 1.0
    if usd <= zero:
        return 0.0
    if usd >= floor:
        return at_floor + (1.0 - at_floor) * (usd - floor) / (full - floor)
    return at_floor * (usd - zero) / (floor - zero)


# --- location -----------------------------------------------------------------------


def _location_part(facts: dict[str, Any], profile: Profile, gaz: Gazetteer, cfg: dict[str, Any]) -> float:
    from jobhunt.rules import is_commutable

    mode, city = facts.get("work_mode"), facts.get("office_city")
    if mode == "remote" and facts.get("open_to_japan_resident") != "no":
        return cfg["remote_or_commutable"]
    if not city:
        return cfg["unknown"]
    if is_commutable(city, profile, gaz):
        return cfg["remote_or_commutable"]
    if mode == "hybrid" and gaz.is_tokyo(gaz.lookup(city)):
        days = facts.get("office_days_per_week")
        if days is not None and days <= cfg["max_few_days"]:
            return cfg["tokyo_hybrid_few_days"]
        return cfg["tokyo_hybrid_many_days"]
    return cfg["unknown"]


# --- stack ----------------------------------------------------------------------------

_SKILL_ALIASES = {
    "js": "javascript", "ecmascript": "javascript", "ts": "typescript", "golang": "go",
    "nodejs": "node.js", "node": "node.js", "reactjs": "react", "react.js": "react",
    "csharp": "c#", "c sharp": "c#", "py": "python", "k8s": "kubernetes", "postgres": "postgresql",
}


def canonical_skill(name: str) -> str:
    key = name.strip().lower()
    return _SKILL_ALIASES.get(key, key)


def skill_level(name: str | None, profile: Profile) -> int | None:
    if not name:
        return None
    wanted = canonical_skill(name)
    for skill, level in profile.skills.items():
        if canonical_skill(skill) == wanted:
            return level
    return 0


def _stack_part(facts: dict[str, Any], profile: Profile, cfg: dict[str, Any]) -> float:
    level = skill_level(facts.get("primary_language"), profile)
    if level is None:
        return cfg["unknown"]
    return cfg["level"].get(str(level), cfg["not_in_profile"])


# --- seniority --------------------------------------------------------------------------


def _seniority_part(facts: dict[str, Any], profile: Profile, cfg: dict[str, Any]) -> float:
    if facts.get("seniority") == "staff_plus":
        return cfg["staff_plus"]
    years = facts.get("years_required")
    if years is None:
        return cfg["unknown"]
    extra = max(0.0, float(years) - profile.years_experience)
    return max(0.0, 1.0 - cfg["per_extra_year"] * extra)


# --- balance ---------------------------------------------------------------------------

_TZ_ABBREVIATIONS = {
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "GMT": "Europe/London", "BST": "Europe/London", "UK": "Europe/London",
    "CET": "Europe/Berlin", "CEST": "Europe/Berlin", "EET": "Europe/Helsinki",
    "IST": "Asia/Kolkata", "SGT": "Asia/Singapore", "HKT": "Asia/Hong_Kong",
    "KST": "Asia/Seoul", "JST": "Asia/Tokyo", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland", "UTC": "UTC",
}
_UTC_OFFSET = re.compile(r"^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_tz(name: str | None) -> tzinfo | None:
    if not name:
        return None
    name = name.strip()
    offset = _UTC_OFFSET.match(name)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        delta = timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3) or 0))
        return timezone(sign * delta)
    try:
        return ZoneInfo(_TZ_ABBREVIATIONS.get(name.upper(), name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def jst_window(overlap: dict[str, Any] | None) -> tuple[datetime, datetime] | None:
    """The overlap's start and end in Japan time, using today's date for daylight saving."""
    if not overlap:
        return None
    tz = resolve_tz(overlap.get("time_zone"))
    start_h, end_h = overlap.get("start_hour"), overlap.get("end_hour")
    if tz is None or start_h is None or end_h is None:
        return None
    midnight = datetime.combine(date.today(), time(), tz)
    start = midnight + timedelta(hours=float(start_h))
    end = midnight + timedelta(hours=float(end_h))
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(JST), end.astimezone(JST)


def overlap_in_jst(overlap: dict[str, Any] | None) -> list[tuple[float, float]] | None:
    """The overlap as hour intervals on the 0-24 Japan-time clock, split at midnight."""
    window = jst_window(overlap)
    if window is None:
        return None
    start, end = window
    from_h = start.hour + start.minute / 60
    to_h = from_h + (end - start).total_seconds() / 3600
    if to_h <= 24:
        return [(from_h, to_h)]
    return [(from_h, 24.0), (0.0, min(to_h - 24, 24.0))]


def _touches(intervals: list[tuple[float, float]], lo: float, hi: float) -> bool:
    return any(a < hi and b > lo for a, b in intervals)


def _balance_part(facts: dict[str, Any], cfg: dict[str, Any]) -> float:
    intervals = overlap_in_jst(facts.get("overlap"))
    if intervals is None:
        part = cfg["unknown"]
    elif _touches(intervals, 23, 24) or _touches(intervals, 0, 7):
        part = cfg["night"]
    elif _touches(intervals, 20, 23) or _touches(intervals, 7, 8):
        part = cfg["evening"]
    else:
        part = cfg["day"]
    if facts.get("overtime") == "heavy":
        part *= cfg["heavy_overtime"]
    if facts.get("on_call") == "yes":
        part *= cfg["on_call"]
    return part


# --- whole score ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    score: float
    parts: dict[str, float]
    reason_line: str
    badges: list[str]


def score_job(facts: dict[str, Any], cfg: Config, gaz: Gazetteer, *, stage: str = "kept") -> Scored:
    from jobhunt.rules import unclear_facts

    profile, weights = cfg.profile, cfg.score["weights"]
    usd = annual_usd(facts.get("pay_min"), facts.get("pay_max"), facts.get("pay_currency"),
                     facts.get("pay_period"), cfg.currency)
    parts = {
        "pay": _pay_part(usd, profile, cfg.score["pay"]),
        "location": _location_part(facts, profile, gaz, cfg.score["location"]),
        "stack": _stack_part(facts, profile, cfg.score["stack"]),
        "seniority": _seniority_part(facts, profile, cfg.score["seniority"]),
        "balance": _balance_part(facts, cfg.score["balance"]),
    }
    total = 100 * sum(weights[name] * value for name, value in parts.items())

    badges = []
    if _is_unicorn(facts, usd, profile, gaz):
        badges.append("unicorn")
    if stage == "extract_failed" or unclear_facts(facts):
        badges.append("check")
    if facts.get("employment") == "dispatch":
        badges.append("dispatch")
    return Scored(round(total, 1), parts, reason_line(facts, usd, profile), badges)


def _is_unicorn(facts: dict[str, Any], usd: float | None, profile: Profile, gaz: Gazetteer) -> bool:
    level = skill_level(facts.get("primary_language"), profile)
    return (
        facts.get("open_to_japan_resident") == "yes"
        and facts.get("japanese") != "required"
        and facts.get("employment") in ("payroll", "employer_of_record")
        and (facts.get("work_mode") == "remote" or gaz.is_kansai(gaz.lookup(facts.get("office_city"))))
        and usd is not None
        and usd >= profile.pay_floor_usd
        and level is not None
        and level >= 1
    )


def pay_text(facts: dict[str, Any], usd: float | None) -> str:
    """Pay in the posting's currency and in dollars, e.g. '9,000,000 JPY/year ($60k)'."""
    if usd is None:
        return "pay not listed"
    listed = "–".join(f"{v:,.0f}" for v in (facts.get("pay_min"), facts.get("pay_max")) if v)
    return f"{listed} {facts.get('pay_currency')}/{facts.get('pay_period') or 'year'} (${usd / 1000:,.0f}k)"


def reason_line(facts: dict[str, Any], usd: float | None, profile: Profile) -> str:
    """Work mode and place · pay · primary language and level · years · overlap in Japan time."""
    pieces = []
    place = facts.get("office_city")
    pieces.append(f"{facts.get('work_mode', '?')}{', ' + place if place else ''}")

    pieces.append(pay_text(facts, usd))

    language = facts.get("primary_language")
    if language:
        level = skill_level(language, profile)
        pieces.append(f"{language} (level {level})" if level else f"{language} (not in profile)")

    years = facts.get("years_required")
    if years is not None:
        pieces.append(f"{years:g} yrs asked / {profile.years_experience:g}")

    window = jst_window(facts.get("overlap"))
    if window:
        pieces.append(f"{window[0]:%H:%M}–{window[1]:%H:%M} JST")
    return " · ".join(pieces)


# --- applying scores to the database --------------------------------------------------------


def apply_facts(conn: sqlite3.Connection, job: sqlite3.Row, cfg: Config, gaz: Gazetteer) -> str:
    """Run the fact rules, then score a kept job. Returns the job's new stage."""
    from jobhunt.rules import fact_rules

    facts = db.loads(job["facts"], {})
    title = conn.execute(
        "SELECT title FROM sightings WHERE id = ?", (job["primary_sighting_id"],)
    ).fetchone()["title"]
    if job["stage"] != "extract_failed":
        removal = fact_rules(
            facts, title=title, profile=cfg.profile, gaz=gaz, restored=db.loads(job["restored_rules"], [])
        )
        if removal:
            conn.execute(
                "UPDATE jobs SET stage = 'removed', removal_rule = ?, removal_quote = ?, score = NULL "
                "WHERE id = ?",
                (removal.rule, removal.quote, job["id"]),
            )
            return "removed"
    stage = job["stage"] if job["stage"] == "extract_failed" else "kept"
    scored = score_job(facts, cfg, gaz, stage=stage)
    conn.execute(
        "UPDATE jobs SET stage = ?, removal_rule = NULL, removal_quote = NULL, score = ?, score_parts = ?, "
        "reason_line = ?, badges = ?, listed_at = COALESCE(listed_at, ?) WHERE id = ?",
        (stage, scored.score, db.dumps(scored.parts), scored.reason_line, db.dumps(scored.badges),
         db.utc_now(), job["id"]),
    )
    return stage


def rescore_all(conn: sqlite3.Connection, cfg: Config, gaz: Gazetteer) -> int:
    """Re-apply fact rules and the formula to every job that has facts. No model calls."""
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE stage IN ('kept', 'removed', 'extract_failed') AND facts IS NOT NULL"
    ).fetchall()
    with db.transaction(conn):
        for job in jobs:
            apply_facts(conn, job, cfg, gaz)
    return len(jobs)


# --- comparator --------------------------------------------------------------------------------


class Comparison(BaseModel):
    winner: str  # "A" or "B"
    why: str


def top_job_ids(conn: sqlite3.Connection, n: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE stage = 'kept' AND closed_at IS NULL AND status = 'none' "
        "ORDER BY score DESC, id LIMIT ?",
        (n,),
    ).fetchall()
    return [r["id"] for r in rows]


def pending_pairs(conn: sqlite3.Connection, n: int) -> list[tuple[int, int]]:
    """Pairs inside the current top n that the model has not judged yet."""
    top = sorted(top_job_ids(conn, n))
    if len(top) < 2:
        return []
    marks = ",".join("?" * len(top))
    done = {
        (r["job_a"], r["job_b"])
        for r in conn.execute(f"SELECT job_a, job_b FROM comparisons WHERE job_a IN ({marks})", top)
    }
    return [(a, b) for i, a in enumerate(top) for b in top[i + 1 :] if (a, b) not in done]


def ordered_top(conn: sqlite3.Connection, n: int) -> list[int]:
    """The top n by most pairwise wins; ties go to the higher score."""
    top = top_job_ids(conn, n)
    if not top:
        return []
    marks = ",".join("?" * len(top))
    wins = {job_id: 0 for job_id in top}
    for row in conn.execute(
        f"SELECT winner FROM comparisons WHERE job_a IN ({marks}) AND job_b IN ({marks})", top + top
    ):
        wins[row["winner"]] += 1
    rank = {job_id: i for i, job_id in enumerate(top)}  # top is already in score order
    return sorted(top, key=lambda j: (-wins[j], rank[j]))


def compare(conn: sqlite3.Connection, llm: LLM, cfg: Config, a: int, b: int) -> None:
    """Ask the model which of two jobs suits the candidate better; store the answer for good."""
    from jobhunt.llm import posting_for_prompt

    first, second = (a, b) if random.random() < 0.5 else (b, a)
    profile = cfg.profile
    skills = ", ".join(f"{name} (level {level})" for name, level in profile.skills.items()) or "not set"
    prompt = (
        "Two job postings follow. Decide which one is the better next job for this candidate.\n"
        f"Candidate: {profile.years_experience:g} years of professional experience; skills: {skills}; "
        f"pay floor ${profile.pay_floor_usd:,.0f} a year; lives in Japan; does not speak Japanese for work.\n"
        "Weigh pay, remote work or a short commute, fit with the candidate's skills, a realistic level, "
        "and working hours that suit Japan time.\n"
        'Answer with JSON: {"winner": "A" or "B", "why": one sentence}.\n\n'
        f"=== Job A ===\n{posting_for_prompt(conn, first, cap=3000)}\n\n"
        f"=== Job B ===\n{posting_for_prompt(conn, second, cap=3000)}"
    )
    answer = llm.chat_json("compare", first, [{"role": "user", "content": prompt}], Comparison)
    letter = answer.winner.strip().upper()[:1]
    if letter not in ("A", "B"):
        raise ValueError(f"comparator answered {answer.winner!r}")
    winner = first if letter == "A" else second
    low, high = sorted((a, b))
    conn.execute(
        "INSERT OR IGNORE INTO comparisons (job_a, job_b, winner, why, prompt_version, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (low, high, winner, answer.why.strip(), COMPARE_PROMPT_VERSION, db.utc_now()),
    )
