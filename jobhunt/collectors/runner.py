"""The bookkeeping around collectors.

For each posting a collector returns: a known one is touched; a new one is matched
against existing jobs (dedupe levels 1 and 2), run through the code rules, given its
detail fetch if needed, compared with the company's recently seen jobs (level 3), and stored as
a new job in the removed pile or the model queue.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Iterable, Literal

from jobhunt import collectors, db, rules
from jobhunt.collectors import BoardGone, Context, Listing, PostingGone, Summary
from jobhunt.config import Config
from jobhunt.dedupe import (
    AgencyIndex,
    canonical_url,
    city_norm,
    company_norm,
    fuzzy_verdict,
    is_agency,
    title_norm,
)
from jobhunt.fetch import DailyCapReached, Fetcher, FetchError, SourceBlocked
from jobhunt.text import short_location, to_text

log = logging.getLogger(__name__)

# What happened to one posting: already stored, added to a known job, removed by code, or queued.
Outcome = Literal["touched", "attached", "removed", "queued"]

REPOST_WINDOW = timedelta(days=30)
DEAD_MONTHS_TO_DROP = 3
_KIND_RANK = {"employer": 0, "japan_board": 1, "global_board": 2, "agent": 3}
_HTML_COLLECTORS = frozenset({"html", "hrmos", "talentio", "rss", "browser"})


@dataclass
class Tally:
    parsed: int = 0
    new_sightings: int = 0
    new_jobs: int = 0
    code_removed: int = 0
    errors: int = 0
    status: str = "ok"

    def add(self, other: Tally) -> None:
        for name in ("parsed", "new_sightings", "new_jobs", "code_removed", "errors"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        if other.status != "ok":
            self.status = other.status


@dataclass
class Shared:
    """What every fetch thread needs for one run."""

    cfg: Config
    gaz: rules.Gazetteer
    fetcher: Fetcher
    deadline: float  # time.time()
    aliases: dict[str, str]
    source_kinds: dict[str, str]
    agency_index: AgencyIndex
    blacklist: frozenset[str] = frozenset()  # company_norm keys of employers whose jobs are always removed
    dry_run: bool = False
    agencies: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.agencies = list(self.cfg.dedupe.get("agencies", []))


@dataclass(frozen=True)
class _Key:
    company: str
    title: str
    city: str


class Runner:
    def __init__(self, shared: Shared, conn: sqlite3.Connection) -> None:
        self.s = shared
        self.conn = conn
        # The totals of a listing a block or the daily cap cut off: what it saved before then still counts.
        self.unfinished: Tally | None = None

    # --- entry points --------------------------------------------------------------

    def run_source(self, source: sqlite3.Row) -> Tally:
        collector = collectors.get(source["collector"])
        ctx = self._context(source["id"], db.loads(source["config"], {}), None)
        detail = detail_loader(collector, ctx.config)
        try:
            listing = collector.list_postings(ctx, db.loads(source["cursor"]))
            tally = self._ingest(ctx, detail, source, None, listing)
        finally:
            self._finish(collector)
            if detail is not collector:
                self._finish(detail)
        # A run cut short keeps the old cursor: a feed's new ETag would hide the entries not yet read.
        if not self.s.dry_run and tally.status == "ok":
            self.conn.execute(
                "UPDATE sources SET cursor = ? WHERE id = ?",
                (db.dumps(listing.cursor) if listing.cursor else None, source["id"]),
            )
        return tally

    def run_board(self, source: sqlite3.Row, board: sqlite3.Row) -> Tally:
        collector = collectors.get(source["collector"])
        config = db.loads(source["config"], {}) | db.loads(board["extra"], {})
        ctx = self._context(source["id"], config, dict(board))
        try:
            listing = collector.list_postings(ctx, db.loads(board["cursor"]))
        except BoardGone:
            self._mark_board(board, job_count=None)
            return Tally(status="board gone")
        detail = detail_loader(collector, config)
        try:
            tally = self._ingest(ctx, detail, source, board["id"], listing)
        finally:
            if detail is not collector:
                self._finish(detail)
        cursor = listing.cursor if tally.status == "ok" else db.loads(board["cursor"])
        self._mark_board(board, job_count=len(listing.seen_ids), cursor=cursor)
        return tally

    # --- one listing -----------------------------------------------------------------

    def _context(self, source_id: str, config: dict[str, Any], board: dict[str, Any] | None) -> Context:
        board_id = board["id"] if board else None
        return Context(
            source_id=source_id,
            config=config,
            fetcher=self.s.fetcher,
            known=lambda ids: self._known(source_id, board_id, ids),
            deadline=self.s.deadline,
            board=board,
        )

    def _known(self, source_id: str, board_id: int | None, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = self.conn.execute(
                f"SELECT external_id FROM sightings WHERE source_id = ? AND board_id IS ? "
                f"AND external_id IN ({','.join('?' * len(chunk))})",
                [source_id, board_id, *chunk],
            )
            found |= {r["external_id"] for r in rows}
        return found

    def _ingest(
        self, ctx: Context, detail: ModuleType, source: sqlite3.Row, board_id: int | None, listing: Listing
    ) -> Tally:
        """detail is the module whose fetch_detail opens job pages (see detail_loader)."""
        tally = self.unfinished = Tally(parsed=len(listing.summaries))
        if self.s.dry_run:
            for summary in listing.summaries:
                print(f"{summary.external_id}\t{summary.title}\t{summary.company}\t{summary.location}\t{summary.url}")
            return tally
        for summary in listing.summaries:
            if ctx.out_of_time():
                tally.status = "partial"  # unprocessed postings are still new next run
                break
            try:
                outcome = self._one(ctx, detail, source, board_id, summary)
            except (SourceBlocked, DailyCapReached):
                raise
            except PostingGone:
                continue  # taken down since the list was read: not an error
            except (FetchError, ValueError, KeyError, sqlite3.IntegrityError) as exc:
                # Any error ends this site for the run; what is left is still new next run.
                tally.errors += 1
                tally.status = f"stopped: {exc!r}"[:200]
                log.warning("%s: stopped at %s: %r", source["id"], summary.url, exc)
                break
            if outcome != "touched":
                tally.new_sightings += 1
            if outcome in ("removed", "queued"):
                tally.new_jobs += 1
            if outcome == "removed":
                tally.code_removed += 1
        self.unfinished = None
        return tally

    def _one(
        self, ctx: Context, detail: ModuleType, source: sqlite3.Row, board_id: int | None, summary: Summary
    ) -> Outcome:
        summary = replace(summary, location=self._place(ctx, summary))
        if not summary.company and source["kind"] == "agent":
            # Agency posts usually hide the employer. The agency's name keeps them apart from each
            # other in dedupe (agency posts are never merged) instead of all sharing a blank company.
            summary = replace(summary, company=source["name"])
        url = canonical_url(summary.url)
        existing = self.conn.execute(
            "SELECT id FROM sightings "
            "WHERE url = ? OR (source_id = ? AND board_id IS ? AND external_id = ?)",
            (url, source["id"], board_id, summary.external_id),
        ).fetchone()
        if existing:
            self._touch(existing)
            return "touched"

        # Dedupe level 2 first: a known position needs no rules and no detail fetch.
        key = self._key(summary)
        job = self._job_by_key(key)
        if job:
            self._attach(job, summary, url, source, board_id)
            return "attached"

        if not summary.from_link:
            removal = self._code_rules(summary)
            if removal:
                self._create(summary, url, source, board_id, key, "code_removed", removal)
                return "removed"

        if not summary.description:
            summary = detail.fetch_detail(ctx, summary)
            summary = replace(summary, location=self._place(ctx, summary), from_link=False)
            new_key = self._key(summary)
            if new_key != key:
                key = new_key
                job = self._job_by_key(key)
                if job:
                    self._attach(job, summary, url, source, board_id)
                    return "attached"

        removal = self._code_rules(summary)  # again: the detail adds the language and maybe the place
        if removal:
            self._create(summary, url, source, board_id, key, "code_removed", removal)
            return "removed"
        return self._place_new(summary, url, source, board_id, key)

    def _place_new(self, summary: Summary, url: str, source: sqlite3.Row, board_id: int | None, key: _Key) -> Outcome:
        """Dedupe level 3, then a new job in the model queue."""
        text = to_text(summary.description)
        if is_agency(key.company, self.s.agencies):
            match = self.s.agency_index.best_match(text)
            job_id = self._create(summary, url, source, board_id, key, "queued", None)
            if match:
                self._pair(match[0], job_id, None, match[1], "probably_same", "minhash")
        else:
            borderline: list[tuple[int, float, float]] = []
            for other in self._recent_jobs_at(key.company):
                verdict, title_sim, desc_sim = fuzzy_verdict(key.title, text, other["title_norm"],
                                                             to_text(other["description"]))
                if verdict == "merge":
                    self._attach(other, summary, url, source, board_id)
                    return "attached"
                if verdict == "borderline":
                    borderline.append((other["id"], title_sim, desc_sim))
            job_id = self._create(summary, url, source, board_id, key, "queued", None)
            for other_id, title_sim, desc_sim in borderline:
                self._pair(other_id, job_id, title_sim, desc_sim, None, None)
        self.s.agency_index.add(job_id, text)
        return "queued"

    # --- rules and keys -----------------------------------------------------------------

    def _code_rules(self, summary: Summary) -> rules.Removal | None:
        if summary.company and company_norm(summary.company, self.s.aliases) in self.s.blacklist:
            return rules.Removal(rules.BLACKLISTED, summary.company)
        return rules.code_rules(
            title=summary.title,
            location=summary.location,
            description_text=to_text(summary.description),
            fields=summary.fields,
            gaz=self.s.gaz,
        )

    @staticmethod
    def _place(ctx: Context, summary: Summary) -> str:
        """The posting's location, shortened when it lists most of the world. A source whose jobs all share
        one (a worldwide-only board) names it as default_location, used when a posting gives none, or as
        fixed_location when the pages give a wrong one (the same boilerplate countries on every job)."""
        if ctx.config.get("fixed_location"):
            return ctx.config["fixed_location"]
        return short_location(summary.location or ctx.config.get("default_location", ""))

    def _key(self, summary: Summary) -> _Key:
        return _Key(
            company_norm(summary.company, self.s.aliases),
            title_norm(summary.title, self.s.gaz),
            city_norm(summary.location, self.s.gaz),
        )

    # Both dedupe lookups match only jobs seen within the repost window. One not seen for longer is an
    # old posting, so the same position now is a new opening: a fresh job, without the old one's status.
    _SEEN_RECENTLY = "EXISTS (SELECT 1 FROM sightings seen WHERE seen.job_id = j.id AND seen.last_seen_at >= ?)"

    @staticmethod
    def _repost_cutoff() -> str:
        return (datetime.now(UTC) - REPOST_WINDOW).isoformat(timespec="seconds")

    def _job_by_key(self, key: _Key) -> sqlite3.Row | None:
        if is_agency(key.company, self.s.agencies):
            return None  # agency posts hide the employer and are never merged
        return self.conn.execute(
            f"SELECT * FROM jobs j WHERE company_norm = ? AND title_norm = ? AND city_norm = ? "
            f"AND {self._SEEN_RECENTLY} ORDER BY id DESC LIMIT 1",
            (key.company, key.title, key.city, self._repost_cutoff()),
        ).fetchone()

    def _recent_jobs_at(self, company: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT j.id, j.title_norm, s.description FROM jobs j "
            "JOIN sightings s ON s.id = j.primary_sighting_id "
            f"WHERE j.company_norm = ? AND j.stage NOT IN ('code_removed', 'removed') AND {self._SEEN_RECENTLY}",
            (company, self._repost_cutoff()),
        ).fetchall()

    # --- writes ---------------------------------------------------------------------------

    def _insert_sighting(self, summary: Summary, url: str, source_id: str, board_id: int | None, job_id: int | None) -> int:
        now = db.utc_now()
        description = summary.description
        cur = self.conn.execute(
            """
            INSERT INTO sightings (source_id, board_id, external_id, url, title, company_raw, location_raw,
                posted_at, description, desc_hash, source_fields, first_seen_at, last_seen_at, job_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, board_id, summary.external_id, url, summary.title, summary.company,
                summary.location, summary.posted_at, description,
                hashlib.sha1(description.encode("utf-8")).hexdigest() if description else None,
                db.dumps({k: v for k, v in summary.fields.items() if v not in (None, "")}),
                now, now, job_id,
            ),
        )
        return cur.lastrowid

    def _create(
        self,
        summary: Summary,
        url: str,
        source: sqlite3.Row,
        board_id: int | None,
        key: _Key,
        stage: str,
        removal: rules.Removal | None,
    ) -> int:
        prescore = 0.0
        if stage == "queued":
            prescore = rules.prescore(
                title=summary.title, location=summary.location, posted_at=summary.posted_at,
                fields=summary.fields, gaz=self.s.gaz, profile=self.s.cfg.profile, rates=self.s.cfg.currency,
                description_text=to_text(summary.description),
            )
        with db.transaction(self.conn):
            sighting_id = self._insert_sighting(summary, url, source["id"], board_id, None)
            job_id = self.conn.execute(
                "INSERT INTO jobs (company_norm, title_norm, city_norm, primary_sighting_id, stage, "
                "removal_rule, removal_quote, prescore, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key.company, key.title, key.city, sighting_id, stage,
                 removal.rule if removal else None, removal.quote[:500] if removal else None,
                 prescore, db.utc_now()),
            ).lastrowid
            self.conn.execute("UPDATE sightings SET job_id = ? WHERE id = ?", (job_id, sighting_id))
        return job_id

    def _attach(
        self, job: sqlite3.Row, summary: Summary, url: str, source: sqlite3.Row, board_id: int | None
    ) -> None:
        """Add a sighting to an existing job, moving the primary link if the new one is better."""
        with db.transaction(self.conn):
            sighting_id = self._insert_sighting(summary, url, source["id"], board_id, job["id"])
            current = self.conn.execute(
                "SELECT s.source_id, s.company_raw FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id "
                "WHERE j.id = ?",
                (job["id"],),
            ).fetchone()
            if current is None or self._rank(source["id"], summary.company) < self._rank(
                current["source_id"], current["company_raw"]
            ):
                self.conn.execute("UPDATE jobs SET primary_sighting_id = ? WHERE id = ?", (sighting_id, job["id"]))

    def _rank(self, source_id: str, company_raw: str) -> int:
        """Employer's hiring system, then a Japan board, then a global board, then an agent."""
        if is_agency(company_norm(company_raw, self.s.aliases), self.s.agencies):
            return _KIND_RANK["agent"]
        return _KIND_RANK.get(self.s.source_kinds.get(source_id, "global_board"), 2)

    def _pair(self, job_a: int, job_b: int, title_sim: float | None, desc_sim: float, verdict: str | None,
              decided_by: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO dupe_pairs (job_a, job_b, title_sim, desc_sim, verdict, decided_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_a, job_b, title_sim, desc_sim, verdict, decided_by),
        )

    def _touch(self, sighting: sqlite3.Row) -> None:
        self.conn.execute("UPDATE sightings SET last_seen_at = ? WHERE id = ?", (db.utc_now(), sighting["id"]))

    def _mark_board(self, board: sqlite3.Row, *, job_count: int | None, cursor: dict | None = None) -> None:
        """Move the board between tiers: active (has jobs), cold (no jobs), dead (404)."""
        if self.s.dry_run:
            return
        if job_count is None:
            streak = board["dead_streak"] + 1
            tier = "dropped" if streak >= DEAD_MONTHS_TO_DROP else "dead"
            self.conn.execute(
                "UPDATE companies SET tier = ?, dead_streak = ?, last_checked_at = ?, last_job_count = 0 WHERE id = ?",
                (tier, streak, db.utc_now(), board["id"]),
            )
            return
        self.conn.execute(
            "UPDATE companies SET tier = ?, dead_streak = 0, last_checked_at = ?, last_job_count = ?, cursor = ? "
            "WHERE id = ?",
            ("active" if job_count else "cold", db.utc_now(), job_count,
             db.dumps(cursor) if cursor else None, board["id"]),
        )

    @staticmethod
    def _finish(collector: ModuleType) -> None:
        finish = getattr(collector, "finish", None)
        if finish:
            finish()


