"""Read-only views into the database and the running workers, for the Data, Job and Activity pages."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunt import db
from jobhunt.config import Profile
from jobhunt.score import canonical_skill, skill_level
from jobhunt.text import to_text

# Tables shown on the Data page, most useful first.
TABLES = (
    "jobs", "sightings", "sources", "companies", "dupe_pairs", "packets",
    "fetch_log", "llm_log", "runs", "activity", "settings", "company_aliases",
)
PAGE_SIZE = 50
CELL_CHARS = 120
STALE_AFTER = timedelta(minutes=30)  # a worker note this old means its process probably died
LOG_TAIL_BYTES = 64 * 1024


# --- Data page ----------------------------------------------------------------------


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}


def table_page(conn: sqlite3.Connection, table: str, query: str, page: int) -> dict[str, Any]:
    """One page of a table, newest rows first, optionally filtered by text in any column."""
    if table not in TABLES:  # the name goes into SQL, so only known tables are allowed
        raise KeyError(table)
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    where, params = "", []
    if query:
        where = "WHERE " + " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columns)
        params = [f"%{query}%"] * len(columns)
    total = conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return {
        "columns": columns,
        "rows": [[_cell(row[c]) for c in columns] for row in rows],
        "total": total,
        "pages": max(1, -(-total // PAGE_SIZE)),
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + "…"


# --- Job page -------------------------------------------------------------------------


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    sightings = conn.execute(
        "SELECT s.*, src.name AS source_name FROM sightings s JOIN sources src ON src.id = s.source_id "
        "WHERE s.job_id = ? ORDER BY s.id = ? DESC, s.first_seen_at",
        (job_id, job["primary_sighting_id"]),
    ).fetchall()
    rows = [dict(s) | {"fields": db.loads(s["source_fields"], {})} for s in sightings]
    primary = next((s for s in rows if s["id"] == job["primary_sighting_id"]), None)
    return {
        "job": dict(job),
        "facts": db.loads(job["facts"], {}) or {},
        "score_parts": db.loads(job["score_parts"], {}) or {},
        "badges": db.loads(job["badges"], []),
        "restored_rules": db.loads(job["restored_rules"], []),
        "sightings": rows,
        "primary": primary,
        "description": to_text(primary["description"]) if primary else "",
        "model_calls": conn.execute(
            "SELECT kind, seconds, ok, at, error FROM llm_log WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall(),
        "pairs": conn.execute(
            "SELECT * FROM dupe_pairs WHERE job_a = ? OR job_b = ?", (job_id, job_id)
        ).fetchall(),
    }


# --- Skills page --------------------------------------------------------------------------

# Which jobs the Skills page counts: the ones still in play, or every job the model has read.
# Either way, jobs that require Japanese (by the model, or moved there by hand) count only when asked for.
SKILL_SCOPES = {
    "kept": "stage IN ('kept', 'japanese')",
    "all": "facts IS NOT NULL AND facts != '{}'",
}
_NOT_JAPANESE = "NOT COALESCE(japanese_override, json_extract(facts, '$.japanese') IS 'required')"


def _skill_jobs(scope: str, japanese: bool) -> str:
    """The WHERE clause for a scope's jobs, with or without the ones that require Japanese."""
    return SKILL_SCOPES[scope] if japanese else f"{SKILL_SCOPES[scope]} AND {_NOT_JAPANESE}"


def skill_stats(
    conn: sqlite3.Connection, profile: Profile, scope: str = "kept", *, japanese: bool = False
) -> dict[str, Any]:
    """Every skill named across the scope's jobs, most common first, spellings merged ("JS" = "JavaScript")."""
    jobs = conn.execute(f"SELECT id, facts, score FROM jobs WHERE {_skill_jobs(scope, japanese)}").fetchall()
    stats: dict[str, dict[str, Any]] = {}
    for job in jobs:
        facts = db.loads(job["facts"], {}) or {}
        main = canonical_skill(facts.get("primary_language") or "")
        for key in {canonical_skill(s) for s in facts.get("stack") or [] if s.strip()}:
            entry = stats.setdefault(key, {"key": key, "spellings": Counter(), "jobs": 0, "main": 0, "scores": []})
            entry["jobs"] += 1
            entry["main"] += key == main
            if job["score"] is not None:
                entry["scores"].append(job["score"])
        for spelling in facts.get("stack") or []:
            if spelling.strip():
                stats[canonical_skill(spelling)]["spellings"][spelling.strip()] += 1

    rows = []
    for entry in stats.values():
        scores = entry["scores"]
        rows.append({
            "key": entry["key"],
            "name": entry["spellings"].most_common(1)[0][0],
            "jobs": entry["jobs"],
            "share": entry["jobs"] / len(jobs) if jobs else 0.0,
            "main": entry["main"],
            "level": skill_level(entry["key"], profile),
            "avg_score": sum(scores) / len(scores) if scores else None,
        })
    rows.sort(key=lambda r: (-r["jobs"], r["name"].lower()))
    return {"skills": rows, "job_count": len(jobs)}


