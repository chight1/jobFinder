"""The JSON API for the Vue app in web/, mounted at /api. Pages move to the Vue app one at a time;
until one has, its server-rendered version in app.py stays. Interactive docs at /api/docs.

No `from __future__ import annotations` here: FastAPI reads the route signatures, and Conn is local to
create_api, which string annotations could not reach."""

import sqlite3
from typing import Annotated, Callable, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from jobhunt import db, profile_file, score
from jobhunt.config import Config, reload_skills
from jobhunt.rules import Gazetteer
from jobhunt.web import inspect


class SkillRow(BaseModel):
    key: str = Field(description="The merged name every spelling maps to, e.g. 'javascript' for 'JS'")
    name: str = Field(description="The most common spelling")
    jobs: int = Field(description="How many of the counted jobs name it")
    share: float = Field(description="Fraction of the counted jobs that name it")
    main: int = Field(description="How many name it as the primary language")
    level: int = Field(description="Your level in profile.toml; 0 when it is not there")
    avg_score: float | None = Field(description="Average score of its jobs; null when none are scored")


class ProfileSkill(BaseModel):
    name: str
    level: int


class SkillsPage(BaseModel):
    skills: list[SkillRow]
    job_count: int = Field(description="How many jobs were counted")
    unseen: list[ProfileSkill] = Field(description="Skills in your profile that none of the counted jobs name")
    levels: list[int] = Field(description="The levels a skill can have; 0 takes it out of the profile")


class SkillLevel(BaseModel):
    name: str = Field(description="Any spelling; an entry already in the profile is changed in place")
    level: int


class SkillLevelSaved(BaseModel):
    name: str = Field(description="The name the skill is stored under in profile.toml")
    level: int
    rescored: int = Field(description="How many jobs were rescored with the new level")


def create_api(
    cfg: Config,
    gazetteer: Callable[[], Gazetteer],
    tab_counts: Callable[[sqlite3.Connection], dict[str, int]],
) -> FastAPI:
    """gazetteer loads GeoNames on first need; tab_counts is the top bar's, shared with the old pages."""
    api = FastAPI(title="jobhunt API", docs_url="/docs", redoc_url=None)

    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(connection)]

    @api.get("/tabs")
    def tabs(conn: Conn) -> dict[str, int]:
        """How many records each tab in the top bar holds."""
        return tab_counts(conn)

    @api.get("/skills")
    def skills(conn: Conn, scope: Literal["kept", "all"] = "kept", japanese: bool = False) -> SkillsPage:
        """Every skill the jobs name, most common first. scope "kept" counts the open jobs still in play,
        "all" every job the model has read; jobs that require Japanese count only with japanese=true."""
        reload_skills(cfg)  # pick up hand edits to profile.toml too
        stats = inspect.skill_stats(conn, cfg.profile, scope, japanese=japanese)
        return SkillsPage(
            skills=[SkillRow(**row) for row in stats["skills"]],
            job_count=stats["job_count"],
            unseen=[ProfileSkill(name=n, level=lvl) for n, lvl in inspect.unseen_skills(cfg.profile, stats)],
            levels=list(profile_file.LEVELS),
        )

    @api.post("/skills/level")
    def set_skill_level(body: SkillLevel, conn: Conn) -> SkillLevelSaved:
        """Change a skill's level in profile.toml (0 removes it), then rescore every job that has facts."""
        try:
            stored = profile_file.set_skill_level(cfg.profile_path, body.name, body.level)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        reload_skills(cfg)
        return SkillLevelSaved(name=stored, level=body.level, rescored=score.rescore_all(conn, cfg, gazetteer()))

    return api
