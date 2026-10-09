"""Schema, migrations and shared queries for the one SQLite file."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

# Each entry runs once, in order; PRAGMA user_version records how many have run.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        collector TEXT NOT NULL,
        kind TEXT NOT NULL,              -- employer, japan_board, global_board, agent
        link TEXT NOT NULL DEFAULT '',
        config TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        base_delay_s REAL,               -- from sources.csv; NULL = the fetch default
        min_delay_s REAL,                -- current gap, raised after a block; NULL = base
        daily_cap INTEGER,
        backoff_until TEXT,
        fail_streak INTEGER NOT NULL DEFAULT 0,
        cursor TEXT,
        last_run_at TEXT,
        last_status TEXT
    );

    CREATE TABLE companies (
        id INTEGER PRIMARY KEY,
        ats TEXT NOT NULL,
        slug TEXT NOT NULL,
        extra TEXT NOT NULL DEFAULT '{}',
        found_via TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT 'new', -- new, active, cold, dead, dropped
        last_checked_at TEXT,
        last_job_count INTEGER,
        dead_streak INTEGER NOT NULL DEFAULT 0,
        cursor TEXT,                     -- where a paged board resumes
        UNIQUE (ats, slug)
    );

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        company_norm TEXT NOT NULL,
        title_norm TEXT NOT NULL,
        city_norm TEXT NOT NULL,
        primary_sighting_id INTEGER,
        stage TEXT NOT NULL,             -- code_removed, queued, extract_failed, removed, kept
        removal_rule TEXT,
        removal_quote TEXT,
        restored_rules TEXT NOT NULL DEFAULT '[]',
        prescore REAL NOT NULL DEFAULT 0,
        facts TEXT,
        prompt_version TEXT,
        score REAL,
        score_parts TEXT,
        reason_line TEXT,
        badges TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'none',
        status_at TEXT,
        follow_up_on TEXT,
        first_seen_at TEXT NOT NULL,
        closed_at TEXT
    );

    CREATE TABLE sightings (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id),
        board_id INTEGER REFERENCES companies(id),
        external_id TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        company_raw TEXT NOT NULL DEFAULT '',
        location_raw TEXT NOT NULL DEFAULT '',
        posted_at TEXT,
        description TEXT,
        desc_hash TEXT,
        source_fields TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        miss_streak INTEGER NOT NULL DEFAULT 0,
        closed_at TEXT,
        job_id INTEGER REFERENCES jobs(id)
    );

    CREATE TABLE dupe_pairs (
        job_a INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        job_b INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        title_sim REAL,
        desc_sim REAL,
        verdict TEXT,                    -- NULL (waiting), same, different, probably_same
        decided_by TEXT,                 -- model, minhash
        PRIMARY KEY (job_a, job_b)
    );

    CREATE TABLE comparisons (
        job_a INTEGER NOT NULL,          -- always the smaller id
        job_b INTEGER NOT NULL,
        winner INTEGER NOT NULL,
        why TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (job_a, job_b)
    );

    CREATE TABLE company_aliases (
        alias_norm TEXT PRIMARY KEY,
        company_norm TEXT NOT NULL
    );

    CREATE TABLE fetch_log (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL,
        url TEXT NOT NULL,
        status INTEGER,
        bytes INTEGER NOT NULL DEFAULT 0,
        ms INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL,
        error TEXT
    );

    CREATE TABLE llm_log (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,              -- extract, dupe, compare, packet
        job_id INTEGER,
        seconds REAL NOT NULL,
        ok INTEGER NOT NULL,
        at TEXT NOT NULL
    );

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        counts TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE packets (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
        resume_file TEXT NOT NULL,
        letter TEXT NOT NULL,
        answers TEXT NOT NULL,
        gaps TEXT NOT NULL,
        made_at TEXT NOT NULL
    );

    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE INDEX sightings_source_ext ON sightings(source_id, external_id);
    CREATE INDEX sightings_board ON sightings(board_id);
    CREATE INDEX sightings_job ON sightings(job_id);
    CREATE INDEX jobs_key ON jobs(company_norm, title_norm, city_norm);
    CREATE INDEX jobs_queue ON jobs(stage, prescore);
    CREATE INDEX jobs_score ON jobs(score);
    CREATE INDEX fetch_log_source_at ON fetch_log(source_id, at);
    """,
    # When a job first reached the list, so an open list page can add it live.
    """
    ALTER TABLE jobs ADD COLUMN listed_at TEXT;
    CREATE INDEX jobs_listed ON jobs(listed_at);
    """,
    # What each running command and worker is doing right now, for the Activity page.
    """
    CREATE TABLE activity (
        worker TEXT PRIMARY KEY,         -- run:nightly, fetch:tokyodev, model, ...
        doing TEXT NOT NULL,
        since TEXT NOT NULL
    );
    """,
    # How much the candidate likes a job, set from its tile; separate from status, so rated jobs stay listed.
    """
    ALTER TABLE jobs ADD COLUMN rating TEXT;  -- green, yellow, red, or NULL
    """,
    """
    -- text.short_location for rows stored before it existed: more than 10 places becomes "Anywhere".
    UPDATE sightings
    SET location_raw = 'Anywhere (' || (length(location_raw) - length(replace(location_raw, ';', '')) + 1) || ' places)'
    WHERE length(location_raw) - length(replace(location_raw, ';', '')) >= 10;
    """,
    """
    -- Why a model call has ok = 0: the server's error, or "invalid reply: ..." when the answer
    -- arrived but was not the JSON asked for.
    ALTER TABLE llm_log ADD COLUMN error TEXT;
    """,
    """
    -- A source's totals from its latest run, nightly or one source by hand (JSON: requests, parsed, ...).
    ALTER TABLE sources ADD COLUMN last_tally TEXT;
    """,
    """
    -- The comparator is gone: lists sort by score alone.
    DROP TABLE comparisons;
    """,
    """
    -- The candidate moves a job between the Japanese list and the main one by hand: 1 Japanese, 0 not, NULL the
    -- model's answer decides. A job restored from the old "Requires Japanese" removal was a manual "not".
    ALTER TABLE jobs ADD COLUMN japanese_override INTEGER;
    UPDATE jobs SET japanese_override = 0,
        restored_rules = (SELECT json_group_array(value) FROM json_each(restored_rules)
                          WHERE value != 'Requires Japanese')
    WHERE EXISTS (SELECT 1 FROM json_each(restored_rules) WHERE value = 'Requires Japanese');
    """,
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    return default if text is None else json.loads(text)


