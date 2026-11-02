"""The JSON API under /api, against a database holding one kept job (the client fixture: #1, Backend
Engineer at Acme, asking for Go)."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import get_args

from jobfinder import db, score
from jobfinder.web import api, filters


def titles(client, **params) -> list[str]:
    """The titles on the jobs table under these filters (each a list of the options that are on)."""
    return [job["title"] for job in client.get("/api/jobs", params=params).json()["jobs"]]


# --- the jobs table ---


def test_a_plain_table_is_the_open_list(client, conn):
    reply = client.get("/api/jobs").json()
    assert [j["title"] for j in reply["jobs"]] == ["Backend Engineer"] and reply["total"] == 1
    on = {f["key"]: [o["key"] for o in f["options"] if o["on"]] for f in reply["filters"]}
    assert on == {"rating": ["green", "yellow", "red", "grey"], "japanese": ["not_japanese"], "status": ["none"],
                  "removed": ["kept"], "source": ["tokyodev"]}


def test_options_within_a_filter_add_up_and_filters_narrow(client, conn):
    conn.execute("UPDATE jobs SET rating = 'red' WHERE id = 1")
    assert titles(client, rating=["green"]) == []
    assert titles(client, rating=["green", "red"]) == ["Backend Engineer"]
    assert titles(client, rating=["red"], status=["applied"]) == []  # every filter must pass
    assert titles(client, rating=[""]) == []  # every option off
    assert titles(client, rating=["purple"]) == []  # an unknown option matches nothing


def test_the_source_filter_hides_jobs_seen_only_on_sources_turned_off(client, conn):
    assert titles(client, source=["tokyodev"]) == ["Backend Engineer"]
    assert titles(client, source=["hn"]) == []
    conn.execute("INSERT INTO sightings (source_id, external_id, url, title, first_seen_at, last_seen_at, job_id) "
                 "VALUES ('hn', 'y', 'https://example.com/2', 'Backend Engineer', '2026-10-01', '2026-10-01', 1)")
    assert titles(client, source=["hn"]) == ["Backend Engineer"]  # seen on HN too


def test_a_job_as_a_tile(client):
    job = client.get("/api/jobs").json()["jobs"][0]
    assert (job["title"], job["company"], job["status"], job["stage"]) == ("Backend Engineer", "Acme", "none", "kept")
    assert job["place"] == ""  # remote with no office city: the raw location is left out
    assert job["badges"] == ["unicorn"] and job["work_mode"] == "remote"
    assert job["skills"] == [{"name": "Go", "key": "go", "level": 0}]  # Go is not in the test profile


def test_a_card_has_pay_in_dollars_and_years_against_yours(client, conn):
    conn.execute("""UPDATE jobs SET facts = json_set(facts, '$.pay_min', 110000, '$.pay_max', 140000,
                    '$.pay_currency', 'USD', '$.pay_period', 'year', '$.years_required', 6,
                    '$.primary_language', 'JavaScript') WHERE id = 1""")
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["pay_usd"] == "$110k–$140k"
    assert job["years_asked"] == 6 and job["years_over"]  # the test profile has 5 years
    assert job["language_level"] == 3


def test_a_live_table_gets_only_jobs_listed_since(client):
    now = client.get("/api/jobs").json()["now"]
    assert titles(client, after=now) == []
    assert titles(client, after="2000-01-01") == ["Backend Engineer"]


def test_jobs_asking_for_a_skill_under_any_spelling(client):
    assert titles(client, skill="GO") == ["Backend Engineer"]
    assert titles(client, skill="rust") == []


def test_the_status_filter_names_every_status():
    assert list(filters.STATUS_LABELS) == list(get_args(api.Status))


# --- one job ---


def test_job_detail(client):
    job = client.get("/api/jobs/1").json()
    assert (job["title"], job["company"], job["stage"], job["score"]) == ("Backend Engineer", "Acme", "kept", 72.5)
    assert job["evidence"] == {"japanese": "English only"} and "evidence" not in job["facts"]
    assert job["facts"]["stack"] == ["Go"] and job["score_parts"] == {"pay": 0.4, "location": 1.0}
    assert [s["primary"] for s in job["sightings"]] == [True] and job["sightings"][0]["source"] == "TokyoDev"
    assert job["model_calls"][0]["seconds"] == 65.0 and job["description"] == "Build things"
    assert job["dedupe_key"] == ["acme", "backend engineer", "tokyo"]


def test_missing_job_is_404(client):
    assert client.get("/api/jobs/99").status_code == 404


# --- ratings and statuses ---


def test_rating_sets_and_toggles_off(client):
    assert client.post("/api/jobs/1/rating", json={"rating": "green"}).json() == {"rating": "green"}
    assert client.get("/api/jobs").json()["jobs"][0]["rating"] == "green"
    assert client.post("/api/jobs/1/rating", json={"rating": "green"}).json() == {"rating": None}
    assert client.post("/api/jobs/1/rating", json={"rating": "purple"}).status_code == 422
    assert client.post("/api/jobs/99/rating", json={"rating": "red"}).status_code == 404


def test_a_status_moves_the_job_between_filters(client):
    reply = client.post("/api/jobs/1/status", json={"status": "not_interested"}).json()
    assert reply["status"] == "not_interested" and reply["status_at"]
    assert titles(client) == [] and titles(client, status=["not_interested"]) == ["Backend Engineer"]

    reply = client.post("/api/jobs/1/status", json={"status": "none"}).json()  # cleared: back in the open list
    assert reply["status_at"] is None and titles(client) == ["Backend Engineer"]


def test_applied_sets_a_follow_up_date(client):
    reply = client.post("/api/jobs/1/status", json={"status": "applied"}).json()
    assert reply["follow_up_on"] and reply["status_at"]
    assert titles(client, status=["applied"]) == ["Backend Engineer"]


def test_an_undo_puts_the_earlier_status_back_as_it_was(client):
    client.post("/api/jobs/1/status", json={"status": "interviewing"})
    earlier = {"status": "applied", "status_at": "2026-10-01T09:00:00Z", "follow_up_on": "2026-10-15"}
    assert client.post("/api/jobs/1/status", json=earlier).json() == earlier  # not stamped now


def test_recently_changed_puts_the_last_job_changed_first(client, conn):
    worse = _add_job(conn, "Globex", "globex", "Platform Engineer")
    conn.execute("UPDATE jobs SET stage = 'kept', score = 1 WHERE id = ?", (worse,))
    assert titles(client) == ["Backend Engineer", "Platform Engineer"]  # best first
    client.post(f"/api/jobs/{worse}/rating", json={"rating": "green"})
    assert titles(client, sort="changed") == ["Platform Engineer", "Backend Engineer"]


# --- removed jobs ---


def test_restore_judges_a_read_job_again(client, conn):
    conn.execute("UPDATE jobs SET stage = 'removed', removal_rule = 'Not payroll', removal_quote = 'Contract', "
                 "facts = json_set(facts, '$.employment', 'contract') WHERE id = 1")
    assert titles(client) == []  # the plain table shows kept jobs only
    removed = client.get("/api/jobs", params={"removed": ["Not payroll"]}).json()
    assert [j["removal_rule"] for j in removed["jobs"]] == ["Not payroll"]
    rules = next(f for f in removed["filters"] if f["key"] == "removed")["options"]
    assert [(o["key"], o["on"]) for o in rules] == [("kept", False), ("Not payroll", True)]  # one option per rule

    assert client.post("/api/jobs/1/restore").json()["stage"] == "kept"  # the rule never removes it again
    assert titles(client) == ["Backend Engineer"]
    assert client.post("/api/jobs/1/restore").status_code == 404  # no longer removed


# --- the blacklist ---


def _add_job(conn, company: str, company_norm: str, title: str) -> int:
    """A job the model has not read yet."""
    now = db.utc_now()
    sighting = conn.execute(
        "INSERT INTO sightings (source_id, external_id, url, title, company_raw, location_raw, first_seen_at, "
        "last_seen_at) VALUES ('tokyodev', ?, ?, ?, ?, 'Tokyo', ?, ?)",
        (title, f"https://example.com/{title}", title, company, now, now),
    ).lastrowid
    job = conn.execute(
        "INSERT INTO jobs (company_norm, title_norm, city_norm, primary_sighting_id, stage, first_seen_at) "
        "VALUES (?, ?, 'tokyo', ?, 'queued', ?)",
        (company_norm, title.lower(), sighting, now),
    ).lastrowid
    conn.execute("UPDATE sightings SET job_id = ? WHERE id = ?", (job, sighting))
    return job


def test_blacklisting_an_employer_hides_its_jobs_until_it_is_taken_off(client, conn):
    unread = _add_job(conn, "Acme", "acme", "Frontend Engineer")
    other = _add_job(conn, "Globex", "globex", "Platform Engineer")

    assert client.post("/api/jobs/1/blacklist").json() == {"company": "Acme", "company_norm": "acme", "hidden": 2}
    stages = dict(conn.execute("SELECT id, stage || ':' || COALESCE(removal_rule, '') FROM jobs").fetchall())
    assert stages == {1: "code_removed:blacklisted", unread: "code_removed:blacklisted", other: "queued:"}
    assert titles(client) == []
    assert [(e["company"], e["hidden"]) for e in client.get("/api/blacklist").json()] == [("Acme", 2)]

    assert client.post("/api/blacklist/remove", json={"company_norm": "acme"}).json() == {"restored": 2}
    stages = dict(conn.execute("SELECT id, stage FROM jobs").fetchall())
    assert stages == {1: "kept", unread: "queued", other: "queued"}  # judged again from its facts, or queued
    assert titles(client) == ["Backend Engineer"]
    assert client.get("/api/blacklist").json() == []
    assert client.post("/api/blacklist/remove", json={"company_norm": "acme"}).status_code == 404


def test_a_job_without_an_employer_cannot_blacklist_one(client, conn):
    nameless = _add_job(conn, "", "", "Backend Engineer II")
    assert client.post(f"/api/jobs/{nameless}/blacklist").status_code == 400
    assert client.post("/api/jobs/999/blacklist").status_code == 404


# --- the Japanese list ---


def test_a_job_requiring_japanese_is_left_out_unless_asked_for(client, conn, cfg, gaz):
    facts = {"japanese": "required", "stack": ["Go"], "evidence": {"japanese": "JLPT N1"}}
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts),))
    score.rescore_all(conn, cfg, gaz)
    assert titles(client, japanese=["japanese"]) == ["Backend Engineer"] and titles(client) == []

    # Japanese is checked last: a job another rule removes is just removed
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts | {"employment": "contract"}),))
    score.rescore_all(conn, cfg, gaz)
    assert tuple(conn.execute("SELECT stage, removal_rule FROM jobs WHERE id = 1").fetchone()) == ("removed", "Not payroll")


def test_moving_a_job_between_the_lists_by_hand(client, conn, cfg, gaz):
    def move(japanese: bool):
        return client.post("/api/jobs/1/japanese", json={"japanese": japanese})

    assert move(True).json()["stage"] == "japanese"
    assert titles(client, japanese=["japanese"]) == ["Backend Engineer"] and titles(client) == []
    assert client.get("/api/skills").json()["job_count"] == 0  # the skill filter follows the move
    score.rescore_all(conn, cfg, gaz)
    assert titles(client, japanese=["japanese"]) == ["Backend Engineer"]  # a rescore keeps it there

    conn.execute("UPDATE jobs SET facts = json_set(facts, '$.japanese', 'required') WHERE id = 1")
    assert move(False).json()["stage"] == "kept"
    score.rescore_all(conn, cfg, gaz)
    assert titles(client) == ["Backend Engineer"]  # whatever the model said


def test_only_listed_jobs_move_between_the_lists(client, conn):
    conn.execute("UPDATE jobs SET stage = 'removed' WHERE id = 1")
    assert client.post("/api/jobs/1/japanese", json={"japanese": True}).status_code == 404
    assert client.post("/api/jobs/99/japanese", json={"japanese": True}).status_code == 404


# --- skills ---


def test_skills_lists_what_the_jobs_ask_for(client):
    page = client.get("/api/skills").json()
    assert page["job_count"] == 1 and [s["name"] for s in page["skills"]] == ["Go"]
    assert page["skills"][0]["level"] == 0  # Go is not in the test profile
    assert {s["name"] for s in page["unseen"]} == {"javascript", "react", "java", "python"}
    assert page["levels"] == [0, 1, 2, 3]


def test_skills_filters(client, conn):
    conn.execute("UPDATE jobs SET facts = json_set(facts, '$.japanese', 'required') WHERE id = 1")
    assert client.get("/api/skills").json()["job_count"] == 0  # Japanese jobs are left out
    assert client.get("/api/skills?japanese=true").json()["job_count"] == 1
    assert client.get("/api/skills?scope=everything").status_code == 422


def test_skill_level_saves_and_rescores(client, cfg):
    reply = client.post("/api/skills/level", json={"name": "Go", "level": 2}).json()
    assert reply == {"name": "go", "level": 2, "rescored": 1}
    assert cfg.profile.skills["go"] == 2  # the running app sees it at once
    assert client.get("/api/skills").json()["skills"][0]["level"] == 2
    assert client.post("/api/skills/level", json={"name": "go", "level": 7}).status_code == 400


def test_skill_level_edits_profile_toml_in_place(client, cfg):
    client.post("/api/skills/level", json={"name": "JS", "level": 2})  # any spelling finds the entry
    client.post("/api/skills/level", json={"name": "python", "level": 0})  # 0 removes it
    text = cfg.profile_path.read_text(encoding="utf-8")
    assert "javascript = 2  # keep this comment" in text and "python" not in text


# --- health ---


def source(client, source_id: str) -> dict:
    return next(s for s in client.get("/api/health").json()["sources"] if s["id"] == source_id)


def test_health_shows_each_source_and_turns_one_back_on(client, conn):
    conn.execute("UPDATE sources SET fail_streak = 99, last_tally = ? WHERE id = 'tokyodev'",
                 (json.dumps({"requests": 3, "parsed": 2, "new_jobs": 1, "errors": 0}),))
    tokyodev = source(client, "tokyodev")
    assert (tokyodev["requests"], tokyodev["parsed"], tokyodev["new_jobs"]) == (3, 2, 1)
    assert tokyodev["flags"] == ["disabled"] and tokyodev["average"] is None  # no nightly runs yet

    assert client.post("/api/sources/tokyodev/enable").status_code == 204
    assert source(client, "tokyodev")["flags"] == []
    assert client.post("/api/sources/nowhere/enable").status_code == 404


def test_health_model_and_backup(client):
    health = client.get("/api/health").json()
    assert health["queue"] == 0 and health["last_run"] is None and health["backup"]["at"] is None


# --- activity ---


def test_activity_shows_the_running_worker_and_the_feed(client):
    activity = client.get("/api/activity").json()
    assert [(w["worker"], w["doing"], w["stale"]) for w in activity["running"]] == [
        ("model", "extracting #1 Backend Engineer", False)]
    assert activity["feed"][0] == {**activity["feed"][0], "kind": "model", "detail": "#1", "what": "ok", "bad": False}
    assert activity["progress"]["queue"] == 0


def test_a_long_run_is_not_stale_while_its_worker_reports(client, conn):
    conn.execute("INSERT INTO activity (worker, doing, since) VALUES ('run:process', 'model worker', "
                 "'2000-01-01T00:00:00+00:00')")
    stale = {w["worker"]: w["stale"] for w in client.get("/api/activity").json()["running"]}
    assert stale["run:process"] is False  # the model note is fresh

    conn.execute("UPDATE activity SET since = '2000-01-01T00:00:00+00:00'")
    assert all(w["stale"] for w in client.get("/api/activity").json()["running"])  # nothing reported in years


def test_a_killed_process_leaves_no_running_note(client, conn):
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    conn.execute("INSERT INTO activity (worker, doing, since, pid) VALUES ('run:nightly', 'nightly run', ?, ?)",
                 (db.utc_now(), ended.pid))
    db.set_activity(conn, "run:process", "model worker")  # this process: alive
    running = [w["worker"] for w in client.get("/api/activity").json()["running"]]
    assert "run:nightly" not in running and "run:process" in running


# --- the model worker's controls ---


def test_the_worker_reads_as_stopped_when_none_is_running(client):
    assert client.get("/api/model").json() == {"state": "stopped", "doing": None}


def test_a_pause_waits_for_the_current_task(client, conn):
    db.set_activity(conn, "run:process", "model worker, always on")  # this process stands in for the worker
    db.set_activity(conn, "model", "extracting #1 Backend Engineer")
    assert client.get("/api/model").json()["state"] == "running"
    assert client.post("/api/model/pause").json() == {"state": "pausing", "doing": "extracting #1 Backend Engineer"}
    db.set_activity(conn, "model", "paused")
    assert client.get("/api/model").json()["state"] == "paused"


def test_stop_ends_the_worker_and_leaves_it_paused(client, conn):
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        conn.execute("INSERT INTO activity (worker, doing, since, pid) VALUES ('run:process', 'model worker', ?, ?)",
                     (db.utc_now(), worker.pid))
        assert client.post("/api/model/stop").json()["state"] == "stopped"
        assert worker.wait(timeout=10) != 0  # ended, not finished
        assert db.get_setting(conn, "model_paused") == "1"
        assert conn.execute("SELECT * FROM activity").fetchall() == []
    finally:
        worker.kill()


def test_play_starts_a_worker_only_when_none_is_running(client, conn, monkeypatch):
    started = []
    monkeypatch.setattr("jobfinder.processes.start_detached", lambda args, cwd: started.append(args))
    db.set_setting(conn, "model_paused", "1")
    client.post("/api/model/play")
    assert db.get_setting(conn, "model_paused") == "0"
    assert len(started) == 1 and started[0][-1] == "process"

    db.set_activity(conn, "run:process", "model worker")  # now one is running: Play only unpauses
    client.post("/api/model/play")
    assert len(started) == 1


# --- data and packets ---


def test_data_pages_any_table(client):
    page = client.get("/api/data/jobs").json()
    assert page["total"] == 1 and "title_norm" in page["columns"] and page["tables"]["jobs"] == 1
    assert client.get("/api/data/sightings", params={"q": "Acme"}).json()["total"] == 1
    assert client.get("/api/data/sightings", params={"q": "Nobody"}).json()["rows"] == []
    assert client.get("/api/data/sqlite_master").status_code == 404  # only the listed tables
    assert client.get("/api/data/jobs", params={"page": 0}).status_code == 422


def test_packet(client, cfg):
    assert client.get("/api/packets/1").status_code == 404
    cfg.packets_dir.mkdir(parents=True, exist_ok=True)
    (cfg.packets_dir / "1.md").write_text("# Packet for #1", encoding="utf-8")
    assert client.get("/api/packets/1").json() == {"job_id": 1, "text": "# Packet for #1"}


# --- the rest ---


def test_docs_describe_the_api(client):
    assert "/jobs" in client.get("/api/openapi.json").json()["paths"]
    assert client.get("/api/docs").status_code == 200
