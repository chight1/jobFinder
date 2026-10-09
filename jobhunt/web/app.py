"""The web server: the JSON API (api.py) and the Vue app built from web/, plus the pages not yet moved
to the Vue app, still server-rendered from Jinja2 templates."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db
from jobhunt.backup import backup
from jobhunt.config import Config
from jobhunt.rules import Gazetteer
from jobhunt.web import inspect
from jobhunt.web.api import create_api, tab_counts

_HERE = Path(__file__).parent


def create_app(cfg: Config, gazetteer_: Gazetteer | None = None) -> FastAPI:
    """gazetteer_ is for tests; the app otherwise loads GeoNames on first need."""
    app = FastAPI(title="jobhunt", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=_HERE / "templates")

    def asset(name: str) -> str:
        """A static file's URL with its modification time, so browsers fetch it again after any change."""
        return f"/static/{name}?v={int((_HERE / 'static' / name).stat().st_mtime)}"

    templates.env.globals.update(asset=asset)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    gaz: list[Gazetteer] = [gazetteer_] if gazetteer_ else []  # else loaded on first restore or rescore

    def gazetteer() -> Gazetteer:
        if not gaz:
            gaz.append(Gazetteer.from_geonames(cfg.geonames_dir))
        return gaz[0]

    @contextmanager
    def connection() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        with connection() as conn:
            counts = tab_counts(conn)
        return templates.TemplateResponse(
            request, name, {"view": name.removesuffix(".html"), "counts": counts, **context}
        )

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/app/new", status_code=303)

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> HTMLResponse:
        with connection() as conn:
            context = _health(conn, cfg)
        return render(request, "health.html", title="Health", **context)

    @app.post("/sources/{source_id}/enable")
    def enable_source(source_id: str) -> RedirectResponse:
        with connection() as conn:
            conn.execute(
                "UPDATE sources SET fail_streak = 0, backoff_until = NULL WHERE id = ?", (source_id,)
            )
        return RedirectResponse("/health", status_code=303)

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

    app.mount("/api", create_api(cfg, gazetteer))
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
