from __future__ import annotations

import dataclasses

import pytest

from jobhunt import backup, db
from tests.conftest import ROOT


def test_dump_and_restore_round_trip(cfg, conn, tmp_path):
    db.sync_sources(conn, ROOT / "sources.csv")
    db.set_setting(conn, "last_list_visit", "2026-10-08T00:00:00+00:00")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()  # Windows cannot move an open database file

    repo = tmp_path / "backup-repo"
    repo.mkdir()
    cfg = dataclasses.replace(cfg, backup_dir=repo)
    backup._write_dump(cfg.db_path, repo / backup.DUMP_NAME)

    with pytest.raises(backup.BackupError):
        backup.restore(cfg)  # an existing database is never replaced by accident
    backup.restore(cfg, force=True)

    restored = db.connect(cfg.db_path)
    try:
        assert restored.execute("PRAGMA user_version").fetchone()[0] == version
        assert restored.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == sources
        assert db.get_setting(restored, "last_list_visit") == "2026-10-08T00:00:00+00:00"
    finally:
        restored.close()
    assert cfg.db_path.with_suffix(".db.bak").exists()


def test_backup_reports_a_missing_repo_as_failure(cfg, conn, tmp_path):
    cfg = dataclasses.replace(cfg, backup_dir=tmp_path / "not-a-repo")
    result = backup.backup(cfg)
    assert result.status.startswith("failed")
    assert db.get_setting(conn, "last_backup_status") == result.status
