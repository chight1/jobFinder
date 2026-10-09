"""The nightly run: a fetch worker and a model worker side by side, then scoring and the summary.

The fetch worker uses the network until fetch_stop; the model worker uses the processor
until model_stop and always takes the highest-priority work first.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Iterator

from jobhunt import collectors, db, packets, score
from jobhunt.backup import backup
from jobhunt.collectors.runner import Runner, Shared, Tally, record_outcome
from jobhunt.config import Config, reload_skills
from jobhunt.dedupe import AgencyIndex, judge_pair, merge_jobs
from jobhunt.fetch import DailyCapReached, Fetcher, SourceBlocked
from jobhunt.llm import EXTRACT_PROMPT_VERSION, LLM, InvalidOutput, LLMError, extract
from jobhunt.rules import Gazetteer
from jobhunt.text import to_text

log = logging.getLogger(__name__)

PER_TENANT_HOSTS = frozenset({"workday"})  # each board has its own site, so a block is that board's alone


def next_clock(after: datetime, hhmm: str) -> datetime:
    """The first local time hh:mm after a moment."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > after else candidate + timedelta(days=1)


# --- the whole night ---------------------------------------------------------------------------


def nightly(cfg: Config) -> dict[str, Any]:
    started = datetime.now().astimezone()
    fetch_stop = next_clock(started, cfg.schedule["fetch_stop"])
    model_stop = next_clock(fetch_stop, cfg.schedule["model_stop"])
    conn = db.connect(cfg.db_path)
    run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (db.utc_now(),)).lastrowid
    gaz = Gazetteer.from_geonames(cfg.geonames_dir)

    fetch_done = threading.Event()
    results: dict[str, Any] = {}

    def fetch_worker() -> None:
        try:
            results["sources"] = collect(cfg, gaz, deadline=fetch_stop.timestamp(), run_started=started)
        except Exception:
            log.exception("fetch worker failed")
        finally:
            fetch_done.set()

    fetch_thread = threading.Thread(target=fetch_worker, name="fetch-worker")
    fetch_thread.start()
    try:
        results["model"] = process(cfg, gaz, stop_at=model_stop.timestamp(), fetch_done=fetch_done)
    except Exception:
        log.exception("model worker failed")
    finally:
        fetch_thread.join()
        counts = finalize(conn, cfg, gaz, run_id, started, results)
        conn.close()
    if cfg.backup_nightly:
        result = backup(cfg)
        log.info("backup: %s", result.status)
    return counts


# --- fetch worker ---------------------------------------------------------------------------------


def collect(
    cfg: Config,
    gaz: Gazetteer,
    *,
    deadline: float,
    run_started: datetime,
    only_source: str | None = None,
    dry_run: bool = False,
) -> dict[str, dict[str, Any]]:
    """Run every due source (or one), up to parallel_sites at a time. Returns a tally per source."""
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, cfg.sources_csv)
    sources = _due_sources(conn, cfg, only_source)
    fetcher = Fetcher(cfg, run_started=run_started, dry_run=dry_run)
    shared = Shared(
        cfg=cfg,
        gaz=gaz,
        fetcher=fetcher,
        deadline=deadline,
        aliases={r["alias_norm"]: r["company_norm"] for r in conn.execute("SELECT * FROM company_aliases")},
        source_kinds={r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM sources")},
        agency_index=AgencyIndex(lambda: _open_job_texts(cfg)),
        dry_run=dry_run,
    )
    conn.close()
    try:
        with ThreadPoolExecutor(max_workers=int(cfg.fetch["parallel_sites"])) as pool:
            futures = {source["id"]: pool.submit(_run_source, shared, source) for source in sources}
            return {source_id: asdict(future.result()) for source_id, future in futures.items()}
    finally:
        fetcher.close()


def _due_sources(conn: sqlite3.Connection, cfg: Config, only: str | None) -> list[sqlite3.Row]:
    if only:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (only,)).fetchone()
        if row is None:
            raise SystemExit(f"no source named {only!r} in sources.csv")
        return [row]
    rows = conn.execute(
        "SELECT * FROM sources WHERE enabled = 1 AND fail_streak < ? "
        "AND (backoff_until IS NULL OR backoff_until <= ?)",
        (int(cfg.fetch["disable_after_fails"]), db.utc_now()),
    ).fetchall()
    return [r for r in rows if r["collector"] in collectors.RUNNABLE]


