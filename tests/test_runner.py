"""The runner against a real SQLite file: new jobs, code removals, attachments and closures."""

from __future__ import annotations

import csv
import time

import pytest

from jobhunt import db
from jobhunt.collectors import Summary
from jobhunt.collectors.runner import Runner, Shared
from jobhunt.dedupe import AgencyIndex
from jobhunt.fetch import SourceBlocked
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
    with (ROOT / "sources.csv").open(encoding="utf-8", newline="") as f:
        rows = sum(1 for _ in csv.DictReader(f))
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == rows - 9  # every row is one or the other
    rakuten = conn.execute("SELECT * FROM companies WHERE ats = 'workday'").fetchone()
    assert db.loads(rakuten["extra"])["site"] == "RakutenInc"


def test_new_jobs_removed_or_queued(conn, cfg, gaz):
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

    # A job missing from the list is not taken as gone: most lists show only the newest jobs.
    responses[BOARD_URL] = {"jobs": [_job(2, "Backend Engineer", "London, UK")]}
    _run(runner, conn)
    assert conn.execute("SELECT stage FROM jobs WHERE id = 1").fetchone()[0] == "queued"

    board = conn.execute("SELECT tier, last_job_count FROM companies WHERE slug = 'acme'").fetchone()
    assert tuple(board) == ("active", 1)



def test_a_blacklisted_employers_new_jobs_are_code_removed(conn, cfg, gaz):
    runner = _setup(conn, cfg, gaz, {BOARD_URL: {"jobs": [_job(1, "Backend Engineer", "Tokyo")]}})
    runner.s.blacklist = frozenset({"acme"})
    assert _run(runner, conn).code_removed == 1
    removed = conn.execute("SELECT stage, removal_rule, removal_quote FROM jobs").fetchone()
    assert tuple(removed) == ("code_removed", "blacklisted", "Acme")
    assert f"{BOARD_URL}/1" not in runner.s.fetcher.requests  # cut before its detail fetch


def test_a_repost_joins_its_job_only_while_recently_seen(conn, cfg, gaz):
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", "Tokyo")]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs in Go.&lt;/p&gt;"},
    }
    runner = _setup(conn, cfg, gaz, responses)
    _run(runner, conn)

    responses[BOARD_URL] = {"jobs": [_job(3, "Backend Engineer", "Tokyo")]}  # same position, new id
    assert _run(runner, conn).new_jobs == 0  # seen this month: the same job

    conn.execute("UPDATE sightings SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    responses[BOARD_URL] = {"jobs": [_job(4, "Backend Engineer", "Tokyo")]}
    responses[f"{BOARD_URL}/4"] = responses[f"{BOARD_URL}/1"]
    assert _run(runner, conn).new_jobs == 1  # not seen for years: a new opening

def test_a_long_list_of_places_is_stored_as_anywhere(conn, cfg, gaz):
    countries = "; ".join(["Tokyo, JP", "AF", "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR"])
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", countries)]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs.&lt;/p&gt;"},
    }
    _run(_setup(conn, cfg, gaz, responses), conn)
    assert conn.execute("SELECT location_raw FROM sightings").fetchone()[0] == "Anywhere (12 places)"
    assert conn.execute("SELECT stage FROM jobs").fetchone()[0] == "queued"

    # The migration gives rows stored before the rule the same treatment.
    conn.execute("UPDATE sightings SET location_raw = ?", (countries,))
    conn.executescript(db.MIGRATIONS[4])
    assert conn.execute("SELECT location_raw FROM sightings").fetchone()[0] == "Anywhere (12 places)"


def test_a_source_default_location_fills_a_blank_one(conn, cfg, gaz):
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", ""), _job(2, "Frontend Engineer", "London, UK")]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs.&lt;/p&gt;"},
    }
    runner = _setup(conn, cfg, gaz, responses)
    source = conn.execute("SELECT * FROM sources WHERE id = 'greenhouse'").fetchone()
    board = conn.execute("SELECT * FROM companies WHERE slug = 'acme'").fetchone()
    conn.execute("UPDATE companies SET extra = ? WHERE id = ?", (db.dumps({"default_location": "Worldwide"}), board["id"]))
    runner.run_board(source, conn.execute("SELECT * FROM companies WHERE slug = 'acme'").fetchone())
    places = dict(conn.execute("SELECT external_id, location_raw FROM sightings").fetchall())
    assert places == {"1": "Worldwide", "2": "London, UK"}  # a posting's own location still wins

    conn.execute("DELETE FROM sightings")
    conn.execute("DELETE FROM jobs")
    conn.execute("UPDATE companies SET extra = ? WHERE id = ?", (db.dumps({"fixed_location": "Remote (open to Japan)"}), board["id"]))
    runner.run_board(source, conn.execute("SELECT * FROM companies WHERE slug = 'acme'").fetchone())
    places = set(r[0] for r in conn.execute("SELECT location_raw FROM sightings"))
    assert places == {"Remote (open to Japan)"}  # a fixed one wins over the posting's


def test_an_agency_post_without_a_company_gets_the_agency_name(conn, cfg, gaz):
    config = {"list_urls": ["https://agency.example/jobs"], "job_link": r"^https://agency\.example/jobs/(\d+)$",
              "selectors": {"company": ".client", "location": ".place"}}
    runner = _setup(conn, cfg, gaz, {
        "https://agency.example/jobs": '<a href="/jobs/1">Backend Engineer</a><a href="/jobs/2">Frontend Engineer</a>',
        "https://agency.example/jobs/1": '<h1>Backend Engineer</h1><p class="place">Tokyo</p><main>Build APIs.</main>',
        "https://agency.example/jobs/2": '<h1>Frontend Engineer</h1><p class="client">Acme KK</p>'
                                         '<p class="place">Tokyo</p><main>Build screens.</main>',
    })
    conn.execute("INSERT INTO sources (id, name, collector, kind, link, config, enabled) "
                 "VALUES ('agency', 'Example Recruitment', 'html', 'agent', 'https://agency.example', ?, 1)",
                 (db.dumps(config),))
    runner.run_source(conn.execute("SELECT * FROM sources WHERE id = 'agency'").fetchone())
    companies = dict(conn.execute("SELECT external_id, company_raw FROM sightings").fetchall())
    assert companies == {"1": "Example Recruitment", "2": "Acme KK"}  # a named employer still wins


def test_a_block_keeps_the_totals_of_what_came_before_it(conn, cfg, gaz):
    responses = {
        BOARD_URL: {"jobs": [_job(1, "Backend Engineer", "Tokyo"), _job(2, "Frontend Engineer", "Osaka")]},
        f"{BOARD_URL}/1": {"content": "&lt;p&gt;Build APIs.&lt;/p&gt;"},
    }
    runner = _setup(conn, cfg, gaz, responses)
    fake_get = runner.s.fetcher.get

    def get(source_id, url, **kwargs):
        if url == f"{BOARD_URL}/2":
            raise SourceBlocked("challenge page")
        return fake_get(source_id, url, **kwargs)

    runner.s.fetcher.get = get
    with pytest.raises(SourceBlocked):
        _run(runner, conn)
    assert (runner.unfinished.parsed, runner.unfinished.new_jobs) == (2, 1)


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
