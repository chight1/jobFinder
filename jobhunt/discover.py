"""Finds company boards on hiring systems in the Common Crawl URL index and in company lists.

Runs once a month, when a new crawl appears. New boards get one check call that sets
their tier: a 404 makes a board dead, an empty list cold, any jobs active.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from jobhunt import collectors, db
from jobhunt.collectors import BoardGone, Context
from jobhunt.config import Config
from jobhunt.fetch import Fetcher, FetchError, SourceBlocked

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


def discover(cfg: Config, *, force: bool = False) -> dict[str, int]:
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, cfg.sources_csv)
    fetcher = Fetcher(cfg, run_started=datetime.now().astimezone())
    counts = {"found": 0, "new": 0, "checked": 0}
    try:
        crawl = fetcher.get(SOURCE_ID, COLLINFO, kind="api").json()[0]
        if not force and db.get_setting(conn, "current_crawl") == crawl["id"]:
            log.info("crawl %s already read", crawl["id"])
        else:
            boards = list(_crawl_boards(fetcher, crawl["cdx-api"]))
            counts["found"] += len(boards)
            counts["new"] += _insert(conn, boards, found_via=crawl["id"])
            db.set_setting(conn, "current_crawl", crawl["id"])
        for source in conn.execute("SELECT * FROM sources WHERE collector = 'company_list' AND enabled = 1").fetchall():
            boards = list(_company_list_boards(fetcher, source))
            counts["found"] += len(boards)
            counts["new"] += _insert(conn, boards, found_via=source["id"])
        counts["checked"] = check_new_boards(cfg, fetcher)
    finally:
        fetcher.close()
        conn.close()
    return counts


def _crawl_boards(fetcher: Fetcher, cdx_api: str) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Each board once: a crawl holds many links per board."""
    seen: set[tuple[str, str]] = set()
    for ats, url_query, match_type in CDX_QUERIES:
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
                        yield board
        except (FetchError, ValueError, KeyError) as exc:
            log.warning("Common Crawl query %s failed: %r", url_query, exc)


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
                    try:
                        yield from _boards_in(fetcher.get(source["id"], url).text)
                    except SourceBlocked:
                        raise
                    except FetchError as exc:
                        log.warning("%s: %s failed: %r", source["id"], url, exc)
    except FetchError as exc:
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


def check_new_boards(cfg: Config, fetcher: Fetcher) -> int:
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
                    log.warning("%s blocked during board checks: %s", ats, exc)
                    break
                except (FetchError, ValueError, KeyError) as exc:
                    log.warning("%s board %s check failed: %r", ats, board["slug"], exc)
                    continue
                conn.execute(
                    "UPDATE companies SET tier = ?, last_checked_at = ?, last_job_count = ?, "
                    "dead_streak = CASE WHEN ? = 'dead' THEN 1 ELSE 0 END WHERE id = ?",
                    (tier, db.utc_now(), count, tier, board["id"]),
                )
                checked += 1
        finally:
            conn.close()
        return checked

    with ThreadPoolExecutor(max_workers=len(CHECKED_SYSTEMS)) as pool:
        return sum(pool.map(check, CHECKED_SYSTEMS))
