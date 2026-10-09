"""The JSON API under /api, against the same one-job database as the page tests (the client fixture)."""

from __future__ import annotations


def test_skills_lists_what_the_jobs_ask_for(client):
    page = client.get("/api/skills").json()
    assert page["job_count"] == 1 and [s["name"] for s in page["skills"]] == ["Go"]
    assert page["skills"][0]["level"] == 0  # Go is not in the test profile
    assert {s["name"] for s in page["unseen"]} == {"javascript", "react", "java", "python"}
    assert page["levels"] == [0, 1, 2, 3]


def test_skills_filters(client, conn):
    conn.execute("""UPDATE jobs SET facts = json_set(facts, '$.japanese', 'required') WHERE id = 1""")
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


def test_tab_counts(client):
    counts = client.get("/api/tabs").json()
    assert counts["All open"] == 1 and counts["Japanese"] == 0


def test_docs_describe_the_api(client):
    assert "/skills" in client.get("/api/openapi.json").json()["paths"]
    assert client.get("/api/docs").status_code == 200
