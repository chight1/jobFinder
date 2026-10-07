"""Workable accounts: the widget API returns every job with its description."""

from __future__ import annotations

from typing import Any

from jobhunt.collectors import BoardGone, Context, Listing, Summary, join_place

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    slug = ctx.board["slug"]
    page = ctx.fetcher.get(ctx.source_id, API.format(slug=slug), kind="api")
    if page.status in (404, 410):
        raise BoardGone(slug)
    data = page.json()
    company = data.get("name") or ctx.config.get("name") or slug
    summaries = [_summary(job, company) for job in data.get("jobs", [])]
    return Listing(summaries, {s.external_id for s in summaries}, whole_list=True)


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    return summary  # the list is complete


def _summary(job: dict[str, Any], company: str) -> Summary:
    places = [
        join_place(loc.get("city"), loc.get("region"), loc.get("country"))
        for loc in job.get("locations") or []
        if not loc.get("hidden")
    ] or [join_place(job.get("city"), job.get("state"), job.get("country"))]
    if job.get("telecommuting"):
        places.append("Remote")
    return Summary(
        external_id=job["shortcode"],
        url=job.get("url") or job.get("shortlink") or job["application_url"],
        title=job["title"],
        company=company,
        location="; ".join(p for p in places if p),
        posted_at=job.get("published_on") or job.get("created_at"),
        description=job.get("description"),
        fields={"employment_type": job.get("employment_type"), "department": job.get("department")},
    )
