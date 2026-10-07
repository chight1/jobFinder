"""SmartRecruiters companies: paged list, descriptions fetched for new jobs."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from jobhunt.collectors import BoardGone, Context, Listing, PostingGone, Summary, join_place

API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
PAGE_SIZE = 100
_DESCRIPTION_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    company_id = ctx.board["slug"]
    offset = (cursor or {}).get("offset", 0)
    summaries: list[Summary] = []
    while True:
        params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": offset}
        if ctx.config.get("country"):
            params["country"] = ctx.config["country"]
        page = ctx.fetcher.get(ctx.source_id, API.format(company=company_id), kind="api", params=params)
        if page.status in (404, 410):
            raise BoardGone(company_id)
        data = page.json()
        content = data.get("content") or []
        summaries += [_summary(item, company_id) for item in content]
        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            # Started from the top and reached the end: the list is whole.
            whole = not cursor
            return Listing(summaries, {s.external_id for s in summaries}, whole_list=whole)
        if ctx.out_of_time():
            return Listing(summaries, {s.external_id for s in summaries}, cursor={"offset": offset})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    url = f"{API.format(company=ctx.board['slug'])}/{summary.external_id}"
    page = ctx.fetcher.get(ctx.source_id, url, kind="api")
    if page.status in (404, 410):
        raise PostingGone(url)
    sections = (page.json().get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in _DESCRIPTION_SECTIONS:
        section = sections.get(key) or {}
        if section.get("text"):
            parts.append(f"<h3>{section.get('title', '')}</h3>{section['text']}")
    return replace(summary, description="\n".join(parts))


def _summary(item: dict[str, Any], company_id: str) -> Summary:
    loc = item.get("location") or {}
    location = loc.get("fullLocation") or join_place(loc.get("city"), loc.get("region"), loc.get("country"))
    if loc.get("remote"):
        location = f"{location}; Remote" if location else "Remote"
    return Summary(
        external_id=item["id"],
        url=f"https://jobs.smartrecruiters.com/{company_id}/{item['id']}",
        title=item["name"],
        company=(item.get("company") or {}).get("name") or company_id,
        location=location,
        posted_at=item.get("releasedDate"),
        fields={"employment_type": (item.get("typeOfEmployment") or {}).get("label")},
    )