def _due_boards(conn: sqlite3.Connection, ats: str) -> list[sqlite3.Row]:
    """Active and new boards nightly, cold weekly, dead monthly; dropped never."""
    now = datetime.now(UTC)
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")
    return conn.execute(
        """
        SELECT * FROM companies WHERE ats = ? AND (
            tier IN ('new', 'active')
            OR (tier = 'cold' AND (last_checked_at IS NULL OR last_checked_at <= ?))
            OR (tier = 'dead' AND (last_checked_at IS NULL OR last_checked_at <= ?))
        )
        ORDER BY last_checked_at IS NOT NULL, last_checked_at
        """,
        (ats, week_ago, month_ago),
    ).fetchall()


def _run_source(shared: Shared, source: sqlite3.Row) -> Tally:
    """One source inside its own error boundary: a broken site is logged and skipped."""
    conn = db.connect(shared.cfg.db_path)
    runner = Runner(shared, conn)
    tally = Tally()
    blocked: str | None = None
    worker = f"fetch:{source['id']}"
    started = db.utc_now()

    def show(doing: str) -> None:
        if not shared.dry_run:
            db.set_activity(conn, worker, doing)

    try:
        show(f"collecting {source['name']}")
        if source["collector"] in collectors.BOARD_COLLECTORS:
            for board in _due_boards(conn, source["collector"]):
                if time.time() >= shared.deadline:
                    break
                own_site = source["collector"] in PER_TENANT_HOSTS
                show(f"collecting {source['name']} board {board['slug']}")
                try:
                    board_tally = runner.run_board(source, board)
                except SourceBlocked as exc:
                    if not own_site:
                        raise  # the shared API host pushed back: stop the whole system
                    tally.errors += 1
                    log.warning("%s board %s blocked: %s", source["id"], board["slug"], exc)
                    continue
                except DailyCapReached:
                    raise
                except Exception as exc:
                    log.warning("%s board %s failed: %r", source["id"], board["slug"], exc)
                    board_tally = Tally(errors=1, status=f"stopped: {exc!r}"[:200])
                tally.add(board_tally)
                # Any error ends the site for the run. On Workday each board is its own site.
                if board_tally.status.startswith("stopped") and not own_site:
                    break
        else:
            tally = runner.run_source(source)
    except SourceBlocked as exc:
        blocked = str(exc)
        tally.add(runner.unfinished or Tally())
        tally.status = "blocked"
    except DailyCapReached:
        tally.add(runner.unfinished or Tally())
        tally.status = "daily cap reached"
    except Exception as exc:
        log.exception("source %s failed", source["id"])
        tally.errors += 1
        tally.status = f"error: {exc!r}"[:200]
    finally:
        if not shared.dry_run:
            requests = conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE source_id = ? AND at >= ?", (source["id"], started)
            ).fetchone()[0]
            record_outcome(conn, shared.cfg, source, tally, blocked, requests=requests)
            db.clear_activity(conn, worker)
        conn.close()
    log.info("%s: %s", source["id"], tally)
    return tally


def _open_job_texts(cfg: Config) -> Iterator[tuple[int, str]]:
    conn = db.connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT j.id, s.description FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        yield row["id"], to_text(row["description"])


# --- model worker -----------------------------------------------------------------------------


Task = tuple[str, Any]  # ("packet", job_id), ("pair", (a, b)), ("extract", job_id)


