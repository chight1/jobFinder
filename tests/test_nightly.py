"""The model worker: one at a time, an always-on one waits instead of stopping, and a pause holds it."""

from __future__ import annotations

from jobhunt import db, nightly
from jobhunt.nightly import _model_lock, process


class Clock:
    """Stands in for the time module, so a minute's wait takes no time."""

    def __init__(self, on_sleep=lambda: None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        self.on_sleep()


def test_only_one_model_worker_at_a_time(tmp_path):
    path = tmp_path / "model.lock"
    with _model_lock(path) as first:
        with _model_lock(path) as second:
            assert first and not second
    with _model_lock(path) as after:
        assert after  # freed when the first worker finished


def test_an_idle_worker_waits_for_work_and_says_so(cfg, conn, gaz, monkeypatch):
    seen = []
    clock = Clock(on_sleep=lambda: seen.append(conn.execute("SELECT doing FROM activity WHERE worker = 'model'").fetchone()))
    monkeypatch.setattr(nightly, "time", clock)
    process(cfg, gaz, stop_at=150, idle_s=60)
    assert clock.sleeps == 2  # looked at 0, 60 and 120; the next look would pass the stop
    assert [row["doing"] for row in seen] == ["waiting for work", "waiting for work"]
    assert conn.execute("SELECT * FROM activity WHERE worker = 'model'").fetchone() is None


def test_a_second_worker_leaves_a_held_queue_alone(cfg, conn, gaz, monkeypatch):
    worked = []
    monkeypatch.setattr(nightly, "_work_queue", lambda *args: worked.append(True))
    with _model_lock(cfg.db_path.parent / "model.lock"):
        process(cfg, gaz, stop_at=float("inf"), idle_s=60)
    assert not worked
    assert conn.execute("SELECT * FROM activity WHERE worker = 'run:process'").fetchone() is None  # the note is the holder's


def test_a_paused_worker_takes_no_work_until_played(cfg, conn, gaz, monkeypatch):
    db.set_setting(conn, nightly.PAUSED, "1")
    seen = []
    clock = Clock(on_sleep=lambda: seen.append(conn.execute("SELECT doing FROM activity WHERE worker = 'model'").fetchone()))
    monkeypatch.setattr(nightly, "time", clock)
    looked = []
    monkeypatch.setattr(nightly, "_next_task", lambda conn, skipped: looked.append(clock.now))
    process(cfg, gaz, stop_at=12, idle_s=60)
    assert not looked and clock.sleeps == 2  # a look every 5 seconds, at 0 and 5; the next would pass the stop
    assert [row["doing"] for row in seen] == ["paused", "paused"]

    db.set_setting(conn, nightly.PAUSED, "0")
    process(cfg, gaz, stop_at=24, idle_s=60)
    assert looked == [10]  # played: the first look goes to the queue
