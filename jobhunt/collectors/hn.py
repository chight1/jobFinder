"""Hacker News "Who is hiring?": each top-level comment in the month's thread is a posting."""

from __future__ import annotations

from typing import Any

from jobhunt.collectors import Context, Listing, Summary
from jobhunt.rules import looks_like_role
from jobhunt.text import to_text

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


def _summary(comment: dict[str, Any], thread_date: str) -> Summary:
    """Postings open with a line like 'Company | Role | Location | REMOTE | Salary'.

    The segments that name a role become the title; the others are kept as the location,
    so a role such as "AI Engineer" is never read as a place.
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
        location=" | ".join(s for s in rest if not looks_like_role(s)),
        posted_at=thread_date,
        description=comment["text"],
        fields={"thread_date": thread_date},
    )