def process(cfg: Config, gaz: Gazetteer, *, stop_at: float, fetch_done: threading.Event | None = None) -> dict[str, int]:
    """Take model work by priority until stop_at; starts no new call after it.

    1. packets for jobs marked apply  2. borderline duplicate pairs  3. extraction, highest prescore first
    A task that fails is skipped for the rest of the run; only a failing server stops the worker.
    """
    counts = {"packets": 0, "pairs": 0, "extracted": 0, "extract_failed": 0, "removed": 0}
    with _model_lock(cfg.db_path.parent / "model.lock") as only_worker:
        if not only_worker:
            log.info("a model worker is already running; the queue is left to it")
            return counts
        return _work_queue(cfg, gaz, stop_at, fetch_done, counts)


@contextmanager
def _model_lock(path: Path) -> Iterator[bool]:
    """True when no other model worker is running. Two would split the processor and could take the
    same job, so the second one stops. The OS frees the lock when a worker exits, even by crashing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0)
        try:
            _lock_file(handle, lock=True)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _lock_file(handle, lock=False)


def _lock_file(handle: IO[bytes], *, lock: bool) -> None:
    """Lock (without waiting) or unlock a file; the lock holds against every other open of it."""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB if lock else fcntl.LOCK_UN)


def _work_queue(cfg: Config, gaz: Gazetteer, stop_at: float, fetch_done: threading.Event | None,
                counts: dict[str, int]) -> dict[str, int]:
    conn = db.connect(cfg.db_path)
    try:
        llm = LLM(cfg, conn)
    except LLMError as exc:
        log.error("model worker not started: %s", exc)
        conn.close()
        return counts
    skipped: set[Task] = set()
    try:
        while time.time() < stop_at:
            reload_skills(cfg)  # levels changed on the Skills page apply from the next job on
            task = _next_task(conn, skipped)
            if task is None:
                if fetch_done is None or fetch_done.is_set():
                    break
                fetch_done.wait(30)  # the fetch worker may still queue jobs
                continue
            try:
                _run_task(conn, llm, cfg, gaz, task, counts)
            except LLMError:
                raise
            except Exception as exc:
                log.warning("model task %s failed: %r", task, exc)
                skipped.add(task)
                if task[0] == "extract":
                    _mark_extract_failed(conn, cfg, gaz, task[1], counts)
    except LLMError as exc:
        log.error("model worker stopped: %s", exc)
    finally:
        db.clear_activity(conn, "model")
        llm.close()
        conn.close()
    return counts


def _next_task(conn: sqlite3.Connection, skipped: set[Task]) -> Task | None:
    """The single most important piece of model work not yet skipped this run."""
    for row in conn.execute(
        "SELECT id FROM jobs WHERE status = 'apply' AND id NOT IN (SELECT job_id FROM packets) ORDER BY status_at"
    ):
        if ("packet", row["id"]) not in skipped:
            return "packet", row["id"]
    for pair in conn.execute("SELECT job_a, job_b FROM dupe_pairs WHERE verdict IS NULL"):
        if ("pair", (pair["job_a"], pair["job_b"])) not in skipped:
            return "pair", (pair["job_a"], pair["job_b"])
    job = conn.execute(
        "SELECT id FROM jobs WHERE stage = 'queued' ORDER BY prescore DESC, id LIMIT 1"
    ).fetchone()
    if job:
        return "extract", job["id"]  # a failed extraction leaves the queue, so it is never offered twice
    return None


def _describe(conn: sqlite3.Connection, task: Task) -> str:
    """The task in words for the Activity page, e.g. 'extracting #42 Backend Engineer'."""
    kind, target = task

    def title(job_id: int) -> str:
        row = conn.execute(
            "SELECT s.title FROM jobs j JOIN sightings s ON s.id = j.primary_sighting_id WHERE j.id = ?", (job_id,)
        ).fetchone()
        return f"#{job_id} {row['title'] if row else ''}".strip()

    if kind == "packet":
        return f"drafting a packet for {title(target)}"
    if kind == "pair":
        return f"deciding whether #{target[0]} and #{target[1]} are the same job"
    return f"extracting {title(target)}"


def _run_task(conn: sqlite3.Connection, llm: LLM, cfg: Config, gaz: Gazetteer, task: Task, counts: dict[str, int]) -> None:
    kind, target = task
    db.set_activity(conn, "model", _describe(conn, task))
    if kind == "packet":
        packets.make_packet(conn, llm, cfg, target)
        counts["packets"] += 1
    elif kind == "pair":
        a, b = target
        if judge_pair(conn, llm, a, b) == "same":
            merge_jobs(conn, keep=a, drop=b)
        else:
            conn.execute(
                "UPDATE dupe_pairs SET verdict = 'different', decided_by = 'model' WHERE job_a = ? AND job_b = ?", (a, b)
            )
        counts["pairs"] += 1
    elif kind == "extract":
        extract_job(conn, llm, cfg, gaz, target, counts)


def extract_job(
    conn: sqlite3.Connection, llm: LLM, cfg: Config, gaz: Gazetteer, job_id: int, counts: dict[str, int]
) -> None:
    """Facts from the model, then the fact rules and the formula, so the top 20 stays current."""
    try:
        facts = extract(conn, llm, cfg, job_id)
    except InvalidOutput as exc:
        log.warning("extraction failed for job %s: %s", job_id, exc)
        _mark_extract_failed(conn, cfg, gaz, job_id, counts)
        return
    with db.transaction(conn):  # facts and their verdict land together, never one without the other
        conn.execute(
            "UPDATE jobs SET stage = 'extracted', facts = ?, prompt_version = ? WHERE id = ?",
            (facts.model_dump_json(), EXTRACT_PROMPT_VERSION, job_id),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if score.apply_facts(conn, job, cfg, gaz) == "removed":
            counts["removed"] += 1
    counts["extracted"] += 1


def _mark_extract_failed(conn: sqlite3.Connection, cfg: Config, gaz: Gazetteer, job_id: int, counts: dict[str, int]) -> None:
    """The job stays in the list, scored on unknowns, with a "check" badge."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE jobs SET stage = 'extract_failed', facts = '{}', prompt_version = ? WHERE id = ?",
            (EXTRACT_PROMPT_VERSION, job_id),
        )
        score.apply_facts(conn, conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(), cfg, gaz)
    counts["extract_failed"] += 1


