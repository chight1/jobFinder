"""Backups to a separate private git repo, and restoring from it.

The database is saved as a text SQL dump, not the .db file: git then stores only the lines
that changed each night, so a year of backups stays small. Packets are copied alongside.
The page cache and GeoNames lists are left out; both can be downloaded again.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobfinder import db
from jobfinder.config import Config

DUMP_NAME = "jobfinder.sql"
PACKETS_NAME = "packets"


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupResult:
    at: str
    status: str  # "pushed abc1234", "no changes", or "failed: ..."


def backup(cfg: Config) -> BackupResult:
    """Dump, commit and push. The outcome is recorded for the Health page whether it worked or not."""
    at = db.utc_now()
    try:
        status = _backup(cfg)
    except (BackupError, OSError, sqlite3.Error) as exc:
        status = f"failed: {exc}"
    conn = db.connect(cfg.db_path)
    try:
        db.set_setting(conn, "last_backup_at", at)
        db.set_setting(conn, "last_backup_status", status)
    finally:
        conn.close()
    return BackupResult(at, status)


def _backup(cfg: Config) -> str:
    repo = cfg.backup_dir
    if not (repo / ".git").is_dir():
        raise BackupError(f"{repo} is not a git repository")
    _write_dump(cfg.db_path, repo / DUMP_NAME)
    _mirror(cfg.packets_dir, repo / PACKETS_NAME)

    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain"):
        return "no changes"
    _git(repo, "commit", "-q", "-m", f"Backup {datetime.now():%Y-%m-%d %H:%M}")
    _git(repo, "push", "-q")
    return f"pushed {_git(repo, 'rev-parse', '--short', 'HEAD')}"


def _write_dump(db_path: Path, target: Path) -> None:
    """A consistent snapshot as SQL text, ending with the schema version so a restore migrates correctly."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")  # one read transaction: the nightly run may be writing meanwhile
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(f"{line}\n" for line in conn.iterdump())
            f.write(f"PRAGMA user_version = {version};\n")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _mirror(source: Path, target: Path) -> None:
    """Make target an exact copy of source, so deleted packets disappear from the backup too."""
    if target.exists():
        shutil.rmtree(target)
    if source.exists():
        shutil.copytree(source, target)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)  # checked below
    if result.returncode != 0:
        raise BackupError(f"git {args[0]}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def restore(cfg: Config, *, force: bool = False) -> Path:
    """Rebuild jobfinder.db from the backup repo's dump. An existing database is kept as jobfinder.db.bak."""
    dump = cfg.backup_dir / DUMP_NAME
    if not dump.exists():
        raise BackupError(f"no backup at {dump}")
    if cfg.db_path.exists():
        if not force:
            raise BackupError(f"{cfg.db_path} exists; pass --force to replace it (it is kept as .bak)")
        backup_copy = cfg.db_path.with_suffix(".db.bak")
        backup_copy.unlink(missing_ok=True)
        cfg.db_path.rename(backup_copy)
        for suffix in ("-wal", "-shm"):
            Path(f"{cfg.db_path}{suffix}").unlink(missing_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.executescript(dump.read_text(encoding="utf-8"))
    finally:
        conn.close()
    _mirror(cfg.backup_dir / PACKETS_NAME, cfg.packets_dir)
    return cfg.db_path
