"""Every page renders against a small real database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.web.app import create_app
from tests.conftest import ROOT


@pytest.fixture
def client(cfg, conn, gaz):
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


@pytest.mark.parametrize(
    "path",
    ["/new", "/all", "/removed", "/applied", "/health", "/activity", "/activity/live",
     "/data", "/data?table=sightings&q=Acme", "/data?table=fetch_log", "/jobs/1",
     "/rows?view=all&after=2000-01-01", "/skills", "/skills?scope=all", "/skills/go"],
)
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]


def test_job_page_shows_facts_and_calls(client):
    page = client.get("/jobs/1").text
    assert "Backend Engineer" in page and "English only" in page and "65.0 s" in page


def test_activity_shows_the_running_worker(client):
    assert "extracting #1 Backend Engineer" in client.get("/activity/live").text


def test_a_long_run_is_not_stale_while_its_worker_reports(client, conn):
    conn.execute("INSERT INTO activity (worker, doing, since) VALUES ('run:process', 'model worker', "
                 "'2000-01-01T00:00:00+00:00')")
    page = client.get("/activity/live").text
    assert "run:process" in page and "may have stopped" not in page  # the model note is fresh

    conn.execute("UPDATE activity SET since = '2000-01-01T00:00:00+00:00'")
    assert "may have stopped" in client.get("/activity/live").text  # nothing has reported in years


def test_skills_counted_and_shown_on_tiles(client):
    skills_page = client.get("/skills").text
    assert ">Go</a>" in skills_page and "1 across 1 jobs" in skills_page
    assert 'class="chip' in client.get("/all").text  # the job tile lists its skills
    assert "Backend Engineer" in client.get("/skills/GO").text  # any spelling finds the jobs


def test_rating_sets_and_toggles_off(client):
    headers = {"X-Requested-With": "fetch"}
    assert client.post("/jobs/1/rating", data={"rating": "green"}, headers=headers).json() == {"rating": "green"}
    assert 'data-rating="green"' in client.get("/all").text  # still listed, now marked
    assert client.post("/jobs/1/rating", data={"rating": "green"}, headers=headers).json() == {"rating": None}
    assert client.post("/jobs/1/rating", data={"rating": "purple"}, headers=headers).status_code == 400


def test_status_answers_the_tile_script_with_fresh_counts(client):
    headers = {"X-Requested-With": "fetch"}
    reply = client.post("/jobs/1/status", data={"status": "not_interested"}, headers=headers).json()
    assert reply["status"] == "not_interested" and reply["counts"]["All open"] == 0
    assert "Backend Engineer" not in client.get("/all").text


def test_clearing_a_status_puts_the_job_back(client):
    headers = {"X-Requested-With": "fetch"}
    reply = client.post("/jobs/1/status", data={"status": "not_interested"}, headers=headers).json()
    assert reply["counts"]["Not interested"] == 1 and reply["counts"]["Applied"] == 0
    assert "Backend Engineer" not in client.get("/applied").text
    assert "clear status" in client.get("/not-interested").text
    reply = client.post("/jobs/1/status", data={"status": "none"}, headers=headers).json()
    assert reply["counts"]["All open"] == 1 and reply["counts"]["Not interested"] == 0
    assert "Backend Engineer" in client.get("/all").text


def test_skill_levels_edit_the_profile_file(client, cfg):
    headers = {"X-Requested-With": "fetch"}
    reply = client.post("/skills/level", data={"name": "Go", "level": "2"}, headers=headers).json()
    assert reply == {"name": "go", "level": 2, "rescored": 1}
    assert cfg.profile.skills["go"] == 2  # the running app sees it at once

    client.post("/skills/level", data={"name": "JS", "level": "2"}, headers=headers)  # any spelling finds the entry
    client.post("/skills/level", data={"name": "python", "level": "0"}, headers=headers)  # 0 removes it
    text = cfg.profile_path.read_text(encoding="utf-8")
    assert "javascript = 2  # keep this comment" in text
    assert "python" not in text and "go = 2" in text

    page = client.get("/skills").text
    assert "In your profile, in none of these jobs" in page and ">react<" in page
    assert client.post("/skills/level", data={"name": "go", "level": "7"}).status_code == 400


def test_unknown_table_is_404(client):
    assert client.get("/data?table=sqlite_master").status_code == 404
