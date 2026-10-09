"""The model worker's single-instance check."""

from __future__ import annotations

from jobhunt.nightly import _model_lock


def test_only_one_model_worker_at_a_time(tmp_path):
    path = tmp_path / "model.lock"
    with _model_lock(path) as first:
        with _model_lock(path) as second:
            assert first and not second
    with _model_lock(path) as after:
        assert after  # freed when the first worker finished
