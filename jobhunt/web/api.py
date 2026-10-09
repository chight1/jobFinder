"""The JSON API behind the Vue app in web/, mounted at /api. Interactive docs at /api/docs.

No `from __future__ import annotations` here: FastAPI reads the route signatures, and Conn is local to
create_api, which string annotations could not reach."""

import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Callable, Iterator, Literal, get_args

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from jobhunt import db, profile_file, rules, score
from jobhunt.backup import backup
from jobhunt.collectors import PostingGone
from jobhunt.collectors.runner import fill_detail
from jobhunt.config import Config, reload_skills
from jobhunt.fetch import Fetcher, FetchError
from jobhunt.rules import Gazetteer
from jobhunt.text import to_text
from jobhunt.web import inspect

log = logging.getLogger(__name__)

Status = Literal["none", "apply", "applied", "interviewing", "rejected", "ghosted", "not_interested"]
Rating = Literal["green", "yellow", "red"]
View = Literal["new", "all", "japanese", "removed", "applied", "not_interested"]

APPLIED_STATUSES = tuple(s for s in get_args(Status) if s not in ("none", "not_interested"))
_OPEN = "j.closed_at IS NULL AND j.status = 'none'"
_LISTED = f"j.stage IN ('kept', 'extract_failed') AND {_OPEN}"  # New and All open
_WHERE: dict[str, str] = {
    "new": _LISTED,  # and first seen since the last visit, added in /jobs
    "all": _LISTED,
    "japanese": f"j.stage = 'japanese' AND {_OPEN}",  # the backup list
    "removed": "j.stage IN ('code_removed', 'removed')",
    "applied": f"j.status IN ({', '.join(repr(s) for s in APPLIED_STATUSES)})",
    "not_interested": "j.status = 'not_interested'",
}
_ORDER: dict[str, str] = {
    "removed": "j.first_seen_at DESC LIMIT 500",
    "applied": "j.status_at DESC",
    "not_interested": "j.status_at DESC",
}
_JOB_COLUMNS = """
    SELECT j.*, s.title, s.company_raw, s.location_raw, s.url, src.name AS source_name,
           EXISTS (SELECT 1 FROM packets p WHERE p.job_id = j.id) AS has_packet
    FROM jobs j
    JOIN sightings s ON s.id = j.primary_sighting_id
    JOIN sources src ON src.id = s.source_id
"""


# --- models ------------------------------------------------------------------------------------


class SkillRow(BaseModel):
    key: str = Field(description="The merged name every spelling maps to, e.g. 'javascript' for 'JS'")
    name: str = Field(description="The most common spelling")
    jobs: int = Field(description="How many of the counted jobs name it")
    share: float = Field(description="Fraction of the counted jobs that name it")
    main: int = Field(description="How many name it as the primary language")
    level: int = Field(description="Your level in profile.toml; 0 when it is not there")
    avg_score: float | None = Field(description="Average score of its jobs; null when none are scored")


class ProfileSkill(BaseModel):
    name: str
    level: int


class SkillsPage(BaseModel):
    skills: list[SkillRow]
    job_count: int = Field(description="How many jobs were counted")
    unseen: list[ProfileSkill] = Field(description="Skills in your profile that none of the counted jobs name")
    levels: list[int] = Field(description="The levels a skill can have; 0 takes it out of the profile")


class SkillLevel(BaseModel):
    name: str = Field(description="Any spelling; an entry already in the profile is changed in place")
    level: int


class SkillLevelSaved(BaseModel):
    name: str = Field(description="The name the skill is stored under in profile.toml")
    level: int
    rescored: int = Field(description="How many jobs were rescored with the new level")


class Link(BaseModel):
    name: str
    url: str


class SkillChip(BaseModel):
    name: str = Field(description="As the posting spells it")
    key: str = Field(description="The merged name, for the skill's job list")
    level: int = Field(description="Your level in it; 0 when it is not in your profile")