def unseen_skills(profile: Profile, stats: dict[str, Any]) -> list[tuple[str, int]]:
    """Skills in the profile that none of skill_stats' jobs name, strongest first."""
    seen = {s["key"] for s in stats["skills"]}
    return sorted(
        ((name, level) for name, level in profile.skills.items() if canonical_skill(name) not in seen),
        key=lambda item: (-item[1], item[0]),
    )


def distinct_skill_count(conn: sqlite3.Connection) -> int:
    """How many different skills the kept jobs name, for the Skills tab. Japanese jobs left out."""
    keys = set()
    for row in conn.execute(f"SELECT facts FROM jobs WHERE {_skill_jobs('kept', False)}"):
        keys |= {canonical_skill(s) for s in (db.loads(row["facts"], {}) or {}).get("stack") or [] if s.strip()}
    return len(keys)


def job_ids_with_skill(conn: sqlite3.Connection, key: str) -> list[int]:
    """Jobs whose stack names this skill under any spelling; the jobs table's filters narrow them."""
    return [
        row["id"]
        for row in conn.execute("SELECT id, facts FROM jobs WHERE facts IS NOT NULL")
        if key in {canonical_skill(s) for s in (db.loads(row["facts"], {}) or {}).get("stack") or []}
    ]


# --- Activity page -----------------------------------------------------------------------


def activity_snapshot(conn: sqlite3.Connection, log_path: Path) -> dict[str, Any]:
    now = datetime.now(UTC)
    rows = conn.execute("SELECT * FROM activity ORDER BY worker").fetchall()
    ages = {row["worker"]: now - datetime.fromisoformat(row["since"]) for row in rows}
    # A worker note changes with every task, so its age shows whether it is alive. A run note
    # ("run:process") is written once at the start, so a long run is alive while any worker reports.
    worker_ages = [age for name, age in ages.items() if not name.startswith("run:")]
    workers_quiet = not worker_ages or min(worker_ages) > STALE_AFTER
    running = []
    for row in rows:
        age = ages[row["worker"]]
        is_run = row["worker"].startswith("run:")
        stale = age > STALE_AFTER and (workers_quiet if is_run else True)
        running.append({"worker": row["worker"], "doing": row["doing"], "age": _duration(age), "stale": stale})

    hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    queue = conn.execute("SELECT COUNT(*) FROM jobs WHERE stage = 'queued'").fetchone()[0]
    per_job = conn.execute(
        "SELECT AVG(seconds) FROM (SELECT seconds FROM llm_log WHERE kind = 'extract' AND ok = 1 "
        "ORDER BY id DESC LIMIT 20)"
    ).fetchone()[0]
    progress = {
        "queue": queue,
        "extracted_last_hour": conn.execute(
            "SELECT COUNT(*) FROM llm_log WHERE kind = 'extract' AND ok = 1 AND at >= ?", (hour_ago,)
        ).fetchone()[0],
        "requests_last_hour": conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE at >= ?", (hour_ago,)
        ).fetchone()[0],
        "seconds_per_job": per_job,
        "time_to_clear": _duration(timedelta(seconds=queue * per_job)) if per_job and queue else None,
    }

    feed = conn.execute(
        """
        SELECT at, 'web' AS kind, source_id AS who, COALESCE(CAST(status AS TEXT), 'no reply') AS what,
               url AS detail, ms / 1000.0 AS seconds, error,
               (status IS NULL OR status >= 400) AS bad
        FROM fetch_log
        UNION ALL
        SELECT at, 'model', kind,
               CASE WHEN ok = 1 THEN 'ok' WHEN error LIKE 'invalid reply%' THEN 'invalid reply' ELSE 'failed' END,
               CASE WHEN job_id IS NULL THEN '' ELSE '#' || job_id END, seconds, error, ok = 0
        FROM llm_log
        ORDER BY at DESC LIMIT 60
        """
    ).fetchall()
    return {"running": running, "progress": progress, "feed": feed, "log": _log_tail(log_path)}


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _log_tail(path: Path, lines: int = 40) -> list[str]:
    """The last lines of the log, reading only its end so a big log stays cheap."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]
