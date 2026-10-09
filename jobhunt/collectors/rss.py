"""RSS and Atom feeds, fetched with conditional requests so an unchanged feed costs one tiny reply.

Source config:
    feeds           feed URLs
    title_format    "company: title" when entry titles carry the company before a colon,
                    "title at company" when they carry it after a last " at ",
                    "title, company" when they carry it after a last comma (Python.org)
    title_prefix    text every entry title starts with, cut off ("Remote " on RemoteYeah)
    company_field   entry key holding the company (default "author")
    company_place_split  text that splits the company field into company, then place
                    (Jobspresso: "Hopper<br>⚲&nbsp;Canada")
    location_field  entry key holding the location (default "location", then "region")
    location_in_text  true when the entry text ends with a "Place · pay" line (startup.jobs)
    location_first_line  true when the entry text starts with the place, as "City, Region, Country" (Python.org)
    fetch_job_page  true when entries lack something only the job page has (Remote First Jobs: the
                    location): every new entry that passes the title rules gets its job page
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

import feedparser

from jobhunt.collectors import Context, Listing, PostingGone, Summary
from jobhunt.collectors.pages import parse_job_page
from jobhunt.dedupe import LEGAL_SUFFIXES
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
    prefix = config.get("title_prefix", "")
    if prefix and title.startswith(prefix):
        title = title[len(prefix):]
    company = entry.get(config.get("company_field", "author"), "")
    place = ""
    split = config.get("company_place_split")
    if split and split in company:
        company, place = (html.unescape(part).strip() for part in company.split(split, 1))
        place = re.sub(r"^\W+", "", place)  # a pin symbol before the place
    if config.get("title_format") == "company: title" and ":" in title:
        company, title = (part.strip() for part in title.split(":", 1))
    elif config.get("title_format") == "title at company" and " at " in title:
        title, named = (part.strip() for part in title.rsplit(" at ", 1))
        company = company or named
    elif config.get("title_format") == "title, company" and ", " in title:
        title, named = title.rsplit(", ", 1)
        if named.strip(" .").lower() in LEGAL_SUFFIXES and ", " in title:  # "ActivePrime, Inc."
            title, first = title.rsplit(", ", 1)
            named = f"{first}, {named}"
        title, company = title.strip(), company or named.strip()

    text = ""
    if entry.get("content"):
        text = entry["content"][0].get("value", "")
    text = text or entry.get("summary", "")

    if config.get("location_field"):
        location = entry.get(config["location_field"], "")
    else:
        location = entry.get("location", "") or entry.get("region", "") or place
    if config.get("location_first_line") and not location:
        first = next((line for line in to_text(text).splitlines() if line.strip()), "")
        parts = [part.strip() for part in first.split(",") if part.strip()]
        location = ", ".join(dict.fromkeys(parts))  # the place fields often repeat: "Remote, Remote, Worldwide"
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
        description=text if len(to_text(text)) >= SHORT_TEXT_CHARS and not config.get("fetch_job_page") else None,
    )
