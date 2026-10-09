"""The list page: server-rendered views over the database. Small scripts save tile and skill changes in place."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db, rules, score
from jobhunt.backup import backup
from jobhunt.collectors import PostingGone
from jobhunt.collectors.runner import fill_detail
from jobhunt.config import Config
from jobhunt.fetch import Fetcher, FetchError
from jobhunt.rules import Gazetteer
from jobhunt.text import to_text
from jobhunt.web import inspect
from jobhunt.web.api import create_api

log = logging.getLogger(__name__)

STATUSES = ("apply", "applied", "interviewing", "rejected", "ghosted", "not_interested")
RATINGS = ("green", "yellow", "red")
# How each status reads on the page, and the button that sets it. "apply" only drafts a packet,
# so its button says so, while the job itself reads as one still to apply to.
STATUS_NAMES = {"apply": "to apply", "not_interested": "not interested"}
BUTTON_LABELS = {"apply": "draft packet", "not_interested": "not interested"}
_HERE = Path(__file__).parent
_LISTED = "j.stage IN ('kept', 'extract_failed') AND j.closed_at IS NULL AND j.status = 'none'"
_JAPANESE = "j.stage = 'japanese' AND j.closed_at IS NULL AND j.status = 'none'"  # the backup list
APPLIED_STATUSES = tuple(s for s in STATUSES if s != "not_interested")  # the Applied tab; not interested has its own
_APPLIED = f"j.status IN ({', '.join(repr(s) for s in APPLIED_STATUSES)})"
_JOB_COLUMNS = """
    SELECT j.*, s.title, s.company_raw, s.location_raw, s.url, src.name AS source_name,
           EXISTS (SELECT 1 FROM packets p WHERE p.job_id = j.id) AS has_packet
    FROM jobs j
    JOIN sightings s ON s.id = j.primary_sighting_id
    JOIN sources src ON src.id = s.source_id
