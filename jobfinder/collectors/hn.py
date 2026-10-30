"""Hacker News "Who is hiring?": each top-level comment in the month's thread is a posting."""

from __future__ import annotations

import re
from typing import Any

from jobfinder.collectors import Context, Listing, Summary
from jobfinder.rules import looks_like_role
from jobfinder.text import to_text

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
THREAD_PREFIX = "ask hn: who is hiring"


def list_postings(ctx: Context, cursor: dict | None) -> Listing:
    search = ctx.fetcher.get(ctx.source_id, SEARCH, kind="api", params={"tags": "story,author_whoishiring"})
    threads = [h for h in search.json().get("hits", []) if h.get("title", "").lower().startswith(THREAD_PREFIX)]
    if not threads:
        return Listing([], set())
    thread = max(threads, key=lambda h: h.get("created_at_i", 0))
    item = ctx.fetcher.get(ctx.source_id, ITEM.format(id=thread["objectID"]), kind="api").json()
    summaries = [
        _summary(comment, thread["created_at"])
        for comment in item.get("children") or []
        if comment.get("text") and comment.get("author")
    ]
    return Listing(summaries, {s.external_id for s in summaries})


def fetch_detail(ctx: Context, summary: Summary) -> Summary:
    return summary  # the comment is the whole posting


# Words in a first-line segment that are plainly not a place: links, pay, employment type, funding
# stage, visa terms. They are cut out, and what is left of the segment is kept as location.
_URL = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|io|ai|dev|co|org|net|app|xyz|tech|so)\b\S*", re.IGNORECASE)
_NOT_PLACE = re.compile(
    r"(?:up\s+to\s*~?\s*)?[$€£¥]\s*[\d.,]+\s*[km]?(?:\s*(?:-|–|to)\s*[$€£¥]?\s*[\d.,]+\s*[km]?)?(?:\s*/\s*(?:hr|hour|yr|year))?"
    r"|\b\d[\d.,]*\s*k\b(?:\s*(?:-|–|to)\s*\d[\d.,]*\s*k\b)?"
    r"|\b(?:full|part)[- ]?time\b|\b(?:usd|eur|gbp|jpy|cad|aud)\b"
    r"|\b(?:contract(?:or)?|freelance|1099|intern(?:ship)?s?|equity|salary|total\s+compensation|compensation|benefits)\b"
    r"|\b(?:no\s+)?visas?(?:\s+sponsor\w*)?(?:\s+(?:offered|available|possible|provided))?\b|\bsponsor\w*\b"
    r"|\bseries\s+[a-e]\b|\byc\s*[swfx]\d{2}\b",
    re.IGNORECASE,
)


def _summary(comment: dict[str, Any], thread_date: str) -> Summary:
    """Postings open with a line like 'Company | Role | Location | REMOTE | Salary'.

    The segments that name a role become the title, so a role such as "AI Engineer" is never read
    as a place. Of the rest, the ones that are plainly something else (pay, a link, "Full-time")
    are dropped, and what remains is the location.
    """
    first_line = to_text(comment["text"]).split("\n", 1)[0]
    segments = [s.strip() for s in first_line.split("|")]
    company, rest = (segments[0], segments[1:]) if len(segments) > 1 else ("", segments)
    roles = [s for s in rest if looks_like_role(s)]
    return Summary(
        external_id=str(comment["id"]),
        url=f"https://news.ycombinator.com/item?id={comment['id']}",
        title=(" | ".join(roles) or first_line)[:200],
        company=company[:100],
        location=location_from(rest),
        posted_at=thread_date,
        description=comment["text"],
        fields={"thread_date": thread_date},
    )


def location_from(segments: list[str]) -> str:
    """The first-line segments that are not a role, with links, pay and the like cut out of them.
    A segment with nothing left ("Full-time", "$150k–$200k") is dropped."""
    kept = []
    for segment in segments:
        if not segment or looks_like_role(segment):
            continue
        rest = _NOT_PLACE.sub(" ", _URL.sub(" ", segment))
        rest = re.sub(r"\(\s*\)|\[\s*\]", " ", rest)  # brackets left empty
        rest = re.sub(r"\s+", " ", rest).strip(" ,;:-–+~/|")
        if re.search(r"[A-Za-z]{2}", rest):
            kept.append(rest)
    return " | ".join(kept)
