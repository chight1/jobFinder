"""The JSON API under /api, against a database holding one kept job (the client fixture: #1, Backend
Engineer at Acme, asking for Go)."""

from __future__ import annotations

import json

import pytest

from jobhunt import score


def titles(client, view: str, **params) -> list[str]:
    return [job["title"] for job in client.get("/api/jobs", params={"view": view, **params}).json()["jobs"]]


# --- job lists ---


@pytest.mark.parametrize("view", ["new", "all", "japanese", "removed", "applied", "not_interested"])
def test_every_list_answers(client, view):
    assert client.get("/api/jobs", params={"view": view}).status_code == 200


def test_a_job_as_a_tile(client):
    job = client.get("/api/jobs", params={"view": "all"}).json()["jobs"][0]
    assert (job["title"], job["company"], job["place"], job["status"]) == ("Backend Engineer", "Acme", "Tokyo", "none")
    assert job["badges"] == ["unicorn"] and job["work_mode"] == "remote"
    assert job["skills"] == [{"name": "Go", "key": "go", "level": 0}]  # Go is not in the test profile


def test_a_live_list_gets_only_jobs_listed_since(client):
    now = client.get("/api/jobs", params={"view": "all"}).json()["now"]
    assert titles(client, "all", after=now) == []
    assert titles(client, "all", after="2000-01-01") == ["Backend Engineer"]


def test_mark_all_seen_empties_new(client):
    assert titles(client, "new") == ["Backend Engineer"]
    assert client.post("/api/seen").status_code == 204
    assert titles(client, "new") == [] and titles(client, "all") == ["Backend Engineer"]


def test_unknown_view_is_refused(client):
    assert client.get("/api/jobs", params={"view": "everything"}).status_code == 422


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
    assert client.get("/api/jobs", params={"view": "all"}).json()["jobs"][0]["rating"] == "green"
    assert client.post("/api/jobs/1/rating", json={"rating": "green"}).json() == {"rating": None}
    assert client.post("/api/jobs/1/rating", json={"rating": "purple"}).status_code == 422
    assert client.post("/api/jobs/99/rating", json={"rating": "red"}).status_code == 404


def test_a_status_moves_the_job_between_lists(client):
    reply = client.post("/api/jobs/1/status", json={"status": "not_interested"}).json()
    assert reply["counts"]["All open"] == 0 and reply["counts"]["Not interested"] == 1
    assert titles(client, "all") == [] and titles(client, "not_interested") == ["Backend Engineer"]

    reply = client.post("/api/jobs/1/status", json={"status": "none"}).json()  # cleared: back in the lists
    assert reply["status_at"] is None and reply["counts"]["All open"] == 1
    assert titles(client, "all") == ["Backend Engineer"]


def test_applied_sets_a_follow_up_date(client):
    reply = client.post("/api/jobs/1/status", json={"status": "applied"}).json()
    assert reply["follow_up_on"] and reply["status_at"]
    assert titles(client, "applied") == ["Backend Engineer"]


# --- removed jobs ---


def test_restore_judges_a_read_job_again(client, conn):
    conn.execute("UPDATE jobs SET stage = 'removed', removal_rule = 'Not payroll', removal_quote = 'Contract', "
                 "facts = json_set(facts, '$.employment', 'contract') WHERE id = 1")
    removed = client.get("/api/jobs", params={"view": "removed", "rule": "Not payroll"}).json()
    assert [j["removal_rule"] for j in removed["jobs"]] == ["Not payroll"] and "Not payroll" in removed["rules"]

    reply = client.post("/api/jobs/1/restore").json()
    assert reply["stage"] == "kept" and reply["counts"]["Removed"] == 0  # the rule never removes it again
    assert client.post("/api/jobs/1/restore").status_code == 404  # no longer removed


# --- the Japanese list ---


def test_a_job_requiring_japanese_has_its_own_list(client, conn, cfg, gaz):
    facts = {"japanese": "required", "stack": ["Go"], "evidence": {"japanese": "JLPT N1"}}
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts),))
    score.rescore_all(conn, cfg, gaz)
    assert titles(client, "japanese") == ["Backend Engineer"] and titles(client, "all") == []
    assert client.get("/api/skills/go/jobs").json()["jobs"] == []  # skill lists leave Japanese jobs out
    assert len(client.get("/api/skills/go/jobs", params={"japanese": True}).json()["jobs"]) == 1  # unless asked

    # Japanese is checked last: a job another rule removes is just removed
    conn.execute("UPDATE jobs SET facts = ? WHERE id = 1", (json.dumps(facts | {"employment": "contract"}),))
    score.rescore_all(conn, cfg, gaz)
    assert tuple(conn.execute("SELECT stage, removal_rule FROM jobs WHERE id = 1").fetchone()) == ("removed", "Not payroll")


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


def test_skill_jobs_any_spelling(client):
    assert [j["title"] for j in client.get("/api/skills/GO/jobs").json()["jobs"]] == ["Backend Engineer"]


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


# --- the rest ---


def test_tab_counts(client):
    counts = client.get("/api/tabs").json()
    assert counts["All open"] == 1 and counts["Japanese"] == 0 and counts["Skills"] == 1


def test_docs_describe_the_api(client):
    assert "/jobs" in client.get("/api/openapi.json").json()["paths"]
    assert client.get("/api/docs").status_code == 200
