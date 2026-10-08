"""The list page: server-rendered views over the database, plain form posts, no JavaScript."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db, rules, score
from jobhunt.collectors import PostingGone
from jobhunt.collectors.runner import fill_detail
from jobhunt.config import Config
from jobhunt.fetch import Fetcher, FetchError
from jobhunt.rules import Gazetteer

log = logging.getLogger(__name__)

STATUSES = ("apply", "applied", "interviewing", "rejected", "ghosted", "not_interested")
# How each status reads on the page, and the button that sets it. "apply" only drafts a packet,
# so its button says so, while the job itself reads as one still to apply to.
STATUS_NAMES = {"apply": "to apply", "not_interested": "not interested"}
BUTTON_LABELS = {"apply": "draft packet", "not_interested": "not interested"}
_HERE = Path(__file__).parent
_LISTED = "j.stage IN ('kept', 'extract_failed') AND j.closed_at IS NULL AND j.status = 'none'"
_JOB_COLUMNS = """
    SELECT j.*, s.title, s.company_raw, s.location_raw, s.url, src.name AS source_name,
           EXISTS (SELECT 1 FROM packets p WHERE p.job_id = j.id) AS has_packet
    FROM jobs j
    JOIN sightings s ON s.id = j.primary_sighting_id
    JOIN sources src ON src.id = s.source_id
"""


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="jobhunt", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=_HERE / "templates")

    def pay(facts: dict[str, Any]) -> str:
        usd = score.annual_usd(facts.get("pay_min"), facts.get("pay_max"), facts.get("pay_currency"),
                               facts.get("pay_period"), cfg.currency)
        return score.pay_text(facts, usd)

    templates.env.globals.update(statuses=STATUSES, status_names=STATUS_NAMES, button_labels=BUTTON_LABELS, pay=pay)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    gaz: list[Gazetteer] = []  # loaded on first restore, which needs the rules

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"view": name.removesuffix(".html"), **context})

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
            jobs = _jobs(conn, "j.status != 'none'", (), "j.status_at DESC")
        return render(request, "applied.html", title="Applied", jobs=jobs, today=date.today().isoformat())

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> HTMLResponse:
        with connection() as conn:
            context = _health(conn, cfg)
        return render(request, "health.html", title="Health", **context)

    @app.post("/jobs/{job_id}/status")
    def set_status(request: Request, job_id: int, status: str = Form(...)) -> RedirectResponse:
        if status not in STATUSES:
            raise HTTPException(400, f"unknown status {status!r}")
        follow_up = None
        if status == "applied":
            follow_up = (date.today() + timedelta(days=int(cfg.list["follow_up_days"]))).isoformat()
        with connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, status_at = ?, follow_up_on = COALESCE(?, follow_up_on) WHERE id = ?",
                (status, db.utc_now(), follow_up, job_id),
            )
        return RedirectResponse(request.headers.get("referer") or "/new", status_code=303)

    def gazetteer() -> Gazetteer:
        if not gaz:
            gaz.append(Gazetteer.from_geonames(cfg.geonames_dir))
        return gaz[0]

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

    @app.get("/packets/{job_id}", response_class=HTMLResponse)
    def packet(request: Request, job_id: int) -> HTMLResponse:
        path = cfg.packets_dir / f"{job_id}.md"
        if not path.exists():
            raise HTTPException(404, "no packet for this job yet")
        return render(request, "packet.html", title=f"Packet {job_id}", text=path.read_text(encoding="utf-8"))

    return app


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
    for src in conn.execute("SELECT * FROM sources WHERE collector NOT IN ('manual') ORDER BY id"):
        tally = last.get("sources", {}).get(src["id"], {})
        nightly_new = [r["counts"].get("sources", {}).get(src["id"], {}).get("new_jobs", 0) for r in runs]
        average = sum(nightly_new) / len(nightly_new) if nightly_new else 0.0
        flags = []
        if src["fail_streak"] >= disable_after:
            flags.append("disabled")
        elif src["fail_streak"] > 0:
            flags.append("blocked")
        if tally.get("requests", 0) > 0 and tally.get("parsed", 0) == 0 and average >= 5:
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
    }
