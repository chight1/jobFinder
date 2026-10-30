"""Finds company boards on hiring systems in the Common Crawl and Wayback Machine URL indexes, and in
company lists.

Runs once a month, when a new crawl appears. The Wayback Machine keeps every capture since the 2010s,
so it is read once, a page at a time, resuming where a stopped run left off. Each hiring system's searches count as read only when
all of them succeeded, so a failed one is tried again on the next run. New boards get one check call
that sets their tier: a 404 makes a board dead, an empty list cold, any jobs active.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobfinder import collectors, db
from jobfinder.collectors import BoardGone, Context
from jobfinder.config import Config
from jobfinder.fetch import Fetcher, FetchError, NetworkError, SourceBlocked

log = logging.getLogger(__name__)

SOURCE_ID = "commoncrawl"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
# (hiring system, CDX url query, matchType)
CDX_QUERIES = (
    ("lever", "jobs.lever.co/*", None),
    ("greenhouse", "boards.greenhouse.io/*", None),
    ("greenhouse", "job-boards.greenhouse.io/*", None),
    ("ashby", "jobs.ashbyhq.com/*", None),
    ("workable", "apply.workable.com/*", None),
    ("workday", "myworkdayjobs.com", "domain"),
)
CHECKED_SYSTEMS = ("lever", "greenhouse", "ashby", "workable", "workday")
WAYBACK = "wayback"  # its source id in sources.csv
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_TIMEOUT_S = 300  # their side scans the whole page for the filter, which can take minutes
WAYBACK_GAP_S = 30  # as in sources.csv; a busy answer doubles the wait from here
WAYBACK_BUSY = (502, 503, 504)  # busy, or a search that timed out on their side
WAYBACK_MAX_WAIT_S = 30 * 60  # the wait stops doubling here
WAYBACK_HOURLY_S = 60 * 60  # still busy after the longest wait: ask once an hour from then on
# (hiring system, url prefix, a board's front page). Only front pages are asked for, one row per board;
# every job page ever captured would be millions of rows.
WAYBACK_QUERIES = (
    ("lever", "jobs.lever.co/", r"https?://jobs\.lever\.co/[^/?#]+/?(\?.*)?$"),
)
CRAWL_SYSTEMS = tuple(dict.fromkeys(ats for ats, _, _ in CDX_QUERIES))

_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")
_WORKDAY_HOST = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_NOT_SLUGS = {"api", "j", "embed", "v0", "v1", "wday", "jobs", "careers"}
ATS_LINK = re.compile(
    r"https?://(?:jobs\.lever\.co|(?:job-)?boards\.greenhouse\.io|jobs\.ashbyhq\.com|apply\.workable\.com|"
    r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/[^\s\"'<>)\]]*"
)
_HOST_TO_ATS = (
    ("lever.co", "lever"), ("greenhouse.io", "greenhouse"), ("ashbyhq.com", "ashby"),
    ("workable.com", "workable"), ("myworkdayjobs.com", "workday"),
)


def board_from_url(url: str) -> tuple[str, str, dict[str, Any]] | None:
    """(hiring system, board slug, extra) for a link into a hiring system, or None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    ats = next((name for suffix, name in _HOST_TO_ATS if host.endswith(suffix)), None)
    if ats is None:
        return None
    segments = [s for s in parts.path.split("/") if s]

    if ats == "workday":
        match = _WORKDAY_HOST.match(host)
        if not match:
            return None
        if segments and _LOCALE.match(segments[0]):
            segments = segments[1:]
        if not segments or segments[0].lower() in _NOT_SLUGS or not _SLUG.match(segments[0]):
            return None
        site = segments[0]
        return ats, f"{host}/{site}", {"host": host, "tenant": match.group(1), "site": site}

    slug = segments[0] if segments else ""
    if ats == "greenhouse" and slug == "embed":
        slug = (parse_qs(parts.query).get("for") or [""])[0]
    slug = slug.lower()
    if not slug or slug in _NOT_SLUGS or not _SLUG.match(slug):
        return None
    return ats, slug, {}


