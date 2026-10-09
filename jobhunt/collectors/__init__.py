"""Collectors: one module per collector type, all with the same two calls.

    list_postings(ctx, cursor) -> Listing
    fetch_detail(ctx, summary) -> Summary   (the summary with its full description)

Collectors never touch the database; the runner does the bookkeeping around them.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Callable, Iterable

from jobhunt.fetch import Fetcher


class BoardGone(Exception):
    """The board answered 404: the company no longer uses this hiring system there."""


class PostingGone(Exception):
    """The posting's own page is gone."""


@dataclass(frozen=True)
class Summary:
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    posted_at: str | None = None  # ISO 8601
    description: str | None = None  # HTML or plain text
    fields: dict[str, Any] = field(default_factory=dict)  # the source's structured fields
    from_link: bool = False  # only a link from a list page: title and location are not reliable yet


@dataclass
class Listing:
    summaries: list[Summary]
    seen_ids: set[str]  # every id present on the source this time
    cursor: dict[str, Any] | None = None  # where to resume next run; None starts over


@dataclass
class Context:
    source_id: str
    config: dict[str, Any]  # the source's config; for a board, the board's extra merged in
    fetcher: Fetcher
    known: Callable[[Iterable[str]], set[str]]  # which of these external ids are already stored
    deadline: float  # time.time() after which no new page is started
    board: dict[str, Any] | None = None  # the companies row, for hiring-system boards

    def out_of_time(self) -> bool:
        return time.time() >= self.deadline


_MODULES = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "workable": "workable",
    "smartrecruiters": "smartrecruiters",
    "workday": "workday",
    "hrmos": "pages",
    "talentio": "pages",
    "html": "pages",
    "rss": "rss",
    "json_api": "json_apis",
    "hn": "hn",
    "browser": "browser",
}
RUNNABLE = frozenset(_MODULES)
BOARD_COLLECTORS = frozenset({"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday", "hrmos", "talentio"})


def get(collector: str) -> ModuleType:
    """The module for a collector type. Imported on demand: the browser one needs Playwright."""
    return importlib.import_module(f"jobhunt.collectors.{_MODULES[collector]}")


# --- small parsing helpers shared by collectors -------------------------------------------


def iso_from_ms(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def iso_from_s(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def join_place(*parts: Any) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())
