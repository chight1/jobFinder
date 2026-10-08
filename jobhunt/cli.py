"""Command-line entry point: jobhunt init | nightly | collect | discover | process | rescore | reextract | packet |
evalextract | backup | restore | serve."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from jobhunt import config as config_module
from jobhunt import db
from jobhunt.config import Config

GEONAMES = "https://download.geonames.org/export/dump/"
GEONAMES_FILES = ("countryInfo.txt", "admin1CodesASCII.txt", "cities15000.zip")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Nightly job collection and ranking.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="folder holding config.toml (default: here)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create folders and the database, load sources.csv, download GeoNames")
    commands.add_parser("nightly", help="the full nightly run")

    collect = commands.add_parser("collect", help="run the collectors now")
    collect.add_argument("--source", help="one source id from sources.csv")
    collect.add_argument("--dry-run", action="store_true", help="print parsed postings, write nothing")
    collect.add_argument("--minutes", type=float, default=240, help="stop starting new pages after this long")

    discover = commands.add_parser("discover", help="find company boards in Common Crawl and the company lists")
    discover.add_argument("--force", action="store_true", help="re-read the current crawl")

    process = commands.add_parser("process", help="run the model worker now")
    process.add_argument("--minutes", type=float, default=60)

    commands.add_parser("rescore", help="re-apply the fact rules and the formula; no model calls")

    reextract = commands.add_parser("reextract", help="queue jobs first seen since a date for extraction again")
    reextract.add_argument("--since", required=True, help="YYYY-MM-DD")

    packet = commands.add_parser("packet", help="draft one application packet now")
    packet.add_argument("job_id", type=int)

    commands.add_parser("backup", help="dump the database and packets to the backup repo, commit and push")
    restore = commands.add_parser("restore", help="rebuild the database from the backup repo")
    restore.add_argument("--force", action="store_true", help="replace an existing database (kept as .bak)")

    evalextract = commands.add_parser("evalextract", help="score the model's extraction against the labeled test set")
    evalextract.add_argument("--export", type=int, metavar="N",
                             help="instead: write N postings from the database to the test set for labeling")

    serve = commands.add_parser("serve", help="the list page on localhost")
    serve.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    # Job titles carry emoji and accents; a Windows console defaults to a code page that can't print them.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = config_module.load(args.root)
    _setup_logging(cfg)
    handler = {
        "init": cmd_init, "nightly": cmd_nightly, "collect": cmd_collect, "discover": cmd_discover,
        "process": cmd_process, "rescore": cmd_rescore, "reextract": cmd_reextract,
        "packet": cmd_packet, "evalextract": cmd_evalextract, "backup": cmd_backup, "restore": cmd_restore,
        "serve": cmd_serve,
    }[args.command]
    return handler(cfg, args) or 0


def _setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(cfg.data_dir / "jobhunt.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _connect(cfg: Config):
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return conn


def _gazetteer(cfg: Config):
    from jobhunt.rules import Gazetteer

    if not (cfg.geonames_dir / "cities15000.txt").exists():
        raise SystemExit("GeoNames files missing: run 'jobhunt init' first")
    return Gazetteer.from_geonames(cfg.geonames_dir)


# --- commands ------------------------------------------------------------------------------


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.fetch import Fetcher

    for folder in (cfg.data_dir, cfg.raw_dir, cfg.geonames_dir, cfg.packets_dir, cfg.resumes_dir):
        folder.mkdir(parents=True, exist_ok=True)
    conn = _connect(cfg)
    db.sync_sources(conn, cfg.sources_csv)
    conn.close()

    fetcher = Fetcher(cfg, run_started=datetime.now().astimezone())
    try:
        for name in GEONAMES_FILES:
            target = cfg.geonames_dir / name
            if target.exists() or (name.endswith(".zip") and target.with_suffix(".txt").exists()):
                continue
            print(f"downloading {name}")
            fetcher.download("geonames", GEONAMES + name, target)
            if name.endswith(".zip"):
                with zipfile.ZipFile(target) as archive:
                    archive.extractall(cfg.geonames_dir)
                target.unlink()
    finally:
        fetcher.close()
    print(f"ready: {cfg.db_path}")
    return 0


def cmd_nightly(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.nightly import nightly

    _connect(cfg).close()
    counts = nightly(cfg)
    print(json.dumps({k: v for k, v in counts.items() if k != "sources"}, indent=2))
    return 0


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.nightly import collect

    _connect(cfg).close()
    tallies = collect(
        cfg,
        _gazetteer(cfg),
        deadline=time.time() + args.minutes * 60,
        run_started=datetime.now().astimezone(),
        only_source=args.source,
        dry_run=args.dry_run,
    )
    for source_id, tally in tallies.items():
        print(f"{source_id}: {tally}", file=sys.stderr)
    return 0


def cmd_discover(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.discover import discover

    _connect(cfg).close()
    print(discover(cfg, force=args.force))
    return 0


def cmd_process(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.nightly import process

    _connect(cfg).close()
    print(process(cfg, _gazetteer(cfg), stop_at=time.time() + args.minutes * 60))
    return 0


def cmd_rescore(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.score import rescore_all

    conn = _connect(cfg)
    print(f"rescored {rescore_all(conn, cfg, _gazetteer(cfg))} jobs")
    conn.close()
    return 0


def cmd_reextract(cfg: Config, args: argparse.Namespace) -> int:
    since = datetime.fromisoformat(args.since).date().isoformat()
    conn = _connect(cfg)
    count = conn.execute(
        "UPDATE jobs SET stage = 'queued', score = NULL WHERE first_seen_at >= ? "
        "AND stage IN ('kept', 'removed', 'extract_failed')",
        (since,),
    ).rowcount
    conn.close()
    print(f"queued {count} jobs; the next model window extracts them ('jobhunt process' to start now)")
    return 0


def cmd_packet(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.llm import LLM
    from jobhunt.packets import make_packet

    conn = _connect(cfg)
    llm = LLM(cfg, conn)
    try:
        make_packet(conn, llm, cfg, args.job_id)
    finally:
        llm.close()
        conn.close()
    print(cfg.packets_dir / f"{args.job_id}.md")
    return 0


def cmd_evalextract(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt import evalset
    from jobhunt.llm import LLM

    conn = _connect(cfg)
    try:
        if args.export:
            written = evalset.export(conn, evalset.set_dir(cfg), args.export)
            print(f"wrote {written} postings to {evalset.set_dir(cfg)}; fill in each file's facts")
            return 0
        llm = LLM(cfg, conn)
        try:
            report = evalset.evaluate(cfg, llm, evalset.set_dir(cfg))
        finally:
            llm.close()
    finally:
        conn.close()
    print("\n".join(report.lines()))
    return 0


def cmd_backup(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.backup import backup

    _connect(cfg).close()
    result = backup(cfg)
    print(f"backup {result.at}: {result.status}")
    return 1 if result.status.startswith("failed") else 0


def cmd_restore(cfg: Config, args: argparse.Namespace) -> int:
    from jobhunt.backup import BackupError, restore

    try:
        path = restore(cfg, force=args.force)
    except BackupError as exc:
        raise SystemExit(str(exc)) from exc
    _connect(cfg).close()  # applies any migrations newer than the backup
    print(f"restored {path} from {cfg.backup_dir}")
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from jobhunt.web.app import create_app

    _connect(cfg).close()
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port or int(cfg.list["port"]))
    return 0
