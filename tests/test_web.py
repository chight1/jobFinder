"""The web server hands everything outside /api to the Vue app built from web/."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from jobhunt.web.app import create_app


@pytest.fixture
def site(cfg, conn, gaz, tmp_path) -> TestClient:
    """The server over a stand-in build: web/dist with index.html and one asset."""
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=app></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("run()", encoding="utf-8")
    (tmp_path / "web" / "secret.txt").write_text("private", encoding="utf-8")
    return TestClient(create_app(dataclasses.replace(cfg, root=tmp_path), gaz))


@pytest.mark.parametrize("path", ["/", "/new", "/jobs/1", "/skills/go", "/data"])
def test_every_page_gets_the_app(site, path):
    assert site.get(path).text == "<div id=app></div>"  # its router shows the page


def test_built_files_are_served(site):
    assert site.get("/assets/app.js").text == "run()"


def test_nothing_outside_the_build_is_served(site):
    assert "private" not in site.get("/..%2fsecret.txt").text


def test_api_paths_never_get_the_app(site):
    assert site.get("/api/nowhere").status_code == 404


def test_an_unbuilt_app_says_so(cfg, gaz, tmp_path):
    site = TestClient(create_app(dataclasses.replace(cfg, root=tmp_path), gaz))
    response = site.get("/new")
    assert response.status_code == 404 and "npm run build" in response.text