# --- 07:30: scoring, the summary ------------------------------------------------------------


def finalize(
    conn: sqlite3.Connection, cfg: Config, gaz: Gazetteer, run_id: int, started: datetime, results: dict[str, Any]
) -> dict[str, Any]:
    score.rescore_all(conn, cfg, gaz)

    sources: dict[str, dict[str, Any]] = results.get("sources", {})
    started_utc = started.astimezone(UTC).isoformat(timespec="seconds")
    requests = {
        r["source_id"]: r["n"]
        for r in conn.execute(
            "SELECT source_id, COUNT(*) AS n FROM fetch_log WHERE at >= ? GROUP BY source_id", (started_utc,)
        )
    }
    for source_id, tally in sources.items():
        tally["requests"] = requests.get(source_id, 0)
    model = results.get("model", {})
    counts = {
        "fetched": sum(requests.values()),
        "new_sightings": sum(t["new_sightings"] for t in sources.values()),
        "new_jobs": sum(t["new_jobs"] for t in sources.values()),
        "removed_by_code": sum(t["code_removed"] for t in sources.values()),
        "extracted": model.get("extracted", 0),
        "removed_after_extraction": model.get("removed", 0),
        "failed": model.get("extract_failed", 0),
        "model": model,
        "sources": sources,
    }
    conn.execute("UPDATE runs SET ended_at = ?, counts = ? WHERE id = ?", (db.utc_now(), db.dumps(counts), run_id))
    return counts

