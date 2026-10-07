"""Workday career sites: paged POST list, descriptions fetched for new jobs."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunt.collectors import BoardGone, Context, Listing, PostingGone, Summary

PAGE_SIZE = 20
_DAYS_AGO = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _base(ctx: Context) -> str:
    extra = ctx.config
    return f"https://{extra['host']}/wday/cxs/{extra['tenant']}/{extra['site']}"


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    offset = (cursor or {}).get("offset", 0)
    total: int | None = (cursor or {}).get("total")
    summaries: list[Summary] = []
    while True:
        body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        page = ctx.fetcher.post_json(ctx.source_id, f"{_base(ctx)}/jobs", body)
        if page.status in (404, 410):
            raise BoardGone(ctx.board["slug"])
        data = page.json()
        if total is None or offset == 0:
            total = data.get("total", 0)  # later pages may report 0
        postings = data.get("jobPostings") or []
        summaries += [_summary(ctx, p) for p in postings if p.get("externalPath")]
        offset += len(postings)
        if not postings or offset >= total:
            return Listing(summaries, {s.external_id for s in summaries}, whole_list=not cursor)
        if ctx.out_of_time():
            return Listing(summaries, {s.external_id for s in summaries}, cursor={"offset": offset, "total": total})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    page = ctx.fetcher.get(ctx.source_id, f"{_base(ctx)}{summary.external_id}", kind="api")
    if page.status in (404, 410):
        raise PostingGone(summary.url)
    data = page.json()
    info = data.get("jobPostingInfo") or {}
    company = (data.get("hiringOrganization") or {}).get("name") or summary.company
    fields = summary.fields | {"employment_type": info.get("timeType")}
    return replace(
        summary,
        url=info.get("externalUrl") or summary.url,
        company=company,
        location=info.get("location") or summary.location,
        description=info.get("jobDescription") or "",
        fields=fields,
    )


def _summary(ctx: Context, posting: dict[str, Any]) -> Summary:
    extra = ctx.config
    path = posting["externalPath"]
    return Summary(
        external_id=path,
        url=f"https://{extra['host']}/{extra['site']}{path}",
        title=posting.get("title", ""),
        company=extra.get("name") or extra["tenant"],
        location=posting.get("locationsText", ""),
        posted_at=_posted_on(posting.get("postedOn", "")),
    )


def _posted_on(text: str) -> str | None:
    """'Posted Today', 'Posted Yesterday', 'Posted 3 Days Ago', 'Posted 30+ Days Ago' to a date."""
    today = datetime.now(UTC).date()
    lowered = text.lower()
    if "today" in lowered:
        return today.isoformat()
    if "yesterday" in lowered:
        return (today - timedelta(days=1)).isoformat()
    match = _DAYS_AGO.search(text)
    return (today - timedelta(days=int(match.group(1)))).isoformat() if match else None
