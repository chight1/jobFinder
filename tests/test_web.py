"""The pages still server-rendered render against a small real database (the client fixture)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "path",
    ["/health", "/activity", "/activity/live", "/data", "/data?table=sightings&q=Acme", "/data?table=fetch_log"],
)
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]


def test_home_is_the_vue_app(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/app/new"


def test_activity_shows_the_running_worker(client):
    assert "extracting #1 Backend Engineer" in client.get("/activity/live").text


def test_a_long_run_is_not_stale_while_its_worker_reports(client, conn):
    conn.execute("INSERT INTO activity (worker, doing, since) VALUES ('run:process', 'model worker', "
                 "'2000-01-01T00:00:00+00:00')")
    page = client.get("/activity/live").text
    assert "run:process" in page and "may have stopped" not in page  # the model note is fresh

    conn.execute("UPDATE activity SET since = '2000-01-01T00:00:00+00:00'")
    assert "may have stopped" in client.get("/activity/live").text  # nothing has reported in years


def test_unknown_table_is_404(client):
    assert client.get("/data?table=sqlite_master").status_code == 404
