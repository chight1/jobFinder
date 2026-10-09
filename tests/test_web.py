"""The pages still server-rendered render against a small real database (the client fixture)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/data", "/data?table=sightings&q=Acme", "/data?table=fetch_log"])
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:500]


def test_home_is_the_vue_app(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/app/new"


def test_unknown_table_is_404(client):
    assert client.get("/data?table=sqlite_master").status_code == 404