def connect(path: Path) -> sqlite3.Connection:
    """One connection per thread. WAL lets the list page read while the nightly run writes."""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- activity: what is running right now -----------------------------------------


def set_activity(conn: sqlite3.Connection, worker: str, doing: str) -> None:
    conn.execute(
        "INSERT INTO activity (worker, doing, since) VALUES (?, ?, ?) "
        "ON CONFLICT (worker) DO UPDATE SET doing = excluded.doing, since = excluded.since",
        (worker, doing, utc_now()),
    )


def clear_activity(conn: sqlite3.Connection, worker: str) -> None:
    conn.execute("DELETE FROM activity WHERE worker = ?", (worker,))


@contextmanager
def activity(conn: sqlite3.Connection, worker: str, doing: str) -> Iterator[None]:
    """Show `doing` on the Activity page for as long as the block runs."""
    set_activity(conn, worker, doing)
    try:
        yield
    finally:
        clear_activity(conn, worker)


# --- sources.csv -------------------------------------------------------------

def sync_sources(conn: sqlite3.Connection, csv_path: Path) -> None:
    """Load sources.csv into the sources table and seed the company boards it names.

    Run-time state (backoff, fail streak, cursor, current gap) is left alone, so the
    file can be edited and re-synced at any time.
    A row whose config has a "board" key is one company's board on a hiring system:
    it becomes a companies row, not a source.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with transaction(conn):
        for row in rows:
            config = json.loads(row["config"] or "{}")
            if "board" in config:
                conn.execute(
                    "INSERT INTO companies (ats, slug, extra, found_via) VALUES (?, ?, ?, 'sources.csv') "
                    "ON CONFLICT (ats, slug) DO UPDATE SET extra = excluded.extra",
                    (row["collector"], config["board"], dumps(config.get("extra", {}))),
                )
                continue
            conn.execute(
                """
                INSERT INTO sources (id, name, collector, kind, link, config, enabled, base_delay_s, daily_cap)
                VALUES (:id, :name, :collector, :kind, :link, :config, :enabled, :base_delay_s, :daily_cap)
                ON CONFLICT (id) DO UPDATE SET
                    name = excluded.name, collector = excluded.collector, kind = excluded.kind,
                    link = excluded.link, config = excluded.config, enabled = excluded.enabled,
                    base_delay_s = excluded.base_delay_s, daily_cap = excluded.daily_cap
                """,
                {
                    "id": row["id"],
                    "name": row["name"],
                    "collector": row["collector"],
                    "kind": row["kind"],
                    "link": row["link"],
                    "config": dumps(config),
                    "enabled": int(row["enabled"]),
                    "base_delay_s": float(row["min_delay_s"]) if row["min_delay_s"] else None,
                    "daily_cap": int(row["daily_cap"]) if row["daily_cap"] else None,
                },
            )
