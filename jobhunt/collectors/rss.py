"""RSS and Atom feeds, fetched with conditional requests so an unchanged feed costs one tiny reply.

Source config:
    feeds           feed URLs
    title_format    "company: title" when entry titles carry the company before a colon,
                    "title at company" when they carry it after a last " at "
    company_field   entry key holding the company (default "author")
    location_field  entry key holding the location (default "location", then "region")
    location_in_text  true when the entry text ends with a "Place · pay" line (startup.jobs)
"""

from __future__ import annotations

import time
from typing import Any

import feedparser

from jobhunt.collectors import Context, Listing, PostingGone, Summary
from jobhunt.collectors.pages import parse_job_page
from jobhunt.text import to_text

SHORT_TEXT_CHARS = 500  # a feed entry shorter than this gets a detail fetch


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    validators: dict[str, dict[str, str]] = dict((cursor or {}).get("feeds", {}))
    summaries: list[Summary] = []
    for feed_url in ctx.config["feeds"]:
        headers = {}
        saved = validators.get(feed_url, {})
        if saved.get("etag"):
            headers["If-None-Match"] = saved["etag"]
        if saved.get("last_modified"):
            headers["If-Modified-Since"] = saved["last_modified"]
        page = ctx.fetcher.get(ctx.source_id, feed_url, headers=headers or None)
        if page.status == 304 or not page.ok:
            continue
        validators[feed_url] = {
            key: value
            for key, value in (("etag", page.headers.get("etag")), ("last_modified", page.headers.get("last-modified")))
            if value
        }
        feed = feedparser.parse(page.text)
        summaries += [_summary(entry, ctx.config) for entry in feed.entries if entry.get("link")]
    unique = {s.external_id: s for s in summaries}
    return Listing(list(unique.values()), set(unique), cursor={"feeds": validators})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    page = ctx.fetcher.get(ctx.source_id, summary.url)
    if page.status in (404, 410):
        raise PostingGone(summary.url)
    return parse_job_page(page.text, summary, ctx.config.get("selectors") or {})


def _summary(entry: Any, config: dict[str, Any]) -> Summary:
    title = entry.get("title", "").strip()
    company = entry.get(config.get("company_field", "author"), "")
    if config.get("title_format") == "company: title" and ":" in title:
        company, title = (part.strip() for part in title.split(":", 1))
    elif config.get("title_format") == "title at company" and " at " in title:
        title, named = (part.strip() for part in title.rsplit(" at ", 1))
        company = company or named

    text = ""
    if entry.get("content"):
        text = entry["content"][0].get("value", "")
    text = text or entry.get("summary", "")

    if config.get("location_field"):
        location = entry.get(config["location_field"], "")
    else:
        location = entry.get("location", "") or entry.get("region", "")
    if config.get("location_in_text") and not location:
        lines = [line for line in to_text(text).splitlines() if line.strip()]
        location = lines[-1].split("·", 1)[0].strip() if len(lines) > 1 else ""  # one line is only the teaser

    published = entry.get("published_parsed") or entry.get("updated_parsed")
    return Summary(
        external_id=entry.get("id") or entry["link"],
        url=entry["link"],
        title=title,
        company=str(company),
        location=str(location),
        posted_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", published) if published else None,
        description=text if len(to_text(text)) >= SHORT_TEXT_CHARS else None,
    )