# --- source health after a run ---------------------------------------------------------------


def fetch_kind(collector: str) -> str:
    return "html" if collector in _HTML_COLLECTORS else "api"


def record_outcome(conn: sqlite3.Connection, cfg: Config, source: sqlite3.Row, tally: Tally,
                   blocked: str | None, *, requests: int) -> None:
    """Keep the run's totals for the Health page, and back off a blocked source or ease a clean one
    back toward its default gap."""
    fetch_cfg = cfg.fetch
    base = source["base_delay_s"] or float(fetch_cfg[f"{fetch_kind(source['collector'])}_gap_s"])
    current = source["min_delay_s"] or base
    now = datetime.now(UTC)
    totals = db.dumps(asdict(tally) | {"requests": requests})
    if blocked:
        conn.execute(
            "UPDATE sources SET backoff_until = ?, min_delay_s = ?, fail_streak = fail_streak + 1, "
            "last_run_at = ?, last_status = ?, last_tally = ? WHERE id = ?",
            ((now + timedelta(hours=20)).isoformat(timespec="seconds"),
             min(float(fetch_cfg["max_gap_s"]), current * 2), now.isoformat(timespec="seconds"),
             f"blocked: {blocked}", totals, source["id"]),
        )
        return
    eased = max(base, current / 2)
    conn.execute(
        "UPDATE sources SET backoff_until = NULL, min_delay_s = ?, fail_streak = 0, last_run_at = ?, "
        "last_status = ?, last_tally = ? WHERE id = ?",
        (None if eased <= base else eased, now.isoformat(timespec="seconds"), tally.status, totals, source["id"]),
    )