"""


def create_app(cfg: Config, gazetteer_: Gazetteer | None = None) -> FastAPI:
    """gazetteer_ is for tests; the app otherwise loads GeoNames on first need."""
    app = FastAPI(title="jobhunt", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=_HERE / "templates")

    def pay(facts: dict[str, Any]) -> str:
        usd = score.annual_usd(facts.get("pay_min"), facts.get("pay_max"), facts.get("pay_currency"),
                               facts.get("pay_period"), cfg.currency)
        return score.pay_text(facts, usd)

    def asset(name: str) -> str:
        """A static file's URL with its modification time, so browsers fetch it again after any change."""
        return f"/static/{name}?v={int((_HERE / 'static' / name).stat().st_mtime)}"

    templates.env.globals.update(
        asset=asset,
        statuses=STATUSES, status_names=STATUS_NAMES, button_labels=BUTTON_LABELS, ratings=RATINGS, pay=pay,
        canonical_skill=score.canonical_skill,
        skill_level=lambda name: score.skill_level(name, cfg.profile),
    )
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    gaz: list[Gazetteer] = [gazetteer_] if gazetteer_ else []  # else loaded on first restore or rescore

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        with connection() as conn:
            counts = _tab_counts(conn)
        return templates.TemplateResponse(
            request, name, {"view": name.removesuffix(".html"), "counts": counts, **context}
        )

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/new", status_code=303)

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request) -> HTMLResponse:
        with connection() as conn:
            since = db.get_setting(conn, "last_list_visit", "")
            jobs = _jobs(conn, f"{_LISTED} AND j.first_seen_at > ?", (since,), "j.score DESC")
        return render(request, "list.html", title="New", jobs=jobs, live="new", now=db.utc_now())

    @app.post("/seen")
    def seen() -> RedirectResponse:
        with connection() as conn:
            db.set_setting(conn, "last_list_visit", db.utc_now())
        return RedirectResponse("/new", status_code=303)

    @app.get("/all", response_class=HTMLResponse)
    def all_open(request: Request) -> HTMLResponse:
        with connection() as conn:
            top = score.ordered_top(conn, int(cfg.score["top_n"]))
            jobs = _jobs(conn, _LISTED, (), "j.score DESC")
        position = {job_id: i for i, job_id in enumerate(top)}
        jobs.sort(key=lambda j: (position.get(j["id"], len(top)), -(j["score"] or 0)))
        return render(request, "list.html", title="All open", jobs=jobs, top_count=len(top), live="all",
                      now=db.utc_now())

    @app.get("/japanese", response_class=HTMLResponse)
    def japanese(request: Request) -> HTMLResponse:
        """Jobs that pass every rule but require Japanese, best first, for when the English ones run out."""
        with connection() as conn:
            jobs = _jobs(conn, _JAPANESE, (), "j.score DESC")
        return render(request, "list.html", title="Japanese", jobs=jobs)

    @app.get("/rows", response_class=HTMLResponse)
    def rows(request: Request, view: str, after: str) -> HTMLResponse:
        """Rows for jobs listed since `after`, for an open New or All open page to add without a reload.

        The X-Now header is the time to ask from next, so no job is missed between two checks.
        """
        now = db.utc_now()
        where = f"{_LISTED} AND j.listed_at > ? AND j.listed_at <= ?"
        params: tuple = (after, now)
        if view == "new":
            with connection() as conn:
                since = db.get_setting(conn, "last_list_visit", "")
            where += " AND j.first_seen_at > ?"
            params += (since,)
        with connection() as conn:
            jobs = _jobs(conn, where, params, "j.score DESC")
        response = templates.TemplateResponse(request, "_rows.html", {"jobs": jobs})
        response.headers["X-Now"] = now
        return response

    @app.get("/removed", response_class=HTMLResponse)
    def removed(request: Request, rule: str = "") -> HTMLResponse:
        with connection() as conn:
            rules = [r["removal_rule"] for r in conn.execute(
                "SELECT DISTINCT removal_rule FROM jobs WHERE removal_rule IS NOT NULL ORDER BY 1")]
            where = "j.stage IN ('code_removed', 'removed')" + (" AND j.removal_rule = ?" if rule else "")
            jobs = _jobs(conn, where, (rule,) if rule else (), "j.first_seen_at DESC LIMIT 500")
        return render(request, "removed.html", title="Removed", jobs=jobs, rules=rules, rule=rule)

    @app.get("/applied", response_class=HTMLResponse)
    def applied(request: Request) -> HTMLResponse:
        with connection() as conn:
            jobs = _jobs(conn, _APPLIED, (), "j.status_at DESC")
        return render(request, "applied.html", title="Applied", jobs=jobs, holds=APPLIED_STATUSES,
                      empty="Nothing applied for yet.", today=date.today().isoformat())

    @app.get("/not-interested", response_class=HTMLResponse)
    def not_interested(request: Request) -> HTMLResponse:
        with connection() as conn:
            jobs = _jobs(conn, "j.status = 'not_interested'", (), "j.status_at DESC")
        return render(request, "applied.html", title="Not interested", jobs=jobs, holds=("not_interested",),
                      empty="Nothing marked not interested.", today=date.today().isoformat())

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> HTMLResponse:
        with connection() as conn:
            context = _health(conn, cfg)
        return render(request, "health.html", title="Health", **context)

    @app.post("/jobs/{job_id}/status")
    def set_status(job_id: int, status: str = Form(...)) -> JSONResponse:
        """Set a job's status from a tile; the reply carries the fresh tab counts for the page to show."""
        if status not in STATUSES and status != "none":
            raise HTTPException(400, f"unknown status {status!r}")
        follow_up = None
        if status == "applied":
            follow_up = (date.today() + timedelta(days=int(cfg.list["follow_up_days"]))).isoformat()
        with connection() as conn:
            if status == "none":  # cleared: the job goes back to the lists as if never marked
                conn.execute("UPDATE jobs SET status = 'none', status_at = NULL, follow_up_on = NULL WHERE id = ?",
                             (job_id,))
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, status_at = ?, follow_up_on = COALESCE(?, follow_up_on) WHERE id = ?",
                    (status, db.utc_now(), follow_up, job_id),
                )
            counts = _tab_counts(conn)
        return JSONResponse({"status": status, "name": STATUS_NAMES.get(status, status), "counts": counts})

    def gazetteer() -> Gazetteer:
        if not gaz:
            gaz.append(Gazetteer.from_geonames(cfg.geonames_dir))
        return gaz[0]

    @app.post("/jobs/{job_id}/rating")
    def set_rating(job_id: int, rating: str = Form(...)) -> JSONResponse:
        """Mark how much the candidate likes a job. Choosing the current color again clears it."""
        if rating not in RATINGS:
            raise HTTPException(400, f"unknown rating {rating!r}")
        with connection() as conn:
            row = conn.execute("SELECT rating FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(404, f"no job #{job_id}")
            new = None if row["rating"] == rating else rating
            conn.execute("UPDATE jobs SET rating = ? WHERE id = ?", (new, job_id))
        return JSONResponse({"rating": new})

    @app.post("/jobs/{job_id}/restore")
    def restore(request: Request, job_id: int) -> RedirectResponse:
        """Put a removed job back; the same rule never removes it again."""
        with connection() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None or job["stage"] not in ("code_removed", "removed"):
                raise HTTPException(404, "no removed job with that id")
            restored = db.dumps(sorted(set(db.loads(job["restored_rules"], [])) | {job["removal_rule"]}))
            if job["stage"] == "removed":
                with db.transaction(conn):
                    conn.execute("UPDATE jobs SET restored_rules = ? WHERE id = ?", (restored, job_id))
                    score.apply_facts(conn, conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(),
                                      cfg, gazetteer())
            else:
                _requeue(conn, job, restored)
        return RedirectResponse(request.headers.get("referer") or "/removed", status_code=303)

    def _requeue(conn: sqlite3.Connection, job: sqlite3.Row, restored: str) -> None:
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

    @app.post("/sources/{source_id}/enable")
    def enable_source(source_id: str) -> RedirectResponse:
        with connection() as conn:
            conn.execute(
                "UPDATE sources SET fail_streak = 0, backoff_until = NULL WHERE id = ?", (source_id,)
            )
        return RedirectResponse("/health", status_code=303)

    @app.get("/skills/{key}", response_class=HTMLResponse)
    def skill_jobs(request: Request, key: str, japanese: bool = False) -> HTMLResponse:
        """The open kept jobs that ask for one skill, and the Japanese ones too when asked."""
        key = score.canonical_skill(key)
        with connection() as conn:
            ids = inspect.job_ids_with_skill(conn, key, japanese=japanese)
            jobs = _jobs(conn, f"j.id IN ({','.join('?' * len(ids))})", tuple(ids), "j.score DESC") if ids else []
        return render(request, "list.html", title=f"Jobs asking for {key}", jobs=jobs)

    @app.get("/data", response_class=HTMLResponse)
    def data(request: Request, table: str = "jobs", q: str = "", page: int = 1) -> HTMLResponse:
        with connection() as conn:
            try:
                contents = inspect.table_page(conn, table, q.strip(), max(1, page))
            except KeyError:
                raise HTTPException(404, f"no table named {table!r}") from None
            counts = inspect.table_counts(conn)
        return render(request, "data.html", title="Data", table=table, q=q, page=max(1, page),
                      table_counts=counts, **contents)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: int) -> HTMLResponse:
        with connection() as conn:
            detail = inspect.job_detail(conn, job_id)
        if detail is None:
            raise HTTPException(404, f"no job #{job_id}")
        return render(request, "job.html", title=f"Job #{job_id}", **detail)

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request) -> HTMLResponse:
        with connection() as conn:
            snapshot = inspect.activity_snapshot(conn, cfg.data_dir / "jobhunt.log")
        return render(request, "activity.html", title="Activity", **snapshot)

    @app.get("/activity/live", response_class=HTMLResponse)
    def activity_live(request: Request) -> HTMLResponse:
        """The Activity page's body alone, which the open page swaps in every few seconds."""
        with connection() as conn:
            snapshot = inspect.activity_snapshot(conn, cfg.data_dir / "jobhunt.log")
        return templates.TemplateResponse(request, "_activity.html", snapshot)

    @app.post("/backup")
    def backup_now() -> RedirectResponse:
        backup(cfg)  # records its own time and result, which Health shows
        return RedirectResponse("/health", status_code=303)

    @app.get("/packets/{job_id}", response_class=HTMLResponse)
    def packet(request: Request, job_id: int) -> HTMLResponse:
        path = cfg.packets_dir / f"{job_id}.md"
        if not path.exists():
            raise HTTPException(404, "no packet for this job yet")
        return render(request, "packet.html", title=f"Packet {job_id}", text=path.read_text(encoding="utf-8"))

    app.mount("/api", create_api(cfg, gazetteer, _tab_counts))
    web_dist = (cfg.root / "web" / "dist").resolve()

    @app.get("/app")
    def vue_root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    @app.get("/app/{path:path}")
    def vue_app(path: str) -> FileResponse:
        """The Vue app built from web/ (npm run build). A path that is not a built file is one of the app's
        pages, so it gets index.html and the app's router shows the page."""
        file = (web_dist / path).resolve()
        if path and file.is_file() and file.is_relative_to(web_dist):
            return FileResponse(file)
        index = web_dist / "index.html"
        if not index.is_file():
            raise HTTPException(404, "the Vue app is not built yet: npm run build in web/")
        return FileResponse(index)

    return app


