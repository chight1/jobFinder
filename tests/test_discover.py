"""Company discovery from the Common Crawl index, and the retries it leans on."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from jobhunt import db, discover
from jobhunt.fetch import Fetcher, FetchError, NetworkError, Page, SourceBlocked
from tests.test_collectors import FakeFetcher

CDX = "https://index.example/CC-1-index"


def _fetcher(cfg, statuses: list[int]) -> Fetcher:
    """A real fetcher with no waits, answering with these statuses in turn."""
    cfg = dataclasses.replace(cfg, fetch=cfg.fetch | {"retry_wait_s": 0, "api_gap_s": 0, "api_jitter_s": 0})
    fetcher = Fetcher(cfg, run_started=discover.datetime.now().astimezone())
    answers = iter(statuses)
    fetcher._local.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(next(answers), json={"pages": 1})))
    return fetcher


def test_a_gateway_timeout_is_tried_again(cfg, conn):
    page = _fetcher(cfg, [504, 502, 200]).get("commoncrawl", CDX, kind="api")
    assert page.status == 200


def test_a_gateway_that_keeps_failing_is_an_error_not_a_block(cfg, conn):
    with pytest.raises(FetchError) as caught:
        _fetcher(cfg, [504, 504, 504]).get("commoncrawl", CDX, kind="api")
    assert not isinstance(caught.value, SourceBlocked)
    with pytest.raises(SourceBlocked):
        _fetcher(cfg, [503, 503, 503]).get("commoncrawl", CDX, kind="api")


class CrawlFetcher(FakeFetcher):
    def close(self) -> None:
        pass


def _lever_line(slug: str) -> str:
    return json.dumps({"url": f"https://jobs.lever.co/{slug}/123"})


def test_a_system_counts_as_read_only_when_its_searches_finished(cfg, conn, monkeypatch):
    lever = {"url": "jobs.lever.co/*", "output": "json", "fl": "url"}
    fake = CrawlFetcher({
        discover.COLLINFO: [{"id": "CC-1", "cdx-api": CDX}],
        str(httpx.URL(CDX, params=lever | {"showNumPages": "true"})): {"pages": 1},
        str(httpx.URL(CDX, params=lever | {"page": 0})): "\n".join([_lever_line("acme"), _lever_line("acme"),
                                                                    _lever_line("globex")]),
        # Greenhouse's searches get no answer: a 404 page, which does not parse
    })
    monkeypatch.setattr(discover, "Fetcher", lambda cfg, run_started: fake)
    monkeypatch.setattr(discover, "check_new_boards", lambda cfg, fetcher, systems: 0)
    wayback = []
    monkeypatch.setattr(discover, "_wayback_boards", lambda conn, fetcher, ats, force: wayback.append(ats) or (0, 0))

    assert discover.discover(cfg, only="lever") == {"found": 2, "new": 2, "checked": 0}
    assert db.get_setting(conn, "crawl_read:lever") == "CC-1"
    discover.discover(cfg, only="greenhouse")
    assert db.get_setting(conn, "crawl_read:greenhouse") is None  # asked again next time

    asked = len(fake.requests)
    discover.discover(cfg, only="lever")
    assert len(fake.requests) == asked + 1  # only the crawl list: Lever was read from this crawl already
    assert wayback == ["lever", "greenhouse", "lever"]  # the Wayback index is asked either way


class StoppingFetcher(CrawlFetcher):
    """Fails on the URLs in `failing` until they are taken out."""

    def __init__(self, responses, failing) -> None:
        super().__init__(responses)
        self.failing = failing

    def get(self, source_id, url, *, kind="html", params=None, headers=None, timeout_s=None, retries=None):
        full = str(httpx.URL(url, params=params)) if params else url
        if full in self.failing:
            self.requests.append(full)
            raise FetchError("HTTP 503 after 2 retries")
        return super().get(source_id, url, kind=kind, params=params, headers=headers)


def test_the_wayback_index_resumes_at_the_page_it_stopped_on(cfg, conn):
    query = {"url": "jobs.lever.co/", "matchType": "prefix"}
    rows = query | {"fl": "original", "collapse": "urlkey",
                    "filter": ["statuscode:200", f"original:{discover.WAYBACK_QUERIES[0][2]}"]}
    page = [str(httpx.URL(discover.WAYBACK_CDX, params=rows | {"page": n})) for n in range(3)]
    fetcher = StoppingFetcher({
        str(httpx.URL(discover.WAYBACK_CDX, params=query | {"showNumPages": "true"})): "3",
        page[0]: "https://jobs.lever.co/acme\nhttps://jobs.lever.co/acme/\n",
        page[1]: "https://jobs.lever.co/vrchat?lever-origin=applied\n",
        page[2]: "https://jobs.lever.co/globex\n",
    }, failing={page[1]})

    assert discover._wayback_boards(conn, fetcher, "lever", force=False) == (2, 1)  # acme twice: one board
    assert db.get_setting(conn, "wayback_page:lever") == "1" and db.get_setting(conn, "wayback_read:lever") is None

    fetcher.failing = set()
    assert discover._wayback_boards(conn, fetcher, "lever", force=False) == (2, 2)
    assert fetcher.requests.count(page[0]) == 1  # not asked again
    assert db.get_setting(conn, "wayback_read:lever")
    assert {r["slug"] for r in conn.execute("SELECT slug FROM companies WHERE found_via = 'wayback'")} == {
        "acme", "vrchat", "globex"}
    assert discover._wayback_boards(conn, fetcher, "lever", force=False) == (0, 0)  # read once


class BusyFetcher(CrawlFetcher):
    """Answers busy (503, a dropped connection, 504, in turn) `busy` times, then with the page."""

    def __init__(self, busy: int) -> None:
        super().__init__({})
        self.busy = busy

    def get(self, source_id, url, *, kind="html", params=None, headers=None, timeout_s=None, retries=None):
        assert retries == 0  # the waiting is done by the caller
        if self.busy:
            self.busy -= 1
            if self.busy % 3 == 2:
                raise SourceBlocked("HTTP 503 after 0 retries", 503)
            if self.busy % 3 == 1:
                raise NetworkError("network error after 0 retries: RemoteProtocolError()")
            raise FetchError("HTTP 504 after 0 retries", 504)
        return Page(url, 200, "7")


def test_a_busy_wayback_index_doubles_the_wait_up_to_half_an_hour_then_waits_hourly(monkeypatch):
    waits = []
    monkeypatch.setattr(discover.time, "sleep", waits.append)
    assert discover._wayback_get(BusyFetcher(busy=6), {}) == "7"
    assert waits == [60, 120, 240, 480, 960, 1800]

    waits.clear()
    assert discover._wayback_get(BusyFetcher(busy=9), {}) == "7"  # never gives up on a busy index
    assert waits == [60, 120, 240, 480, 960, 1800, 3600, 3600, 3600]


def test_a_gateway_or_network_failure_from_the_fetcher_is_marked(cfg, conn):
    with pytest.raises(FetchError) as caught:
        _fetcher(cfg, [504]).get("commoncrawl", CDX, kind="api", retries=0)
    assert caught.value.status == 504

    fetcher = _fetcher(cfg, [])
    fetcher._local.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=request))))
    with pytest.raises(NetworkError):
        fetcher.get("commoncrawl", CDX, kind="api", retries=0)


def test_another_failure_stops_at_once(monkeypatch):
    class Gone(CrawlFetcher):
        def get(self, source_id, url, **kwargs):
            raise FetchError("HTTP 400 for the index", 400)

    waits = []
    monkeypatch.setattr(discover.time, "sleep", waits.append)
    with pytest.raises(FetchError):
        discover._wayback_get(Gone({}), {})
    assert waits == []


class CheckFetcher(CrawlFetcher):
    """Answers each board's list call by slug: an exception to raise, or the postings."""

    def __init__(self, answers) -> None:
        super().__init__({})
        self.answers = answers
        self.asked: list[str] = []

    def get(self, source_id, url, **kwargs):
        slug = httpx.URL(url).path.rstrip("/").rsplit("/", 1)[-1]
        self.asked.append(slug)
        answer = self.answers[slug]
        if isinstance(answer, Exception):
            raise answer
        return Page(url, 200, json.dumps(answer))


def _new_lever_boards(conn, *slugs):
    for slug in slugs:
        conn.execute("INSERT INTO companies (ats, slug, found_via) VALUES ('lever', ?, 'test')", (slug,))
    conn.commit()


def _tiers(conn):
    return {r["slug"]: r["tier"] for r in conn.execute("SELECT slug, tier FROM companies")}


def test_a_broken_board_check_skips_only_that_board(cfg, conn):
    _new_lever_boards(conn, "acme", "globex")
    job = {"id": "1", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/globex/1", "categories": {}}
    fetcher = CheckFetcher({"acme": FetchError("HTTP 500", 500), "globex": [job]})

    assert discover.check_new_boards(cfg, fetcher, ("lever",)) == 1
    assert _tiers(conn) == {"acme": "new", "globex": "active"}  # acme is asked again next time


def test_pushback_ends_the_checks_on_a_shared_host(cfg, conn):
    _new_lever_boards(conn, "acme", "globex")
    fetcher = CheckFetcher({"acme": SourceBlocked("HTTP 429", 429), "globex": []})

    assert discover.check_new_boards(cfg, fetcher, ("lever",)) == 0
    assert fetcher.asked == ["acme"]
