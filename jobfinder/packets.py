"""Application packets for jobs the candidate marks apply. Every application is submitted by hand.

Resume choice and the gap list are code; the cover letter and screening answers are
written by the model from the posting, the job's facts, the resume and profile.toml.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from jobfinder import db
from jobfinder.config import Config
from jobfinder.llm import LLM, posting_for_prompt
from jobfinder.rules import japanese_share
from jobfinder.score import canonical_skill, skill_level

LETTER_MAX_WORDS = 250


class PacketError(Exception):
    pass


class Answer(BaseModel):
    question: str
    answer: str


class ScreeningAnswers(BaseModel):
    questions: list[Answer]


@dataclass(frozen=True)
class Packet:
    resume_file: str
    letter: str
    answers: list[Answer]
    gaps: list[str]


def choose_resume(facts: dict, cfg: Config) -> Path:
    """The frontend file for React and JavaScript, the backend file for Java, the general file otherwise."""
    by_language = cfg.packets["resume_by_language"]
    language = canonical_skill(facts.get("primary_language") or "")
    stack = {canonical_skill(s) for s in facts.get("stack") or []}
    name = by_language.get(language)
    if name is None and "react" in stack:
        name = by_language.get("react")
    path = cfg.resumes_dir / (name or cfg.packets["default_resume"])
    if not path.exists():
        raise PacketError(f"resume file missing: {path}")
    return path


def gap_list(facts: dict, cfg: Config) -> list[str]:
    profile = cfg.profile
    gaps = [f"{item}: not in profile" for item in facts.get("stack") or [] if not skill_level(item, profile)]
    years = facts.get("years_required")
    if years is not None and years > profile.years_experience:
        gaps.append(f"experience: {years:g} years asked, {profile.years_experience:g} held "
                    f"({years - profile.years_experience:g} short)")
    return gaps


def _rules_text(cfg: Config) -> str:
    return (
        "Rules: write in English only. Use only facts from the resume and the profile below; invent nothing. "
        f"Never name the candidate's previous project or the agency it served; call it {cfg.profile.stand_in}."
    )


def _profile_text(cfg: Config) -> str:
    profile = cfg.profile
    skills = ", ".join(f"{name} (level {level} of 3)" for name, level in profile.skills.items())
    return f"Profile: {profile.years_experience:g} years of professional experience. Skills: {skills or 'not set'}."


def _letter_problems(letter: str, cfg: Config) -> list[str]:
    problems = []
    if len(letter.split()) > LETTER_MAX_WORDS:
        problems.append(f"it has {len(letter.split())} words; the limit is {LETTER_MAX_WORDS}")
    if japanese_share(letter) > 0:
        problems.append("it contains Japanese; write English only")
    for name in cfg.profile.forbidden_names:
        if name.lower() in letter.lower():
            problems.append(f"it names '{name}'; call it {cfg.profile.stand_in}")
    return problems


def _scrub(text: str, cfg: Config) -> str:
    """The last guard: any forbidden name left in model text becomes the stand-in."""
    for name in cfg.profile.forbidden_names:
        text = re.sub(re.escape(name), cfg.profile.stand_in, text, flags=re.IGNORECASE)
    return text


def make_packet(conn: sqlite3.Connection, llm: LLM, cfg: Config, job_id: int) -> Packet:
    job = conn.execute(
        "SELECT j.facts, s.title, s.company_raw, s.url FROM jobs j "
        "JOIN sightings s ON s.id = j.primary_sighting_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise PacketError(f"no job {job_id}")
    facts = db.loads(job["facts"], {}) or {}
    resume_path = choose_resume(facts, cfg)
    resume = resume_path.read_text(encoding="utf-8")
    posting = posting_for_prompt(conn, job_id, cap=int(cfg.llm["input_cap_chars"]))
    context = f"{_rules_text(cfg)}\n\n{_profile_text(cfg)}\n\n=== Resume ===\n{resume}\n\n=== Posting ===\n{posting}"

    messages = [{
        "role": "user",
        "content": f"{context}\n\nWrite a cover letter for this job, {LETTER_MAX_WORDS} words or fewer. "
                   "Plain text, no placeholders.",
    }]
    letter = llm.chat_text("packet", job_id, messages)
    problems = _letter_problems(letter, cfg)
    if problems:
        messages += [{"role": "assistant", "content": letter},
                     {"role": "user", "content": f"Rewrite it: {'; '.join(problems)}."}]
        letter = llm.chat_text("packet", job_id, messages)

    screening = llm.chat_json("packet", job_id, [{
        "role": "user",
        "content": f"{context}\n\nList every screening question printed in the posting itself and answer each "
                   'from the resume and profile. If the posting prints none, return {"questions": []}. '
                   'Reply as JSON: {"questions": [{"question": ..., "answer": ...}]}.',
    }], ScreeningAnswers)

    packet = Packet(
        resume_file=resume_path.name,
        letter=_scrub(letter, cfg),
        answers=[Answer(question=a.question, answer=_scrub(a.answer, cfg)) for a in screening.questions],
        gaps=gap_list(facts, cfg),
    )
    _save(conn, cfg, job_id, job, packet)
    return packet


def _save(conn: sqlite3.Connection, cfg: Config, job_id: int, job: sqlite3.Row, packet: Packet) -> None:
    lines = [
        f"# {job['title']} — {job['company_raw']}",
        "",
        f"Link: {job['url']}",
        f"Resume: {packet.resume_file}",
        "",
        "## Cover letter",
        "",
        packet.letter,
        "",
        "## Screening answers",
        "",
    ]
    lines += [f"**{a.question}**\n\n{a.answer}\n" for a in packet.answers] or ["None printed in the posting.", ""]
    lines += ["## Gaps", ""] + ([f"- {gap}" for gap in packet.gaps] or ["- none"])
    cfg.packets_dir.mkdir(parents=True, exist_ok=True)
    (cfg.packets_dir / f"{job_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO packets (job_id, resume_file, letter, answers, gaps, made_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (job_id) DO UPDATE SET resume_file = excluded.resume_file, letter = excluded.letter, "
        "answers = excluded.answers, gaps = excluded.gaps, made_at = excluded.made_at",
        (job_id, packet.resume_file, packet.letter, db.dumps([a.model_dump() for a in packet.answers]),
         db.dumps(packet.gaps), db.utc_now()),
    )
