"""Public job-board JSON APIs. Source config "api" picks the parser; "url" and "params" the request.
"params" may be a list of param sets: each is its own search, and the jobs are merged by id.
"body", when given, makes the request a POST of that JSON with the params and page merged in
(Algolia searches).
"categories", when given, keeps only jobs whose category (fields["category"]) is listed, for APIs
that cannot filter by category themselves.

The lists are newest first. Paged APIs stop at the first page where every id is known.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from jobhunt.collectors import Context, Listing, Summary, iso_from_s, join_place
from jobhunt.collectors.pages import parse_job_page

DEFAULT_MAX_PAGES = 10

_PERIODS = {"annual": "year", "yearly": "year", "year": "year", "monthly": "month", "month": "month",
            "hourly": "hour", "hour": "hour"}


def _pay(low: Any, high: Any, currency: Any, period: Any = "year") -> dict[str, Any]:
    low = low if isinstance(low, (int, float)) and low > 0 else None
    high = high if isinstance(high, (int, float)) and high > 0 else None
    if low is None and high is None:
        return {}
    return {"pay_min": low, "pay_max": high, "pay_currency": currency or "USD",
            "pay_period": _PERIODS.get(str(period or "year").lower(), "year")}


# --- one parser per API: response JSON -> summaries ----------------------------------------


def _himalayas(data: Any) -> list[Summary]:
    out = []
    for job in data.get("jobs", []):
        guid = job.get("guid") or job.get("applicationLink")
        out.append(Summary(
            external_id=guid,
            url=guid if str(guid).startswith("http") else job.get("applicationLink"),
            title=job.get("title", ""),
            company=job.get("companyName", ""),
            location="; ".join(job.get("locationRestrictions") or []) or "Worldwide",
            posted_at=iso_from_s(job.get("pubDate")),
            description=job.get("description"),
            fields={"employment_type": job.get("employmentType"),
                    **_pay(job.get("minSalary"), job.get("maxSalary"), job.get("currency"), job.get("salaryPeriod"))},
        ))
    return out


def _jobicy(data: Any) -> list[Summary]:
    return [
        Summary(
            external_id=str(job["id"]),
            url=job["url"],
            title=job.get("jobTitle", ""),
            company=job.get("companyName", ""),
            location=job.get("jobGeo", ""),
            posted_at=job.get("pubDate"),
            description=job.get("jobDescription"),
            fields={"employment_type": ", ".join(job.get("jobType") or []),
                    **_pay(job.get("salaryMin"), job.get("salaryMax"), job.get("salaryCurrency"), job.get("salaryPeriod"))},
        )
        for job in data.get("jobs", [])
    ]


def _remotive(data: Any) -> list[Summary]:
    return [
        Summary(
            external_id=str(job["id"]),
            url=job["url"],
            title=job.get("title", ""),
            company=job.get("company_name", ""),
            location=job.get("candidate_required_location", ""),
            posted_at=job.get("publication_date"),
            description=job.get("description"),
            fields={"employment_type": job.get("job_type"), "salary_text": job.get("salary")},
        )
        for job in data.get("jobs", [])
    ]


def _remoteok(data: Any) -> list[Summary]:
    jobs = [item for item in data if isinstance(item, dict) and "id" in item]  # item 0 is the legal notice
    return [
        Summary(
            external_id=str(job["id"]),
            url=job.get("url") or f"https://remoteok.com/remote-jobs/{job['id']}",
            title=job.get("position", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            posted_at=iso_from_s(job.get("epoch")) or job.get("date"),
            description=job.get("description"),
            fields=_pay(job.get("salary_min"), job.get("salary_max"), "USD"),
        )
        for job in jobs
    ]


def _working_nomads(data: Any) -> list[Summary]:
    return [
        Summary(
            external_id=job["url"],  # the API has no id field
            url=job["url"],
            title=job.get("title", ""),
            company=job.get("company_name", ""),
            location=job.get("location", ""),
            posted_at=job.get("pub_date"),
            description=job.get("description"),
            fields={"category": job.get("category_name")},
        )
        for job in data
    ]


def _amazon(data: Any) -> list[Summary]:
    out = []
    for job in data.get("jobs", []):
        description = "\n".join(
            f"<h3>{heading}</h3><div>{job[key]}</div>"
            for heading, key in (("Description", "description"), ("Basic qualifications", "basic_qualifications"),
                                 ("Preferred qualifications", "preferred_qualifications"))
            if job.get(key)
        )
        out.append(Summary(
            external_id=str(job.get("id_icims") or job["id"]),
            url=f"https://www.amazon.jobs{job['job_path']}",
            title=job.get("title", ""),
            company=job.get("company_name") or "Amazon",
            location=job.get("normalized_location") or job.get("location", ""),
            posted_at=_amazon_date(job.get("posted_date")),
            description=description,
            fields={"employment_type": job.get("job_schedule_type")},
        ))
    return out


def _techinasia(data: Any) -> list[Summary]:
    """Tech in Asia's Algolia index. The company is a JSON string inside the hit."""
    out = []
    for job in data.get("hits", []):
        company = job.get("company") or {}
        if isinstance(company, str):
            company = json.loads(company)
        city = job.get("city") or {}
        remote = "Remote" if job.get("work_arrangement") == "remote" else ""
        published = job.get("published_at")
        out.append(Summary(
            external_id=job["id"],
            url=f"https://www.techinasia.com/jobs/{job['id']}",
            title=job.get("title", ""),
            company=company.get("name", ""),
            location="; ".join(p for p in (join_place(city.get("name"), city.get("country_name")), remote) if p),
            posted_at=published.replace(" ", "T") + "+00:00" if published else None,
            description=job.get("description"),
            fields={"employment_type": (job.get("job_type") or {}).get("name"),
                    "category": (job.get("position") or {}).get("name")},
        ))
    return out


