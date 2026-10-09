from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import config as config_module
from jobhunt import db
from jobhunt.config import Config, Profile
from jobhunt.rules import Gazetteer, Place
from jobhunt.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]

JAPAN = Place("Japan", "JP", "", 125_000_000, "country")
UNITED_STATES = Place("United States", "US", "", 330_000_000, "country")
UNITED_KINGDOM = Place("United Kingdom", "GB", "", 67_000_000, "country")
GERMANY = Place("Germany", "DE", "", 83_000_000, "country")

CITIES = [
    Place("Tokyo", "JP", "Tokyo", 8_336_599, "city"),
    Place("Shibuya", "JP", "Tokyo", 224_533, "city"),
    Place("Osaka", "JP", "Osaka", 2_592_413, "city"),
    Place("Kyoto", "JP", "Kyoto", 1_459_640, "city"),
    Place("London", "GB", "England", 8_961_989, "city"),
    Place("San Francisco", "US", "California", 864_816, "city"),
    Place("Berlin", "DE", "Berlin", 3_426_354, "city"),
]


@pytest.fixture(scope="session")
def gaz() -> Gazetteer:
    codes = {"JP": JAPAN, "JPN": JAPAN, "US": UNITED_STATES, "USA": UNITED_STATES,
             "GB": UNITED_KINGDOM, "GBR": UNITED_KINGDOM, "DE": GERMANY, "DEU": GERMANY}
    return Gazetteer([JAPAN, UNITED_STATES, UNITED_KINGDOM, GERMANY, *CITIES], codes=codes)


@pytest.fixture
def profile() -> Profile:
    return Profile(
        years_experience=5,
        pay_floor_usd=100_000,
        commutable_cities=frozenset({"osaka", "kyoto"}),
        skills={"javascript": 3, "react": 3, "java": 2, "python": 1},
        forbidden_names=("Project Orion",),
        stand_in="a large-scale federal modernization program",
    )


@pytest.fixture
def cfg(tmp_path: Path, profile: Profile) -> Config:
    """The repository's config.toml with a test profile and a temporary data folder."""
    loaded = config_module.load(ROOT)
    profile_path = tmp_path / "profile.toml"  # the same skills as the profile fixture, so reloads change nothing
    profile_path.write_text(
        "[skills]\njavascript = 3  # keep this comment\nreact = 3\njava = 2\npython = 1\n", encoding="utf-8"
    )
    return dataclasses.replace(
        loaded, profile=profile, profile_path=profile_path, data_dir=tmp_path / "data",
        packets_dir=tmp_path / "packets", resumes_dir=tmp_path / "resumes",
    )


@pytest.fixture
def conn(cfg: Config):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    connection = db.connect(cfg.db_path)
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(cfg: Config, conn, gaz: Gazetteer) -> TestClient:
    """The web app over a database holding one kept job (#1, Backend Engineer at Acme, asking for Go)."""
    db.sync_sources(conn, ROOT / "sources.csv")
    now = db.utc_now()
    sighting = conn.execute(
        "INSERT INTO sightings (source_id, external_id, url, title, company_raw, location_raw, description, "
        "first_seen_at, last_seen_at) VALUES ('tokyodev', 'x', 'https://example.com/1', 'Backend Engineer', "
        "'Acme', 'Tokyo', '<p>Build things</p>', ?, ?)",
        (now, now),
    ).lastrowid
    job = conn.execute(
        "INSERT INTO jobs (company_norm, title_norm, city_norm, primary_sighting_id, stage, facts, score, "
        "score_parts, reason_line, badges, first_seen_at, listed_at) VALUES ('acme', 'backend engineer', 'tokyo', "
        "?, 'kept', ?, 72.5, ?, 'remote', '[\"unicorn\"]', ?, ?)",
        (sighting, '{"work_mode": "remote", "stack": ["Go"], "evidence": {"japanese": "English only"}}',
         '{"pay": 0.4, "location": 1.0}', now, now),
    ).lastrowid
    conn.execute("UPDATE sightings SET job_id = ? WHERE id = ?", (job, sighting))
    conn.execute("INSERT INTO llm_log (kind, job_id, seconds, ok, at) VALUES ('extract', ?, 65.0, 1, ?)", (job, now))
    db.set_activity(conn, "model", f"extracting #{job} Backend Engineer")
    return TestClient(create_app(cfg, gaz))
