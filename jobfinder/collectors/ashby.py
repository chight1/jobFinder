"""Ashby boards: one call returns every posting with description and pay."""

from __future__ import annotations

from typing import Any

from jobfinder.collectors import BoardGone, Context, Listing, Summary

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
_INTERVALS = {"1 YEAR": "year", "1 MONTH": "month", "1 HOUR": "hour"}


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    slug = ctx.board["slug"]
    page = ctx.fetcher.get(ctx.source_id, API.format(slug=slug), kind="api")
    if page.status in (404, 410):
        raise BoardGone(slug)
    company = ctx.config.get("name") or slug
    summaries = [_summary(job, company) for job in page.json().get("jobs", []) if job.get("isListed", True)]
    return Listing(summaries, {s.external_id for s in summaries})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    return summary  # the list is complete


def _summary(job: dict[str, Any], company: str) -> Summary:
    places = [job.get("location") or ""]
    places += [s.get("location", "") for s in job.get("secondaryLocations") or []]
    if job.get("isRemote") or job.get("workplaceType") == "Remote":
        places.append("Remote")
    fields: dict[str, Any] = {
        "employment_type": job.get("employmentType"),
        "workplace_type": job.get("workplaceType"),
        "department": job.get("department"),
    }
    fields |= _salary(job.get("compensation") or {})
    return Summary(
        external_id=job["id"],
        url=job.get("jobUrl") or job.get("applyUrl"),
        title=job["title"],
        company=company,
        location="; ".join(p for p in places if p),
        posted_at=job.get("publishedAt"),
        description=job.get("descriptionHtml") or job.get("descriptionPlain"),
        fields=fields,
    )


def _salary(compensation: dict[str, Any]) -> dict[str, Any]:
    """The first salary component of the first pay tier."""
    components = list(compensation.get("summaryComponents") or [])
    for tier in compensation.get("compensationTiers") or []:
        components += tier.get("components") or []
    for component in components:
        if component.get("compensationType") == "Salary":
            return {
                "pay_min": component.get("minValue"),
                "pay_max": component.get("maxValue"),
                "pay_currency": component.get("currencyCode"),
                "pay_period": _INTERVALS.get(component.get("interval", ""), "year"),
            }
    return {}
