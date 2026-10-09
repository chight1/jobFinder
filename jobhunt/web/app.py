"""The web server: the JSON API (api.py) and the Vue app built from web/, plus the pages not yet moved
to the Vue app, still server-rendered from Jinja2 templates."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from jobhunt import db
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
