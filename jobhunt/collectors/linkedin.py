"""LinkedIn's guest job search: public HTML fragments, no login.

The guest endpoints answer plain HTTP requests, so no browser is needed.
Search config: "params" for seeMoreJobPostings (location, f_TPR, f_WT, keywords).
"""

from __future__ import annotations

import re
from dataclasses import replace

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobhunt.collectors import Context, Listing, PostingGone, Summary
from jobhunt.text import tidy

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
PAGE_SIZE = 10
RESULT_CAP = 1000  # LinkedIn stops serving results near here
_URN = re.compile(r"urn:li:jobPosting:(\d+)")


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    found: dict[str, Summary] = {}
    for start in range(0, RESULT_CAP, PAGE_SIZE):
        params = dict(ctx.config.get("params") or {}) | {"start": start}
        page = ctx.fetcher.get(ctx.source_id, SEARCH, params=params)
        if not page.ok or not page.text.strip():
            break
        cards = [_card(node) for node in LexborHTMLParser(page.text).css("div.base-card[data-entity-urn]")]
        fresh = {s.external_id: s for s in cards if s and s.external_id not in found}
        if not fresh:
            break
        found |= fresh
        if not (set(fresh) - ctx.known(fresh)) or ctx.out_of_time():
            break
    return Listing(list(found.values()), set(found))


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    page = ctx.fetcher.get(ctx.source_id, DETAIL.format(id=summary.external_id))
    if page.status in (404, 410):
        raise PostingGone(summary.url)
    tree = LexborHTMLParser(page.text)
    markup = tree.css_first("div.show-more-less-html__markup")
    criteria = {
        _text(item.css_first(".description__job-criteria-subheader")).lower():
            _text(item.css_first(".description__job-criteria-text"))
        for item in tree.css("li.description__job-criteria-item")
    }
    fields = summary.fields | {
        "employment_type": criteria.get("employment type"),
        "seniority_level": criteria.get("seniority level"),
    }
    return replace(summary, description=markup.html if markup else "", fields=fields)


def _card(node: LexborNode) -> Summary | None:
    match = _URN.search(node.attributes.get("data-entity-urn") or "")
    link = node.css_first("a.base-card__full-link")
    if not match or link is None:
        return None
    posted = node.css_first("time[datetime]")
    return Summary(
        external_id=match.group(1),
        url=(link.attributes.get("href") or "").split("?")[0],
        title=_text(node.css_first("h3.base-search-card__title")),
        company=_text(node.css_first("h4.base-search-card__subtitle")),
        location=_text(node.css_first("span.job-search-card__location")),
        posted_at=posted.attributes.get("datetime") if posted else None,
    )


def _text(node: LexborNode | None) -> str:
    return tidy(node.text(separator=" ")) if node else ""
