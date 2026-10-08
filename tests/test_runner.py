"""The runner against a real SQLite file: new jobs, code removals, attachments and closures."""

from __future__ import annotations

import time

from jobhunt import db
from jobhunt.collectors import Summary
from jobhunt.collectors.runner import Runner, Shared
from jobhunt.dedupe import AgencyIndex
from tests.conftest import ROOT
from tests.test_collectors import FakeFetcher

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _job(job_id: int, title: str, location: str) -> dict:
    return {"id": job_id, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}", "title": title,
            "company_name": "Acme", "location": {"name": location}}


def _setup(conn, cfg, gaz, responses):
    db.sync_sources(conn, ROOT / "sources.csv")
    conn.execute("INSERT INTO companies (ats, slug, found_via) VALUES ('greenhouse', 'acme', 'test')")
    shared = Shared(
        cfg=cfg, gaz=gaz, fetcher=FakeFetcher(responses), deadline=time.time() + 60,  # type: ignore[arg-type]
        aliases={}, source_kinds={"greenhouse": "employer"}, agency_index=AgencyIndex(lambda: []),
    )
    return Runner(shared, conn)


def _run(runner, conn):
    source = conn.execute("SELECT * FROM sources WHERE id = 'greenhouse'").fetchone()
    board = conn.execute("SELECT * FROM companies WHERE slug = 'acme'").fetchone()
    return runner.run_board(source, board)


def test_sync_sources_splits_boards_from_sources(conn):
    db.sync_sources(conn, ROOT / "sources.csv")
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 51
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 9
    rakuten = conn.execute("SELECT * FROM companies WHERE ats = 'workday'").fetchone()
    assert db.loads(rakuten["extra"])["site"] == "RakutenInc"


def test_new_jobs_removed_queued_then_closed(conn, cfg, gaz):
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", "Tokyo"), _job(2, "Backend Engineer", "London, UK")]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs in Go.&lt;/p&gt;"},
    }
    runner = _setup(conn, cfg, gaz, responses)

    tally = _run(runner, conn)
    assert (tally.parsed, tally.new_jobs, tally.code_removed) == (2, 2, 1)
    stages = dict(conn.execute(
        "SELECT s.external_id, j.stage FROM jobs j JOIN sightings s ON s.job_id = j.id").fetchall())
    assert stages == {"1": "queued", "2": "code_removed"}
    removed = conn.execute("SELECT removal_rule, removal_quote FROM jobs WHERE stage = 'code_removed'").fetchone()
    assert tuple(removed) == ("location", "London, UK")
    # The London job needed no detail fetch: the code rule cut it first.
    assert f"{BOARD_URL}/2" not in runner.s.fetcher.requests

    assert _run(runner, conn).new_jobs == 0  # the same list again changes nothing

    responses[BOARD_URL] = {"jobs": [_job(2, "Backend Engineer", "London, UK")]}
    _run(runner, conn)
    assert conn.execute("SELECT miss_streak, closed_at FROM sightings WHERE external_id = '1'").fetchone()[0] == 1
    assert _run(runner, conn).closed == 1
    assert conn.execute(
        "SELECT j.closed_at FROM jobs j JOIN sightings s ON s.job_id = j.id WHERE s.external_id = '1'"
    ).fetchone()[0] is not None

    board = conn.execute("SELECT tier, last_job_count FROM companies WHERE slug = 'acme'").fetchone()
    assert tuple(board) == ("active", 1)


def test_same_key_from_second_source_attaches(conn, cfg, gaz):
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", "Tokyo")]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs.&lt;/p&gt;"},
    }
    runner = _setup(conn, cfg, gaz, responses)
    _run(runner, conn)
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]

    tokyodev = conn.execute("SELECT * FROM sources WHERE id = 'tokyodev'").fetchone()
    summary = Summary("acme/jobs/backend", "https://www.tokyodev.com/companies/acme/jobs/backend",
                      "Backend Engineer", company="Acme", location="Tokyo, Japan", description="<p>Build APIs.</p>")
    assert runner._one(runner._context("tokyodev", {}, None), None, tokyodev, None, summary) == "attached"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sightings WHERE job_id = ?", (job_id,)).fetchone()[0] == 2
    # The employer's own board stays the primary link.
    primary = conn.execute(
        "SELECT s.source_id FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id").fetchone()[0]
    assert primary == "greenhouse"
