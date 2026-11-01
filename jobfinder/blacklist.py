"""Employers whose jobs are never shown. A blacklisted employer's jobs are code-removed with the rule
"blacklisted": the ones stored now, and every one a later run finds (the runner checks the list).

Taking an employer off brings its jobs back where they were: a job the model has read is judged
again from its facts, one it never read goes to the model queue.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jobfinder import db, score
from jobfinder.config import Config
from jobfinder.rules import BLACKLISTED, Gazetteer


def names(conn: sqlite3.Connection) -> frozenset[str]:
    """The blacklisted employers, as dedupe.company_norm keys."""
    return frozenset(row["company_norm"] for row in conn.execute("SELECT company_norm FROM blacklist"))


def add(conn: sqlite3.Connection, company_norm: str, company: str) -> int:
    """Blacklist an employer and hide its stored jobs. Returns how many jobs were hidden. A job another
    code rule already removed keeps that rule."""
    now = db.utc_now()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO blacklist (company_norm, company, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT (company_norm) DO NOTHING",
            (company_norm, company, now),
        )
        return conn.execute(
            "UPDATE jobs SET stage = 'code_removed', removal_rule = ?, removal_quote = ?, changed_at = ? "
            "WHERE company_norm = ? AND stage != 'code_removed'",
            (BLACKLISTED, company, now, company_norm),
        ).rowcount


def remove(conn: sqlite3.Connection, cfg: Config, gaz: Gazetteer, company_norm: str) -> int | None:
    """Take an employer off the list and bring its jobs back. Returns how many came back, or None when
    the employer was not on the list."""
    with db.transaction(conn):
        if not conn.execute("DELETE FROM blacklist WHERE company_norm = ?", (company_norm,)).rowcount:
            return None
        jobs = conn.execute(
            "SELECT id, facts FROM jobs WHERE company_norm = ? AND stage = 'code_removed' AND removal_rule = ?",
            (company_norm, BLACKLISTED),
        ).fetchall()
        now = db.utc_now()
        for job in jobs:
            if job["facts"] is None:
                conn.execute("UPDATE jobs SET stage = 'queued', removal_rule = NULL, removal_quote = NULL, "
                             "changed_at = ? WHERE id = ?", (now, job["id"]))
                continue
            # The stage apply_facts starts from: a failed extraction stored empty facts.
            stage = "extract_failed" if job["facts"] == "{}" else "extracted"
            conn.execute("UPDATE jobs SET stage = ?, changed_at = ? WHERE id = ?", (stage, now, job["id"]))
            score.apply_facts(conn, conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone(), cfg, gaz)
        return len(jobs)


def entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every blacklisted employer, newest first, with how many jobs it hides."""
    return [dict(row) for row in conn.execute(
        "SELECT b.company_norm, b.company, b.added_at, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.company_norm = b.company_norm AND j.removal_rule = ?) AS hidden "
        "FROM blacklist b ORDER BY b.added_at DESC",
        (BLACKLISTED,),
    )]