def _amazon_date(text: str | None) -> str | None:
    """'October  7, 2026' to '2026-10-07'."""
    if not text:
        return None
    try:
        return datetime.strptime(" ".join(text.split()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


# api name -> (parser, how to ask for page n or None when the API has no paging)
_APIS: dict[str, tuple[Callable[[Any], list[Summary]], Callable[[int], dict[str, Any]] | None]] = {
    "himalayas": (_himalayas, lambda n: {"page": n}),
    "jobicy": (_jobicy, None),
    "remotive": (_remotive, None),
    "remoteok": (_remoteok, None),
    "workingnomads": (_working_nomads, None),
    "amazon": (_amazon, lambda n: {"offset": (n - 1) * 100, "result_limit": 100}),
    "techinasia": (_techinasia, lambda n: {"page": n - 1}),
}


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    params = ctx.config.get("params") or {}
    found: dict[str, Summary] = {}
    for one in params if isinstance(params, list) else [params]:
        found = _search(ctx, one) | found  # a job in more than one search keeps its first copy
    return Listing(list(found.values()), set(found))


def _search(ctx: Context, base_params: dict[str, Any]) -> dict[str, Summary]:
    parse, paging = _APIS[ctx.config["api"]]
    max_pages = int(ctx.config.get("max_pages", DEFAULT_MAX_PAGES)) if paging else 1
    categories = set(ctx.config.get("categories") or ())
    found: dict[str, Summary] = {}
    for number in range(1, max_pages + 1):
        params = base_params | (paging(number) if paging else {})
        if "body" in ctx.config:
            page = ctx.fetcher.post_json(ctx.source_id, ctx.config["url"], ctx.config["body"] | params)
        else:
            page = ctx.fetcher.get(ctx.source_id, ctx.config["url"], kind="api", params=params or None)
        summaries = [
            s for s in parse(page.json())
            if s.external_id and s.url and (not categories or s.fields.get("category") in categories)
        ]
        fresh = {s.external_id: s for s in summaries if s.external_id not in found}
        found |= fresh
        if not fresh or not (set(fresh) - ctx.known(fresh)) or ctx.out_of_time():
            break
    return found


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    page = ctx.fetcher.get(ctx.source_id, summary.url)
    return parse_job_page(page.text, summary, ctx.config.get("selectors") or {})