class JobCard(BaseModel):
    """One job as a tile shows it."""

    id: int
    title: str
    url: str = Field(description="The primary sighting's link")
    company: str
    work_mode: str | None
    place: str = Field(description="The office city; else, for a job that is not remote, the posting's location")
    pay_usd: str | None = Field(description="Yearly pay in dollars, e.g. '$66k–$100k'; null when not listed")
    pay: str = Field(description="In the posting's currency and in dollars, e.g. '9,000,000 JPY/year ($60k)'")
    primary_language: str | None
    language_level: int = Field(description="Your level in the primary language; 0 when not in your profile")
    years_asked: float | None = Field(description="Years of experience the posting asks for")
    years_over: bool = Field(description="It asks for more years than you have")
    score: float | None
    badges: list[str]
    rating: Rating | None
    status: Status
    status_at: str | None
    follow_up_on: str | None = Field(description="ISO date to follow up an application")
    removal_rule: str | None
    removal_quote: str | None
    source: str = Field(description="Where the primary sighting was seen")
    others: list[Link] = Field(description="Other places the same job was seen")
    probably_same: list[int] = Field(description="Jobs judged probably the same position")
    has_packet: bool
    skills: list[SkillChip]


class JobList(BaseModel):
    jobs: list[JobCard]
    now: str = Field(description="Pass back as `after` to get only the jobs listed since")
    rules: list[str] = Field(description="Removed: every removal rule, for the filter")


class SightingRow(BaseModel):
    id: int
    source: str
    url: str
    title: str
    company: str
    location: str
    first_seen_at: str
    last_seen_at: str
    closed_at: str | None
    primary: bool = Field(description="The sighting whose link and text the job shows")


class ModelCall(BaseModel):
    at: str
    kind: str = Field(description="extract, dupe or packet")
    seconds: float | None
    ok: bool
    error: str | None


class DupePair(BaseModel):
    job_a: int
    job_b: int
    verdict: str | None = Field(description="same, different or probably_same; null while waiting for the model")
    decided_by: str | None


class JobDetail(BaseModel):
    """Everything known about one job, for its page."""

    id: int
    title: str
    url: str | None
    company: str
    location: str
    badges: list[str]
    stage: str
    closed_at: str | None
    removal_rule: str | None
    removal_quote: str | None
    restored_rules: list[str] = Field(description="Removal rules this job was restored from; they never remove it again")
    rating: Rating | None
    status: Status
    status_at: str | None
    follow_up_on: str | None
    first_seen_at: str
    listed_at: str | None
    prescore: float = Field(description="Queue order for the model")
    dedupe_key: list[str] = Field(description="Normalized company, title and city: equal keys are the same job")
    score: float | None
    score_parts: dict[str, float]
    reason_line: str | None
    facts: dict[str, Any] = Field(description="What the model read from the posting, evidence aside")
    evidence: dict[str, str] = Field(description="The model's quote behind each removal-relevant fact")
    sightings: list[SightingRow]
    source_fields: dict[str, Any] = Field(description="The primary sighting's own fields from its source")
    model_calls: list[ModelCall]
    pairs: list[DupePair]
    description: str


class SourceHealth(BaseModel):
    id: str
    name: str
    enabled: bool
    last_run_at: str | None
    last_status: str | None
    requests: int = Field(description="From the source's latest run, nightly or by hand")
    parsed: int
    new_jobs: int
    errors: int
    average: float | None = Field(description="New jobs per night over the last 7 nightly runs; null before any")
    backoff_until: str | None
    min_delay_s: float | None = Field(description="This source's own gap between requests, when it has one")
    fail_streak: int
    flags: list[str] = Field(description="disabled, blocked or parser broken")


class RunTotals(BaseModel):
    started_at: str
    fetched: int = 0
    new_sightings: int = 0
    new_jobs: int = 0
    removed_by_code: int = 0
    extracted: int = 0
    removed_after_extraction: int = 0
    failed: int = 0
    closed: int = 0


class BackupState(BaseModel):
    at: str | None = Field(description="When the last backup ran (UTC); null before the first")
    status: str | None = Field(description="Its result; starts with 'failed' when it failed")
    folder: str = Field(description="The data repository it commits to")


class Health(BaseModel):
    seconds_per_job: float | None = Field(description="Average over the last 200 extractions")
    queue: int
    nights: float | None = Field(description="Nights the queue takes at that speed")
    model_flags: list[str] = Field(description="extraction failing, model behind")
    last_run: RunTotals | None
    boards: dict[str, int] = Field(description="Company boards per tier")
    backup: BackupState
    sources: list[SourceHealth]


