"""Every page renders against a small real database."""

from __future__ import annotations

import json

import pytest

from jobhunt import score


@pytest.mark.parametrize(
    "path",
    ["/new", "/all", "/removed", "/applied", "/health", "/activity", "/activity/live",
     "/data", "/data?table=sightings&q=Acme", "/data?table=fetch_log", "/jobs/1",
     "/rows?view=all&after=2000-01-01", "/skills/go", "/japanese", "/skills/go?japanese=1"],
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


def test_skills_shown_on_tiles(client):
    assert 'class="chip' in client.get("/all").text  # the job tile lists its skills
    assert "Backend Engineer" in client.get("/skills/GO").text  # any spelling finds the jobs


def test_a_job_requiring_japanese_has_its_own_tab(client, conn, cfg, gaz):
    facts = {"japanese": "required", "stack": ["Go"], "evidence": {"japanese": "JLPT N1"}}
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts),))
    score.rescore_all(conn, cfg, gaz)
    assert "Backend Engineer" in client.get("/japanese").text and "Backend Engineer" not in client.get("/all").text
    assert "Backend Engineer" not in client.get("/skills/go").text  # skill lists leave Japanese jobs out
    assert "Backend Engineer" in client.get("/skills/go?japanese=1").text  # unless asked

    # Japanese is checked last: a job another rule removes is just removed
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts | {"employment": "contract"}),))
    score.rescore_all(conn, cfg, gaz)
    assert tuple(conn.execute("SELECT stage, removal_rule FROM jobs WHERE id = 1").fetchone()) == ("removed", "Not payroll")


def test_rating_sets_and_toggles_off(client):
    assert client.post("/jobs/1/rating", data={"rating": "green"}).json() == {"rating": "green"}
    assert 'data-rating="green"' in client.get("/all").text  # still listed, now marked
    assert client.post("/jobs/1/rating", data={"rating": "green"}).json() == {"rating": None}
    assert client.post("/jobs/1/rating", data={"rating": "purple"}).status_code == 400


def test_status_answers_the_tile_script_with_fresh_counts(client):
    reply = client.post("/jobs/1/status", data={"status": "not_interested"}).json()
    assert reply["status"] == "not_interested" and reply["counts"]["All open"] == 0
    assert "Backend Engineer" not in client.get("/all").text


def test_clearing_a_status_puts_the_job_back(client):
    reply = client.post("/jobs/1/status", data={"status": "not_interested"}).json()
    assert reply["counts"]["Not interested"] == 1 and reply["counts"]["Applied"] == 0
    assert "Backend Engineer" not in client.get("/applied").text
    assert "clear status" in client.get("/not-interested").text
    reply = client.post("/jobs/1/status", data={"status": "none"}).json()
    assert reply["counts"]["All open"] == 1 and reply["counts"]["Not interested"] == 0
    assert "Backend Engineer" in client.get("/all").text


def test_unknown_table_is_404(client):
    assert client.get("/data?table=sqlite_master").status_code == 404
