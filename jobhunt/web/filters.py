"""Filters for the jobs table. Every job is in one table, and the page narrows it with filters, as a
ServiceNow list does.

A filter is a set of on/off options, each with its own SQL condition on the job (`j`). A job passes a
filter when an option that is on matches it, and it shows when it passes every filter. The query string
lists the options that are on, one parameter per option (?status=applied&status=interviewing; a lone
empty value turns them all off). A filter the query leaves out uses its defaults, so a plain /jobs is
the open list: kept, not Japanese, no status.

Adding a filter is one more entry in FILTERS. The API's query parameters and the page's filter bar
are both built from it."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, create_model


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    where: str
    params: tuple[Any, ...] = ()
    default: bool = True  # on when the query leaves its filter out


@dataclass(frozen=True)
class Filter:
    key: str
    label: str
    # Fixed options, or a function that reads them from the database (one per removal rule, say).
    options: tuple[Option, ...] | Callable[[sqlite3.Connection], tuple[Option, ...]]
    look: Literal["buttons", "dots"] = "buttons"

    def options_now(self, conn: sqlite3.Connection) -> tuple[Option, ...]:
        return self.options(conn) if callable(self.options) else self.options


# The model's answer, unless the candidate moved the job by hand. 0 for a job without facts.
NEEDS_JAPANESE = "COALESCE(j.japanese_override, json_extract(j.facts, '$.japanese') = 'required', 0)"
REMOVED = "j.stage IN ('removed', 'code_removed')"

# In the order a job's life goes. The API's Status literal must name the same ones (a test checks).
STATUS_LABELS = {
    "none": "No status",
    "apply": "To apply",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "rejected": "Rejected",
    "ghosted": "Ghosted",
    "not_interested": "Not interested",
}


def _removal_options(conn: sqlite3.Connection) -> tuple[Option, ...]:
    """Kept jobs, and one option per rule that has removed any."""
    rules = [r[0] for r in conn.execute(
        f"SELECT DISTINCT removal_rule FROM jobs j WHERE {REMOVED} AND removal_rule IS NOT NULL ORDER BY 1")]
    kept = Option("kept", "Kept", "j.stage IN ('kept', 'japanese', 'extract_failed')")
    return (kept, *(Option(rule, rule, f"{REMOVED} AND j.removal_rule = ?", (rule,), default=False) for rule in rules))


FILTERS: tuple[Filter, ...] = (
    Filter("rating", "Rating", (
        Option("green", "Green", "j.rating = 'green'"),
        Option("yellow", "Yellow", "j.rating = 'yellow'"),
        Option("red", "Red", "j.rating = 'red'"),
        Option("grey", "Not rated", "j.rating IS NULL"),
    ), look="dots"),
    Filter("japanese", "Japanese", (
        Option("japanese", "Japanese", f"{NEEDS_JAPANESE} = 1", default=False),
        Option("not_japanese", "Not Japanese", f"{NEEDS_JAPANESE} = 0"),
    )),
    Filter("status", "Status", tuple(
        Option(key, label, "j.status = ?", (key,), default=key == "none") for key, label in STATUS_LABELS.items()
    )),
    Filter("removed", "Removed", _removal_options),
)

# The query parameters, one list per filter: None when the query leaves the filter out.
FilterQuery = create_model("FilterQuery", **{f.key: (list[str] | None, None) for f in FILTERS})


class OptionState(BaseModel):
    key: str
    label: str
    on: bool


class FilterState(BaseModel):
    key: str
    label: str
    look: Literal["buttons", "dots"]
    options: list[OptionState]


def apply(conn: sqlite3.Connection, query: BaseModel) -> tuple[str, list[Any], list[FilterState]]:
    """The WHERE clause and parameters for the filters in the query, and each filter's options with
    which are on, for the filter bar."""
    clauses: list[str] = []
    params: list[Any] = []
    states: list[FilterState] = []
    for f in FILTERS:
        chosen = getattr(query, f.key)
        options = f.options_now(conn)
        on = [o for o in options if (o.default if chosen is None else o.key in chosen)]
        clauses.append("(" + " OR ".join(f"({o.where})" for o in on) + ")" if on else "0")
        params += [p for o in on for p in o.params]
        states.append(FilterState(key=f.key, label=f.label, look=f.look,
                                  options=[OptionState(key=o.key, label=o.label, on=o in on) for o in options]))
    return " AND ".join(clauses), params, states