def _tab_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many records each tab in the top bar holds."""
    since = db.get_setting(conn, "last_list_visit", "")

    def one(sql: str, *params: Any) -> int:
        return conn.execute(sql, params).fetchone()[0]

    return {
        "New": one(f"SELECT COUNT(*) FROM jobs j WHERE {_LISTED} AND j.first_seen_at > ?", since),
        "All open": one(f"SELECT COUNT(*) FROM jobs j WHERE {_LISTED}"),
        "Japanese": one(f"SELECT COUNT(*) FROM jobs j WHERE {_JAPANESE}"),
        "Removed": one("SELECT COUNT(*) FROM jobs WHERE stage IN ('code_removed', 'removed')"),
        "Applied": one(f"SELECT COUNT(*) FROM jobs j WHERE {_APPLIED}"),
        "Not interested": one("SELECT COUNT(*) FROM jobs WHERE status = 'not_interested'"),
        "Skills": inspect.distinct_skill_count(conn),
    }


def _jobs(conn: sqlite3.Connection, where: str, params: tuple, order: str) -> list[dict[str, Any]]:
    """Job rows for display, with facts, badges, other sightings and probable duplicates attached."""
    rows = [dict(r) for r in conn.execute(f"{_JOB_COLUMNS} WHERE {where} ORDER BY {order}", params)]
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    others: dict[int, list[dict[str, str]]] = defaultdict(list)
    for s in conn.execute(
        f"SELECT s.job_id, s.url, src.name FROM sightings s JOIN sources src ON src.id = s.source_id "
        f"JOIN jobs j ON j.id = s.job_id WHERE s.job_id IN ({marks}) AND s.id != j.primary_sighting_id",
        ids,
    ):
        others[s["job_id"]].append({"url": s["url"], "name": s["name"]})
    probably: dict[int, list[int]] = defaultdict(list)
    for p in conn.execute(
        f"SELECT job_a, job_b FROM dupe_pairs WHERE verdict = 'probably_same' AND job_b IN ({marks})", ids
    ):
        probably[p["job_b"]].append(p["job_a"])
    for row in rows:
        row["facts"] = db.loads(row["facts"], {}) or {}
        row["badges"] = db.loads(row["badges"], [])
        row["others"] = others[row["id"]]
        row["probably_same"] = probably[row["id"]]
    return rows


def _health(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
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
        sources.append(dict(src) | {"tally": tally, "average": average, "flags": flags})

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

    tiers = {r["tier"]: r["n"] for r in conn.execute("SELECT tier, COUNT(*) AS n FROM companies GROUP BY tier")}
    return {
        "sources": sources,
        "last_run": runs[0] if runs else None,
        "seconds_per_job": seconds,
        "queue": queue,
        "nights": nights,
        "model_flags": model_flags,
        "tiers": tiers,
        "backup_at": db.get_setting(conn, "last_backup_at"),
        "backup_status": db.get_setting(conn, "last_backup_status"),
        "backup_dir": cfg.backup_dir,
    }
