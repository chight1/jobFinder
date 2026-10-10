"""Plain HTML sites: job boards, company career pages, HRMOS and Talentio.

A list page gives links that match the source's job_link pattern; the job page gives
the details, from its schema.org JobPosting data when it has one.

Source config:
    list_urls   list pages (or XML sitemaps) to start from
    job_link    regex matched against absolute links; group 1 is the job's stable id
    page_param  query parameter for page 2, 3, ... (omit for a single page)
    page_url    or a template for page 2, 3, ... such as "https://example.com/jobs/{page}"
    max_pages   page limit per list URL (default 10)
    whole_list  true when the list pages show every open job in no set order (company pages,
                HRMOS): every page is walked, not just up to the first page of known jobs
    selectors   CSS selectors for title, company, location, description when a job
                page has no JobPosting data
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from jobhunt.collectors import BoardGone, Context, Listing, PostingGone, Summary, join_place
from jobhunt.rules import JAPAN_PREFECTURES
from jobhunt.text import tidy

DEFAULT_MAX_PAGES = 10


def board_config(ats: str, slug: str, extra: dict[str, Any]) -> dict[str, Any]:
    """List settings for a company's HRMOS or Talentio board."""
    if ats == "hrmos":
        return {
            "list_urls": [f"https://hrmos.co/pages/{slug}/jobs"],
            "job_link": rf"^https://hrmos\.co/pages/{re.escape(slug)}/jobs/([0-9A-Za-z]+)$",
            "page_param": "page",
            "max_pages": 20,
            "whole_list": True,
        } | extra
    if ats == "talentio":
        return {
            "job_link": rf"^https://open\.talentio\.com/r/1/c/{re.escape(slug)}/pages/(\d+)",
            "whole_list": True,
        } | extra  # extra carries list_urls: the company's Talentio home pages
    raise ValueError(f"no page settings for {ats}")


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    config = ctx.config
    if ctx.board:
        config = board_config(ctx.board["ats"], ctx.board["slug"], config)

    def load(url: str) -> tuple[int, str, str]:
        page = ctx.fetcher.get(ctx.source_id, url)
        return page.status, page.text, page.url

    return list_from_pages(ctx, config, load)


def list_from_pages(ctx: Context, config: dict[str, Any], load: Callable[[str], tuple[int, str, str]]) -> Listing:
    """Walk the list pages and collect job links. load(url) gives (status, html, final url), so the
    browser collector shares this loop and differs only in how a page is loaded."""
    pattern = re.compile(config["job_link"])
    whole = bool(config.get("whole_list"))
    paged = config.get("page_param") or config.get("page_url")
    max_pages = int(config.get("max_pages", DEFAULT_MAX_PAGES)) if paged else 1

    found: dict[str, Summary] = {}
    for list_url in config["list_urls"]:
        for number in range(1, max_pages + 1):
            if ctx.out_of_time():
                break
            status, text, final_url = load(page_url(list_url, number, config))
            if not 200 <= status < 300:
                if ctx.board and number == 1 and status in (404, 410):
                    raise BoardGone(ctx.board["slug"])
                break
            links: dict[str, Summary] = {}
            for external_id, summary in job_links(text, final_url, pattern):
                # A card often links twice (logo, then title); keep the link that has text.
                if external_id not in links or links[external_id].title == links[external_id].url:
                    links[external_id] = summary
            fresh = {i: s for i, s in links.items() if i not in found}
            if not fresh:
                break  # past the last page
            found |= fresh
            # Newest-first lists stop at the first page where every id is already known.
            if not whole and not (set(fresh) - ctx.known(fresh)):
                break
    return Listing(list(found.values()), set(found))


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    page = ctx.fetcher.get(ctx.source_id, summary.url)
    if page.status in (404, 410):
        raise PostingGone(summary.url)
    if ctx.board and ctx.board["ats"] == "talentio":
        return parse_talentio_page(page.text, summary, ctx.config.get("name", ""))
    return parse_job_page(page.text, summary, ctx.config.get("selectors") or {})


def parse_talentio_page(html: str, summary: Summary, company: str) -> Summary:
    """Talentio pages carry the job as JSON in a React component's data-react-props attribute."""
    node = LexborHTMLParser(html).css_first('div[data-react-class^="RecruitmentOpenPageView"]')
    if node is None:
        return parse_job_page(html, summary, {})
    job = json.loads(node.attributes.get("data-react-props") or "{}").get("recruitmentOpenPage") or {}
    details = (job.get("requisitionDetails") or []) + (job.get("jobDescriptionDetails") or [])
    description = "\n".join(
        f"<h3>{d.get('name', '')}</h3><div>{d.get('value', '')}</div>" for d in details if d.get("value")
    )
    return replace(
        summary,
        title=job.get("name") or summary.title,
        company=company or summary.company,
        url=job.get("publishedUrl") or summary.url,
        description=description,
        from_link=False,
    )


