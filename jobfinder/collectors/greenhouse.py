"""Greenhouse boards: the whole board in one list call, descriptions fetched for new jobs."""

from __future__ import annotations

import html
from dataclasses import replace

from jobfinder.collectors import BoardGone, Context, Listing, PostingGone, Summary

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    slug = ctx.board["slug"]
    page = ctx.fetcher.get(ctx.source_id, API.format(slug=slug), kind="api")
    if page.status in (404, 410):
        raise BoardGone(slug)
    company = ctx.config.get("name") or slug
    summaries = [
        Summary(
            external_id=str(job["id"]),
            url=job["absolute_url"],
            title=job["title"],
            company=job.get("company_name") or company,
            location=(job.get("location") or {}).get("name", ""),
            posted_at=job.get("first_published") or job.get("updated_at"),
        )
        for job in page.json().get("jobs", [])
    ]
    return Listing(summaries, {s.external_id for s in summaries})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    url = f"{API.format(slug=ctx.board['slug'])}/{summary.external_id}"
    page = ctx.fetcher.get(ctx.source_id, url, kind="api")
    if page.status in (404, 410):
        raise PostingGone(url)
    # Greenhouse returns the description HTML entity-escaped.
    return replace(summary, description=html.unescape(page.json().get("content") or ""))
