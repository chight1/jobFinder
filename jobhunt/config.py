"""Reads config.toml (settings, weights, schedule) and profile.toml (the candidate's facts)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Profile:
    years_experience: float
    pay_floor_usd: float
    commutable_cities: frozenset[str]  # lowercase
    skills: dict[str, int]  # lowercase skill name -> level 1..3
    forbidden_names: tuple[str, ...]
    stand_in: str


@dataclass(frozen=True)
class Config:
    root: Path
    data_dir: Path
    packets_dir: Path
    resumes_dir: Path
    sources_csv: Path
    schedule: dict[str, Any]
    fetch: dict[str, Any]
    llm: dict[str, Any]
    score: dict[str, Any]
    currency: dict[str, float]
    dedupe: dict[str, Any]
    list: dict[str, Any]
    packets: dict[str, Any]
    profile: Profile

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobhunt.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def geonames_dir(self) -> Path:
        return self.data_dir / "geonames"


def load(root: Path) -> Config:
    root = root.resolve()
    with open(root / "config.toml", "rb") as f:
        raw = tomllib.load(f)
    with open(root / "profile.toml", "rb") as f:
        prof = tomllib.load(f)

    paths = raw["paths"]
    packets_prof = prof.get("packets", {})
    profile = Profile(
        years_experience=float(prof["years_experience"]),
        pay_floor_usd=float(prof["pay_floor_usd"]),
        commutable_cities=frozenset(c.strip().lower() for c in prof.get("commutable_cities", [])),
        skills={name.strip().lower(): int(level) for name, level in prof.get("skills", {}).items()},
        forbidden_names=tuple(n for n in packets_prof.get("forbidden_names", []) if n.strip()),
        stand_in=packets_prof.get("stand_in", "a large-scale federal modernization program"),
    )
    return Config(
        root=root,
        data_dir=root / paths["data_dir"],
        packets_dir=root / paths["packets_dir"],
        resumes_dir=root / paths["resumes_dir"],
        sources_csv=root / paths["sources_csv"],
        schedule=raw["schedule"],
        fetch=raw["fetch"],
        llm=raw["llm"],
        score=raw["score"],
        currency={code.upper(): float(rate) for code, rate in raw["currency"].items()},
        dedupe=raw["dedupe"],
        list=raw["list"],
        packets=raw["packets"],
        profile=profile,
    )
