"""The web server: the JSON API under /api (api.py) and, everywhere else, the Vue app built from web/."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from jobfinder.config import Config
from jobfinder.rules import Gazetteer
from jobfinder.web.api import create_api


def create_app(cfg: Config, gazetteer_: Gazetteer | None = None) -> FastAPI:
    """gazetteer_ is for tests; the app otherwise loads GeoNames on first need."""
    app = FastAPI(title="jobfinder", docs_url=None, redoc_url=None, openapi_url=None)  # the API has its own docs
    gaz: list[Gazetteer] = [gazetteer_] if gazetteer_ else []

    def gazetteer() -> Gazetteer:
        if not gaz:
            gaz.append(Gazetteer.from_geonames(cfg.geonames_dir))
        return gaz[0]

    app.mount("/api", create_api(cfg, gazetteer))
    dist = (cfg.root / "web" / "dist").resolve()

    @app.get("/{path:path}")
    def vue_app(path: str) -> FileResponse:
        """The Vue app built from web/ (npm run build). A path that is not a built file is one of the app's
        pages, so it gets index.html and the app's router shows the page."""
        file = (dist / path).resolve()
        if path and file.is_file() and file.is_relative_to(dist):
            return FileResponse(file)
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(404, "the Vue app is not built yet: npm run build in web/")
        return FileResponse(index)

    return app