class Worker(BaseModel):
    worker: str = Field(description="e.g. run:nightly, fetch:tokyodev, model")
    doing: str
    age: str = Field(description="How long since it last reported, e.g. '2m 05s'")
    stale: bool = Field(description="No report in a long time: its process has probably stopped")


class Progress(BaseModel):
    queue: int = Field(description="Jobs waiting for the model")
    extracted_last_hour: int
    requests_last_hour: int
    seconds_per_job: float | None = Field(description="Average over the last 20 extractions")
    time_to_clear: str | None = Field(description="Model time the queue takes at that speed")


class Event(BaseModel):
    at: str
    kind: Literal["web", "model"]
    who: str = Field(description="The source of a request, or the kind of model call")
    what: str = Field(description="HTTP status, 'no reply', or ok / failed / invalid reply")
    detail: str = Field(description="The URL requested, or '#id' of the job a model call was about")
    seconds: float | None
    error: str | None
    bad: bool


class Activity(BaseModel):
    running: list[Worker]
    progress: Progress
    feed: list[Event] = Field(description="The latest web requests and model calls, newest first")
    log: list[str] = Field(description="The last lines of jobhunt.log")


class TablePage(BaseModel):
    table: str
    columns: list[str]
    rows: list[list[str]] = Field(description="Cells as text, newest row first; long cells are cut short")
    total: int = Field(description="Rows matching the search")
    page: int
    pages: int
    tables: dict[str, int] = Field(description="Every table the page can show, with its row count")


class Packet(BaseModel):
    job_id: int
    text: str = Field(description="The application packet, as Markdown")


class StatusChange(BaseModel):
    status: Status


class StatusSaved(BaseModel):
    status: Status
    status_at: str | None
    follow_up_on: str | None
    counts: dict[str, int] = Field(description="The top bar's counts after the change")


class RatingChange(BaseModel):
    rating: Rating


class RatingSaved(BaseModel):
    rating: Rating | None = Field(description="null when the same color was picked again, which clears it")


class Restored(BaseModel):
    stage: str = Field(description="queued for a code-removed job, else the stage its facts now give")
    counts: dict[str, int]


# --- shared with the server-rendered pages ----------------------------------------------------------