def discover(cfg: Config, *, force: bool = False, only: str | None = None) -> dict[str, int]:
    """Read the newest crawl for every hiring system, or `only` one (the company lists are then skipped)."""
    if only is not None and only not in CRAWL_SYSTEMS:
        raise SystemExit(f"no hiring system {only!r}; choose from {', '.join(CRAWL_SYSTEMS)}")
    systems = (only,) if only else CRAWL_SYSTEMS
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, cfg.sources_csv)
    fetcher = Fetcher(cfg, run_started=datetime.now().astimezone())
    counts = {"found": 0, "new": 0, "checked": 0}
    try:
        crawl = fetcher.get(SOURCE_ID, COLLINFO, kind="api").json()[0]
        for ats in systems:
            read = f"crawl_read:{ats}"
            if not force and db.get_setting(conn, read) == crawl["id"]:
                log.info("crawl %s already read for %s", crawl["id"], ats)
            else:
                boards, complete = _crawl_boards(fetcher, crawl["cdx-api"], ats)
                counts["found"] += len(boards)
                counts["new"] += _insert(conn, boards, found_via=crawl["id"])
                if complete:
                    db.set_setting(conn, read, crawl["id"])
            found, new = _wayback_boards(conn, fetcher, ats, force=force)
            counts["found"] += found
            counts["new"] += new
        if only is None:
            for source in conn.execute(
                "SELECT * FROM sources WHERE collector = 'company_list' AND enabled = 1"
            ).fetchall():
                boards = list(_company_list_boards(fetcher, source))
                counts["found"] += len(boards)
                counts["new"] += _insert(conn, boards, found_via=source["id"])
        counts["checked"] = check_new_boards(cfg, fetcher, systems if only else CHECKED_SYSTEMS)
    finally:
        fetcher.close()
        conn.close()
    return counts


def _crawl_boards(fetcher: Fetcher, cdx_api: str, ats: str) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """One hiring system's boards in the crawl, each once (a crawl holds many links per board), and
    whether every search finished. Boards from a search that failed partway are kept."""
    boards: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    complete = True
    for system, url_query, match_type in CDX_QUERIES:
        if system != ats:
            continue
        params: dict[str, Any] = {"url": url_query, "output": "json", "fl": "url"}
        if match_type:
            params["matchType"] = match_type
        try:
            pages = fetcher.get(SOURCE_ID, cdx_api, kind="api", params=params | {"showNumPages": "true"}).json()["pages"]
            for number in range(int(pages)):
                page = fetcher.get(SOURCE_ID, cdx_api, kind="api", params=params | {"page": number})
                for line in page.text.splitlines():
                    if not line.strip():
                        continue
                    board = board_from_url(json.loads(line)["url"])
                    if board and board[0] == ats and board[:2] not in seen:
                        seen.add(board[:2])
                        boards.append(board)
        except (FetchError, ValueError, KeyError) as exc:
            log.warning("Common Crawl query %s failed: %r", url_query, exc)
            complete = False
    return boards, complete


def _wayback_boards(conn: sqlite3.Connection, fetcher: Fetcher, ats: str, *, force: bool) -> tuple[int, int]:
    """Board front pages in the Wayback Machine's index, page by page. Each page's boards are saved as
    it comes, with the next page to ask for, so a run that stops resumes there. Returns (found, new)."""
    found = new = 0
    for system, prefix, front_page in WAYBACK_QUERIES:
        if system != ats:
            continue
        done, next_page = f"wayback_read:{ats}", f"wayback_page:{ats}"
        if force:
            db.set_setting(conn, next_page, "0")
            db.set_setting(conn, done, "")  # a forced read that stops partway resumes like any other
        elif db.get_setting(conn, done):
            log.info("Wayback index already read for %s", ats)
            continue
        query = {"url": prefix, "matchType": "prefix"}
        rows = query | {"fl": "original", "collapse": "urlkey", "filter": ["statuscode:200", f"original:{front_page}"]}
        start = number = int(db.get_setting(conn, next_page) or 0)
        try:
            pages = int(_wayback_get(fetcher, query | {"showNumPages": "true"}).strip())
            for number in range(start, pages):
                text = _wayback_get(fetcher, rows | {"page": number})
                boards = [board for line in text.splitlines()
                          if (board := board_from_url(line.strip())) and board[0] == ats]
                found += len(boards)
                new += _insert(conn, boards, found_via=WAYBACK)
                db.set_setting(conn, next_page, str(number + 1))
                log.info("Wayback %s page %d of %d: %d boards", ats, number + 1, pages, len(boards))
            db.set_setting(conn, done, db.utc_now())
        except (FetchError, ValueError) as exc:
            log.warning("Wayback query %s stopped at page %d: %r", prefix, number, exc)
    return found, new