def parse_job_page(html: str, summary: Summary, selectors: dict[str, str]) -> Summary:
    """Fill a summary from a job page: JobPosting data first, then the given selectors."""
    tree = LexborHTMLParser(html)
    posting = find_job_posting(tree)
    if posting:
        return apply_job_posting(summary, posting)

    def pick(name: str, default: str = "") -> str:
        selector = selectors.get(name)
        node = tree.css_first(selector) if selector else None
        return tidy(node.text(separator=" ")) if node else default

    heading = tree.css_first("h1")
    title = pick("title") or (tidy(heading.text()) if heading else summary.title)
    description_node = tree.css_first(selectors.get("description") or "main") or tree.body
    return replace(
        summary,
        title=title,
        company=pick("company", summary.company),
        location=pick("location", summary.location),
        description=description_node.html if description_node else "",
        from_link=False,
    )


# --- schema.org JobPosting ----------------------------------------------------------------

_PERIODS = {"YEAR": "year", "MONTH": "month", "HOUR": "hour"}


_COMMENT_WRAPPER = re.compile(r"^\s*(?://\s*)?<!--|(?://\s*)?-->\s*$")  # old-browser hiding (GaijinPot)


def find_job_posting(tree: LexborHTMLParser) -> dict[str, Any] | None:
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(_COMMENT_WRAPPER.sub("", script.text() or ""))
        except json.JSONDecodeError:
            continue
        for item in _walk(data):
            kind = item.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return item
    return None


def _walk(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _walk(item)
    elif isinstance(data, dict):
        yield data
        yield from _walk(data.get("@graph", []))


def apply_job_posting(summary: Summary, posting: dict[str, Any]) -> Summary:
    org = posting.get("hiringOrganization") or {}
    fields = dict(summary.fields)
    employment = posting.get("employmentType")
    if employment:
        fields["employment_type"] = ", ".join(employment) if isinstance(employment, list) else employment
    fields |= _salary(posting.get("baseSalary"))
    return replace(
        summary,
        title=posting.get("title") or summary.title,
        company=(org.get("name") if isinstance(org, dict) else str(org)) or summary.company,
        location=_location(posting) or summary.location,
        posted_at=posting.get("datePosted") or summary.posted_at,
        description=posting.get("description") or summary.description or "",
        fields=fields,
        from_link=False,
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _name(value: Any) -> str:
    return value.get("name", "") if isinstance(value, dict) else str(value or "")


_PREFECTURE_CODE = re.compile(r"JP-(\d\d)")
_PREFECTURE_NAMES = list(JAPAN_PREFECTURES)


def _region(value: Any) -> str:
    """A region, with an ISO prefecture code (GaijinPot's "JP-13") turned into its name ("Tokyo")."""
    region = _name(value)
    code = _PREFECTURE_CODE.fullmatch(region.strip())
    if code and 1 <= int(code[1]) <= len(_PREFECTURE_NAMES):
        return _PREFECTURE_NAMES[int(code[1]) - 1]
    return region


def _location(posting: dict[str, Any]) -> str:
    places = []
    for loc in _as_list(posting.get("jobLocation")):
        address = loc.get("address", {}) if isinstance(loc, dict) else {}
        if isinstance(address, str):
            places.append(address)
        else:
            places.append(join_place(address.get("addressLocality"), _region(address.get("addressRegion")),
                                     _name(address.get("addressCountry"))))
    for requirement in _as_list(posting.get("applicantLocationRequirements")):
        places.append(_name(requirement))
    if posting.get("jobLocationType") == "TELECOMMUTE":
        places.append("Remote")
    return "; ".join(p for p in places if p)


def _salary(salary: Any) -> dict[str, Any]:
    if not isinstance(salary, dict):
        return {}
    value = salary.get("value") or {}
    if not isinstance(value, dict):
        value = {"value": value}
    low = value.get("minValue") or value.get("value")
    high = value.get("maxValue") or value.get("value")
    if not low and not high:
        return {}
    return {
        "pay_min": _number(low),
        "pay_max": _number(high),
        "pay_currency": salary.get("currency"),
        "pay_period": _PERIODS.get(str(value.get("unitText", "YEAR")).upper(), "year"),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- list pages ---------------------------------------------------------------------------


_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def job_links(text: str, base_url: str, pattern: re.Pattern[str]) -> Iterator[tuple[str, Summary]]:
    """(id, summary) for every link to a job: <a href>, data-href cards, or sitemap <loc> entries."""
    if "<urlset" in text[:1000]:
        candidates = [(loc, "") for loc in _SITEMAP_LOC.findall(text)]
    else:
        candidates = [
            (node.attributes.get("href") or node.attributes.get("data-href") or "", node.text(separator=" "))
            for node in LexborHTMLParser(text).css("a[href], [data-href]")
        ]
    for href, text_in_link in candidates:
        url = urljoin(base_url, href).split("#")[0]
        match = pattern.search(url)
        if not match:
            continue
        external_id = match.group(1) if match.groups() else url
        title = tidy(text_in_link)[:200] or url
        yield external_id, Summary(external_id=external_id, url=url, title=title, from_link=True)


def page_url(list_url: str, number: int, config: dict[str, Any]) -> str:
    if number == 1:
        return list_url
    if config.get("page_url"):
        return config["page_url"].format(page=number)
    parts = urlsplit(list_url)
    name = config["page_param"]
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != name] + [(name, str(number))]
    return urlunsplit(parts._replace(query=urlencode(query)))