def tab_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many records each tab in the top bar holds."""
    since = db.get_setting(conn, "last_list_visit", "")

    def one(sql: str, *params: Any) -> int:
        return conn.execute(sql, params).fetchone()[0]

    return {
        "New": one(f"SELECT COUNT(*) FROM jobs j WHERE {_LISTED} AND j.first_seen_at > ?", since),
        "All open": one(f"SELECT COUNT(*) FROM jobs j WHERE {_WHERE['all']}"),
        "Japanese": one(f"SELECT COUNT(*) FROM jobs j WHERE {_WHERE['japanese']}"),
        "Removed": one(f"SELECT COUNT(*) FROM jobs j WHERE {_WHERE['removed']}"),
        "Applied": one(f"SELECT COUNT(*) FROM jobs j WHERE {_WHERE['applied']}"),
        "Not interested": one(f"SELECT COUNT(*) FROM jobs j WHERE {_WHERE['not_interested']}"),
        "Skills": inspect.distinct_skill_count(conn),
    }


def health_report(conn: sqlite3.Connection, cfg: Config) -> Health:
    runs = [dict(r) | {"counts": db.loads(r["counts"], {})} for r in
            conn.execute("SELECT * FROM runs WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 7")]
    last = runs[0]["counts"] if runs else {}
    disable_after = int(cfg.fetch["disable_after_fails"])

    sources = []
    for src in conn.execute("SELECT * FROM sources WHERE collector NOT IN ('manual') ORDER BY name COLLATE NOCASE"):
        tally = db.loads(src["last_tally"], {})  # its latest run, nightly or by hand
        nightly_new = [r["counts"].get("sources", {}).get(src["id"], {}).get("new_jobs", 0) for r in runs]
        average = sum(nightly_new) / len(nightly_new) if nightly_new else None
        flags = []
        if src["fail_streak"] >= disable_after:
            flags.append("disabled")
        elif src["fail_streak"] > 0:
            flags.append("blocked")
        if tally.get("requests", 0) > 0 and tally.get("parsed", 0) == 0 and (average or 0) >= 5:
            flags.append("parser broken")
        sources.append(SourceHealth(
            id=src["id"], name=src["name"], enabled=bool(src["enabled"]), last_run_at=src["last_run_at"],
            last_status=src["last_status"], requests=tally.get("requests", 0), parsed=tally.get("parsed", 0),
            new_jobs=tally.get("new_jobs", 0), errors=tally.get("errors", 0), average=average,
            backoff_until=src["backoff_until"], min_delay_s=src["min_delay_s"] or None,
            fail_streak=src["fail_streak"], flags=flags,
        ))

    seconds = conn.execute(
        "SELECT AVG(seconds) FROM (SELECT seconds FROM llm_log WHERE kind = 'extract' AND ok = 1 "
        "ORDER BY id DESC LIMIT 200)"
    ).fetchone()[0]
    queue = conn.execute("SELECT COUNT(*) FROM jobs WHERE stage = 'queued'").fetchone()[0]
    start = datetime.strptime(cfg.schedule["start"], "%H:%M")
    stop = datetime.strptime(cfg.schedule["model_stop"], "%H:%M")
    window_s = (stop - start).total_seconds() % 86400
    per_night = window_s / seconds if seconds else None
    nights = queue / per_night if per_night else None

    extracted, failed = last.get("extracted", 0), last.get("failed", 0)
    model_flags = []
    if extracted + failed and failed / (extracted + failed) > 0.10:
        model_flags.append("extraction failing")
    if nights is not None and nights > 3:
        model_flags.append("model behind")

    totals = {k: v for k, v in last.items() if k in RunTotals.model_fields and isinstance(v, int)}
    return Health(
        seconds_per_job=seconds,
        queue=queue,
        nights=nights,
        model_flags=model_flags,
        last_run=RunTotals(started_at=runs[0]["started_at"], **totals) if runs else None,
        boards={r["tier"]: r["n"] for r in conn.execute("SELECT tier, COUNT(*) AS n FROM companies GROUP BY tier")},
        backup=_backup_state(conn, cfg),
        sources=sources,
    )


def _backup_state(conn: sqlite3.Connection, cfg: Config) -> BackupState:
    return BackupState(at=db.get_setting(conn, "last_backup_at"), status=db.get_setting(conn, "last_backup_status"),
                       folder=str(cfg.backup_dir))


# --- the API ---------------------------------------------------------------------------------------


def create_api(cfg: Config, gazetteer: Callable[[], Gazetteer]) -> FastAPI:
    """gazetteer loads GeoNames on first need."""
    api = FastAPI(title="jobhunt API", docs_url="/docs", redoc_url=None)

    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(connection)]

    def pay_range_usd(facts: dict[str, Any]) -> str | None:
        """Yearly pay in dollars, rounded to thousands: '$66k–$100k', or '$73k' for one figure."""
        ends = [score.annual_usd(v, None, facts.get("pay_currency"), facts.get("pay_period"), cfg.currency)
                for v in (facts.get("pay_min"), facts.get("pay_max"))]
        thousands = sorted({round(v / 1000) for v in ends if v is not None})
        return "–".join(f"${k:,}k" for k in thousands) or None

    def cards(conn: sqlite3.Connection, where: str, params: list[Any], order: str) -> list[JobCard]:
        """Tiles for the jobs matching where, with other sightings and probable duplicates attached."""
        rows = conn.execute(f"{_JOB_COLUMNS} WHERE {where} ORDER BY {order}", params).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        others: dict[int, list[Link]] = defaultdict(list)
        for s in conn.execute(
            f"SELECT s.job_id, s.url, src.name FROM sightings s JOIN sources src ON src.id = s.source_id "
            f"JOIN jobs j ON j.id = s.job_id WHERE s.job_id IN ({marks}) AND s.id != j.primary_sighting_id",
            ids,
        ):
            others[s["job_id"]].append(Link(name=s["name"], url=s["url"]))
        probably: dict[int, list[int]] = defaultdict(list)
        for p in conn.execute(
            f"SELECT job_a, job_b FROM dupe_pairs WHERE verdict = 'probably_same' AND job_b IN ({marks})", ids
        ):
            probably[p["job_b"]].append(p["job_a"])
        return [card(row, others[row["id"]], probably[row["id"]]) for row in rows]

    def card(row: sqlite3.Row, others: list[Link], probably_same: list[int]) -> JobCard:
        facts = db.loads(row["facts"], {}) or {}
        usd = score.annual_usd(facts.get("pay_min"), facts.get("pay_max"), facts.get("pay_currency"),
                               facts.get("pay_period"), cfg.currency)
        years = facts.get("years_required")
        return JobCard(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            company=row["company_raw"],
            work_mode=facts.get("work_mode"),
            # A remote job's raw location is often a list of regions or a whole line of the posting.
            place=facts.get("office_city") or ("" if facts.get("work_mode") == "remote" else row["location_raw"]),
            pay_usd=pay_range_usd(facts),
            pay=score.pay_text(facts, usd),
            primary_language=facts.get("primary_language"),
            language_level=score.skill_level(facts.get("primary_language"), cfg.profile) or 0,
            years_asked=years,
            years_over=years is not None and years > cfg.profile.years_experience,
            score=row["score"],
            badges=db.loads(row["badges"], []),
            rating=row["rating"],
            status=row["status"],
            status_at=row["status_at"],
            follow_up_on=row["follow_up_on"],
            removal_rule=row["removal_rule"],
            removal_quote=row["removal_quote"],
            source=row["source_name"],
            others=others,
            probably_same=probably_same,
            has_packet=bool(row["has_packet"]),
            skills=[
                SkillChip(name=s, key=score.canonical_skill(s), level=score.skill_level(s, cfg.profile))
                for s in facts.get("stack") or [] if s.strip()
            ],
        )

    @api.get("/tabs")
    def tabs(conn: Conn) -> dict[str, int]:
        """How many records each tab in the top bar holds."""
        return tab_counts(conn)

    # --- job lists ---

    @api.get("/jobs")
    def jobs(conn: Conn, view: View, rule: str | None = None, after: str | None = None) -> JobList:
        """One tab's jobs. rule narrows Removed to one removal rule. after (an earlier reply's `now`) narrows
        New and All open to the jobs listed since, for an open page to add as the model finishes them."""
        now = db.utc_now()
        where, params = _WHERE[view], []
        if view == "new":
            where += " AND j.first_seen_at > ?"
            params.append(db.get_setting(conn, "last_list_visit", ""))
        if after:
            where += " AND j.listed_at > ? AND j.listed_at <= ?"
            params += [after, now]
        if view == "removed" and rule:
            where += " AND j.removal_rule = ?"
            params.append(rule)
        found = cards(conn, where, params, _ORDER.get(view, "j.score DESC"))
        rule_names = []
        if view == "removed":
            rule_names = [r[0] for r in conn.execute(
                "SELECT DISTINCT removal_rule FROM jobs WHERE removal_rule IS NOT NULL ORDER BY 1")]
        return JobList(jobs=found, now=now, rules=rule_names)

    @api.get("/jobs/{job_id}")
    def job(job_id: int, conn: Conn) -> JobDetail:
        """Everything known about one job: state, score parts, the model's facts and evidence, every place
        it was seen, its model calls and duplicate checks, and the description."""
        detail = inspect.job_detail(conn, job_id)
        if detail is None:
            raise HTTPException(404, f"no job #{job_id}")
        job, primary, facts = detail["job"], detail["primary"], dict(detail["facts"])
        return JobDetail(
            id=job_id,
            title=primary["title"] if primary else "",
            url=primary["url"] if primary else None,
            company=primary["company_raw"] if primary else "",
            location=primary["location_raw"] if primary else "",
            badges=detail["badges"],
            stage=job["stage"],
            closed_at=job["closed_at"],
            removal_rule=job["removal_rule"],
            removal_quote=job["removal_quote"],
            restored_rules=detail["restored_rules"],
            rating=job["rating"],
            status=job["status"],
            status_at=job["status_at"],
            follow_up_on=job["follow_up_on"],
            first_seen_at=job["first_seen_at"],
            listed_at=job["listed_at"],
            prescore=job["prescore"],
            dedupe_key=[job["company_norm"], job["title_norm"], job["city_norm"]],
            score=job["score"],
            score_parts=detail["score_parts"],
            reason_line=job["reason_line"],
            evidence=facts.pop("evidence", None) or {},
            facts=facts,
            sightings=[
                SightingRow(id=s["id"], source=s["source_name"], url=s["url"], title=s["title"],
                            company=s["company_raw"], location=s["location_raw"], first_seen_at=s["first_seen_at"],
                            last_seen_at=s["last_seen_at"], closed_at=s["closed_at"],
                            primary=bool(primary) and s["id"] == primary["id"])
                for s in detail["sightings"]
            ],
            source_fields=primary["fields"] if primary else {},
            model_calls=[ModelCall(**dict(call)) for call in detail["model_calls"]],
            pairs=[DupePair(job_a=p["job_a"], job_b=p["job_b"], verdict=p["verdict"], decided_by=p["decided_by"])
                   for p in detail["pairs"]],
            description=detail["description"],
        )

    # --- data and packets ---

    @api.get("/data/{table}")
    def data(table: str, conn: Conn, q: str = "", page: Annotated[int, Query(ge=1)] = 1) -> TablePage:
        """One page of a table, read-only, newest rows first; q keeps rows with that text in any column."""
        try:
            contents = inspect.table_page(conn, table, q.strip(), page)
        except KeyError:
            raise HTTPException(404, f"no table named {table!r}") from None
        return TablePage(table=table, page=page, tables=inspect.table_counts(conn), **contents)

    @api.get("/packets/{job_id}")
    def packet(job_id: int) -> Packet:
        """The application packet drafted for a job marked apply."""
        path = cfg.packets_dir / f"{job_id}.md"
        if not path.exists():
            raise HTTPException(404, "no packet for this job yet")
        return Packet(job_id=job_id, text=path.read_text(encoding="utf-8"))

    # --- activity ---

    @api.get("/activity")
    def activity(conn: Conn) -> Activity:
        """What each running command and worker is doing, queue progress, the latest web requests and model
        calls in time order, and the end of the log. The page asks again every few seconds."""
        snapshot = inspect.activity_snapshot(conn, cfg.data_dir / "jobhunt.log")
        return Activity(
            running=[Worker(**item) for item in snapshot["running"]],
            progress=Progress(**snapshot["progress"]),
            feed=[Event(**(dict(event) | {"bad": bool(event["bad"])})) for event in snapshot["feed"]],
            log=snapshot["log"],
        )

    # --- health ---

    @api.get("/health")
    def health(conn: Conn) -> Health:
        """How each source and the model are doing, and the last backup."""
        return health_report(conn, cfg)

    @api.post("/sources/{source_id}/enable", status_code=204)
    def enable_source(source_id: str, conn: Conn) -> None:
        """Turn a source back on after failures: its fail streak and backoff are cleared."""
        if not conn.execute("UPDATE sources SET fail_streak = 0, backoff_until = NULL WHERE id = ?",
                            (source_id,)).rowcount:
            raise HTTPException(404, f"no source {source_id!r}")

    @api.post("/backup")
    def backup_now(conn: Conn) -> BackupState:
        """Commit and push the data now, as the nightly run does."""
        backup(cfg)  # records its own time and result
        return _backup_state(conn, cfg)

    @api.post("/seen", status_code=204)
    def seen(conn: Conn) -> None:
        """Mark everything seen: New then holds only jobs first seen after now."""
        db.set_setting(conn, "last_list_visit", db.utc_now())

    @api.post("/jobs/{job_id}/status")
    def set_status(job_id: int, body: StatusChange, conn: Conn) -> StatusSaved:
        """Set a job's status. "none" clears it, and the job goes back to the lists as if never marked.
        Marking a job applied sets a follow-up date."""
        if body.status == "none":
            conn.execute("UPDATE jobs SET status = 'none', status_at = NULL, follow_up_on = NULL WHERE id = ?",
                         (job_id,))
        else:
            follow_up = None
            if body.status == "applied":
                follow_up = (date.today() + timedelta(days=int(cfg.list["follow_up_days"]))).isoformat()
            conn.execute(
                "UPDATE jobs SET status = ?, status_at = ?, follow_up_on = COALESCE(?, follow_up_on) WHERE id = ?",
                (body.status, db.utc_now(), follow_up, job_id),
            )
        row = conn.execute("SELECT status_at, follow_up_on FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no job #{job_id}")
        return StatusSaved(status=body.status, status_at=row["status_at"], follow_up_on=row["follow_up_on"],
                           counts=tab_counts(conn))

    @api.post("/jobs/{job_id}/rating")
    def set_rating(job_id: int, body: RatingChange, conn: Conn) -> RatingSaved:
        """Mark how much the candidate likes a job. Picking the current color again clears it."""
        row = conn.execute("SELECT rating FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no job #{job_id}")
        new = None if row["rating"] == body.rating else body.rating
        conn.execute("UPDATE jobs SET rating = ? WHERE id = ?", (new, job_id))
        return RatingSaved(rating=new)

    @api.post("/jobs/{job_id}/restore")
    def restore(job_id: int, conn: Conn) -> Restored:
        """Put a removed job back; the same rule never removes it again. A job the model read is judged
        again from its facts; a code-removed one goes to the model queue."""
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["stage"] not in ("code_removed", "removed"):
            raise HTTPException(404, "no removed job with that id")
        restored = db.dumps(sorted(set(db.loads(job["restored_rules"], [])) | {job["removal_rule"]}))
        if job["stage"] == "removed":
            with db.transaction(conn):
                conn.execute("UPDATE jobs SET restored_rules = ? WHERE id = ?", (restored, job_id))
                stage = score.apply_facts(conn, conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(),
                                          cfg, gazetteer())
        else:
            requeue(conn, job, restored)
            stage = "queued"
        return Restored(stage=stage, counts=tab_counts(conn))

    def requeue(conn: sqlite3.Connection, job: sqlite3.Row, restored: str) -> None:
        """A code-removed job goes to the model queue, with the description it never had and a fresh prescore."""
        fetcher = Fetcher(cfg, run_started=datetime.now().astimezone())
        try:
            fill_detail(conn, fetcher, job["primary_sighting_id"])
        except (FetchError, PostingGone) as exc:
            log.warning("restore of job %s: detail fetch failed: %r", job["id"], exc)
        finally:
            fetcher.close()
        sighting = conn.execute("SELECT * FROM sightings WHERE id = ?", (job["primary_sighting_id"],)).fetchone()
        prescore = rules.prescore(
            title=sighting["title"], location=sighting["location_raw"], posted_at=sighting["posted_at"],
            fields=db.loads(sighting["source_fields"], {}), gaz=gazetteer(), profile=cfg.profile, rates=cfg.currency,
            description_text=to_text(sighting["description"]),
        )
        conn.execute(
            "UPDATE jobs SET stage = 'queued', removal_rule = NULL, removal_quote = NULL, restored_rules = ?, "
            "prescore = ? WHERE id = ?",
            (restored, prescore, job["id"]),
        )

    # --- skills ---

    @api.get("/skills")
    def skills(conn: Conn, scope: Literal["kept", "all"] = "kept", japanese: bool = False) -> SkillsPage:
        """Every skill the jobs name, most common first. scope "kept" counts the open jobs still in play,
        "all" every job the model has read; jobs that require Japanese count only with japanese=true."""
        reload_skills(cfg)  # pick up hand edits to profile.toml too
        stats = inspect.skill_stats(conn, cfg.profile, scope, japanese=japanese)
        return SkillsPage(
            skills=[SkillRow(**row) for row in stats["skills"]],
            job_count=stats["job_count"],
            unseen=[ProfileSkill(name=n, level=lvl) for n, lvl in inspect.unseen_skills(cfg.profile, stats)],
            levels=list(profile_file.LEVELS),
        )

    @api.get("/skills/{key}/jobs")
    def skill_jobs(key: str, conn: Conn, japanese: bool = False) -> JobList:
        """The open kept jobs that ask for one skill (any spelling), best first; the Japanese ones too when asked."""
        ids = inspect.job_ids_with_skill(conn, score.canonical_skill(key), japanese=japanese)
        found = cards(conn, f"j.id IN ({','.join('?' * len(ids))})", ids, "j.score DESC") if ids else []
        return JobList(jobs=found, now=db.utc_now(), rules=[])

    @api.post("/skills/level")
    def set_skill_level(body: SkillLevel, conn: Conn) -> SkillLevelSaved:
        """Change a skill's level in profile.toml (0 removes it), then rescore every job that has facts."""
        try:
            stored = profile_file.set_skill_level(cfg.profile_path, body.name, body.level)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        reload_skills(cfg)
        return SkillLevelSaved(name=stored, level=body.level, rescored=score.rescore_all(conn, cfg, gazetteer()))

    return api
