import json

import pytest

import finalization_timing as timing

pytestmark = pytest.mark.offline_deterministic


def test_finalization_timings_are_local_and_exclude_their_own_write(tmp_path, monkeypatch):
    values = iter([11.0, 12.0, 15.0, 16.0])
    monkeypatch.setattr(timing.time, 'perf_counter', lambda: next(values))
    start = timing.finalization_checkpoint(tmp_path, 'retention', 10.0)
    assert start == 12.0
    assert timing.finalization_checkpoint(tmp_path, 'publication', start) == 16.0
    rows = [json.loads(line) for line in (tmp_path / 'finalization-timing.jsonl').read_text().splitlines()]
    assert [(row['step'], row['elapsed_seconds']) for row in rows] == [('retention', 1.0), ('publication', 3.0)]
    assert all(row['recorded_at_utc'].endswith('+00:00') for row in rows)
    assert list(tmp_path.iterdir()) == [tmp_path / 'finalization-timing.jsonl']


def test_diagnostic_write_failure_does_not_change_run_outcome(tmp_path, monkeypatch, caplog):
    values = iter([11.0, 12.0])
    monkeypatch.setattr(timing.time, 'perf_counter', lambda: next(values))
    assert timing.finalization_checkpoint(tmp_path / 'missing', 'retention', 10.0) == 12.0
    assert 'Could not retain finalization timing' in caplog.text