def _wayback_get(fetcher: Fetcher, params: dict[str, Any]) -> str:
    """One index request. Each busy answer (502, 503, 504) or lost request (a dropped connection, a
    timeout) doubles the wait before asking again, from the 30 s gap up to 30 minutes, then asks once
    an hour for as long as it takes. A success starts over at 30 s."""
    wait = WAYBACK_GAP_S
    while True:
        try:
            return fetcher.get(WAYBACK, WAYBACK_CDX, kind="api", params=params, timeout_s=WAYBACK_TIMEOUT_S,
                               retries=0).text
        except FetchError as exc:
            if not (exc.status in WAYBACK_BUSY or isinstance(exc, NetworkError)):
                raise
            wait = WAYBACK_HOURLY_S if wait >= WAYBACK_MAX_WAIT_S else min(wait * 2, WAYBACK_MAX_WAIT_S)
            log.info("Wayback index busy; asking again in %d s", wait)
            time.sleep(wait)


def _company_list_boards(fetcher: Fetcher, source: sqlite3.Row) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Hiring-system links on a company list, and on the pages it links to when "follow" is set."""
    config = db.loads(source["config"], {})
    follow = re.compile(config["follow"]) if config.get("follow") else None
    max_follow = int(config.get("max_follow", 1000))
    try:
        for list_url in config["list_urls"]:
            text = fetcher.get(source["id"], list_url).text
            yield from _boards_in(text)
            if follow:
                for url in list(dict.fromkeys(follow.findall(text)))[:max_follow]:
                    yield from _boards_in(fetcher.get(source["id"], url).text)
    except FetchError as exc:  # any error ends this list for the run
        log.warning("company list %s stopped: %r", source["id"], exc)


def _boards_in(text: str) -> Iterable[tuple[str, str, dict[str, Any]]]:
    for url in ATS_LINK.findall(text):
        board = board_from_url(url)
        if board:
            yield board


def _insert(conn: sqlite3.Connection, boards: list[tuple[str, str, dict[str, Any]]], *, found_via: str) -> int:
    """Insert boards not already known (the (ats, slug) key ignores repeats). Returns how many were new."""
    before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    with db.transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO companies (ats, slug, extra, found_via) VALUES (?, ?, ?, ?)",
            [(ats, slug, db.dumps(extra), found_via) for ats, slug, extra in boards],
        )
    return conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] - before


def check_new_boards(cfg: Config, fetcher: Fetcher, systems: tuple[str, ...] = CHECKED_SYSTEMS) -> int:
    """One list call per new board, one hiring system per thread, the systems side by side."""

    def check(ats: str) -> int:
        conn = db.connect(cfg.db_path)
        collector = collectors.get(ats)
        checked = 0
        try:
            for board in conn.execute("SELECT * FROM companies WHERE ats = ? AND tier = 'new'", (ats,)).fetchall():
                ctx = Context(
                    source_id=ats,
                    config=db.loads(board["extra"], {}),
                    fetcher=fetcher,
                    known=lambda ids: set(),
                    deadline=time.time(),  # paged systems stop after their first page
                    board=dict(board),
                )
                try:
                    count = len(collector.list_postings(ctx, None).seen_ids)
                    tier = "active" if count else "cold"
                except BoardGone:
                    count, tier = 0, "dead"
                except SourceBlocked as exc:
                    log.warning("%s board %s check blocked: %r", ats, board["slug"], exc)
                    if ats == "workday":
                        continue  # each Workday board is its own site
                    break  # the shared API host pushed back: no more checks there this run
                except (FetchError, ValueError, KeyError) as exc:
                    log.warning("%s board %s check failed: %r", ats, board["slug"], exc)
                    continue  # that board's own trouble: it stays new and is asked again next time
                conn.execute(
                    "UPDATE companies SET tier = ?, last_checked_at = ?, last_job_count = ?, "
                    "dead_streak = CASE WHEN ? = 'dead' THEN 1 ELSE 0 END WHERE id = ?",
                    (tier, db.utc_now(), count, tier, board["id"]),
                )
                checked += 1
        finally:
            conn.close()
        return checked

    with ThreadPoolExecutor(max_workers=len(systems)) as pool:
        return sum(pool.map(check, systems))
