"""Lever boards: one call returns every posting with its full description."""

from __future__ import annotations

from typing import Any

from jobhunt.collectors import BoardGone, Context, Listing, Summary, iso_from_ms

API = "https://api.lever.co/v0/postings/{slug}?mode=json"
_INTERVALS = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    slug = ctx.board["slug"]
    page = ctx.fetcher.get(ctx.source_id, API.format(slug=slug), kind="api")
    if page.status in (404, 410):
        raise BoardGone(slug)
    company = ctx.config.get("name") or slug
    summaries = [_summary(posting, company) for posting in page.json()]
    return Listing(summaries, {s.external_id for s in summaries})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    return summary  # the list is complete


def _summary(posting: dict[str, Any], company: str) -> Summary:
    categories = posting.get("categories") or {}
    location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
    if posting.get("workplaceType") == "remote" and "remote" not in location.lower():
        location = f"{location}; Remote" if location else "Remote"
    fields: dict[str, Any] = {
        "employment_type": categories.get("commitment"),
        "workplace_type": posting.get("workplaceType"),
        "team": categories.get("team"),
    }
    salary = posting.get("salaryRange") or {}
    if salary:
        fields |= {
            "pay_min": salary.get("min"),
            "pay_max": salary.get("max"),
            "pay_currency": salary.get("currency"),
            "pay_period": _INTERVALS.get(salary.get("interval", ""), "year"),
        }
    return Summary(
        external_id=posting["id"],
        url=posting["hostedUrl"],
        title=posting["text"],
        company=company,
        location=location,
        posted_at=iso_from_ms(posting.get("createdAt")),
        description=_description(posting),
        fields=fields,
    )


def _description(posting: dict[str, Any]) -> str:
    parts = [posting.get("description") or ""]
    for block in posting.get("lists") or []:
        parts.append(f"<h3>{block.get('text', '')}</h3><ul>{block.get('content', '')}</ul>")
    parts.append(posting.get("additional") or "")
    return "\n".join(p for p in parts if p)