def detail_loader(collector: ModuleType, config: dict[str, Any]) -> ModuleType:
    """The module that opens job pages: the source's own collector, or the browser one when the source
    sets detail_browser because its job pages stop plain requests (the list or feed still loads plainly)."""
    return collectors.get("browser") if config.get("detail_browser") else collector


# --- restoring a job the code rules cut ----------------------------------------------------------


def fill_detail(conn: sqlite3.Connection, fetcher: Fetcher, sighting_id: int) -> None:
    """Fetch the description a sighting never got because a code rule removed it first."""
    row = conn.execute(
        "SELECT s.*, src.collector, src.config AS source_config FROM sightings s "
        "JOIN sources src ON src.id = s.source_id WHERE s.id = ?",
        (sighting_id,),
    ).fetchone()
    if row is None or row["description"]:
        return
    board = None
    if row["board_id"] is not None:
        board = conn.execute("SELECT * FROM companies WHERE id = ?", (row["board_id"],)).fetchone()
    config = db.loads(row["source_config"], {}) | (db.loads(board["extra"], {}) if board else {})
    ctx = Context(
        source_id=row["source_id"],
        config=config,
        fetcher=fetcher,
        known=lambda ids: set(),
        deadline=time.time() + 60,
        board=dict(board) if board else None,
    )
    summary = Summary(row["external_id"], row["url"], row["title"], row["company_raw"], row["location_raw"],
                      row["posted_at"], None, db.loads(row["source_fields"], {}))
    detail = detail_loader(collectors.get(row["collector"]), config)
    try:
        filled = detail.fetch_detail(ctx, summary)
    finally:
        Runner._finish(detail)
    description = filled.description or ""
    conn.execute(
        "UPDATE sightings SET description = ?, desc_hash = ?, source_fields = ? WHERE id = ?",
        (description, hashlib.sha1(description.encode("utf-8")).hexdigest() if description else None,
         db.dumps({k: v for k, v in filled.fields.items() if v not in (None, "")}), sighting_id),
    )
