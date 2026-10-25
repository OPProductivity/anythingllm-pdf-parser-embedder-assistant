"""Completed-data rendering must keep category labels and work counts aligned."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from benchmarks import environment
from benchmarks.ocr_plots import plot_inputs, render

pytestmark = pytest.mark.offline_deterministic


def observations():
    groups = [{"group": key, "pdfs": count, "mean_seconds": 1.0 if count else None,
               "base_ocr_executions": 0, "ocr_rechecks": 0, "total_ocr_executions": 0,
               "time_share": count / 4}
              for key, count in [("low_outliers", 1), ("lower_whisker_to_Q1", 0),
                                  ("Q1_to_median", 0), ("median_to_Q3", 3),
                                  ("Q3_to_upper_whisker", 0), ("high_outliers", 0)]]
    return {"source": "Completed test observations", "distributions": {
        condition: {"pdfs": 4, "values": [.1, 1, 1, 1], "groups": deepcopy(groups)}
        for condition in ("original", "neutral_filename", "pure_neutral")}}


def test_sparse_group_labels_follow_their_actual_category_including_low_outliers():
    groups, labels = plot_inputs(observations())
    assert labels == ["Low outliers", "Median–Q3"]
    assert [g["pdfs"] for g in groups] == [1, 3]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_unusable_durations_are_reported_instead_of_plotted_as_observations(bad):
    data = observations()
    data["distributions"]["original"]["values"][0] = bad
    with pytest.raises(ValueError, match="finite nonnegative"):
        plot_inputs(data)


def test_ocr_readings_and_population_counts_have_to_reconcile():
    data = observations()
    data["distributions"]["neutral_filename"]["groups"][0]["total_ocr_executions"] = 1
    with pytest.raises(ValueError, match="Base readings plus rechecks"):
        plot_inputs(data)
    data = observations()
    data["distributions"]["neutral_filename"]["groups"][0]["pdfs"] = 2
    with pytest.raises(ValueError, match="reconcile"):
        plot_inputs(data)


def test_zero_ocr_workload_renders_both_formats_without_changing_observations(tmp_path):
    pytest.importorskip("matplotlib")
    data = observations()
    before = deepcopy(data)
    result = render(data, tmp_path / "plots")
    assert result["maximum_ocr_executions"] == 0
    assert (tmp_path / "plots.png").stat().st_size > 1000
    assert (tmp_path / "plots.svg").stat().st_size > 1000
    assert data == before


def test_registering_a_worker_during_sampling_does_not_stop_the_observer(tmp_path, monkeypatch):
    observer = environment.Observer(tmp_path / "observations.jsonl")
    observer.worker_pids = {1}
    waits = iter([False, True])
    observer.stop_event = SimpleNamespace(wait=lambda _interval: next(waits))
    monkeypatch.setattr(environment, "inventory", lambda: {})
    monkeypatch.setattr(environment, "system_counters", lambda: {})
    monkeypatch.setattr(environment, "power", lambda: {})
    monkeypatch.setattr(environment.psutil, "virtual_memory", lambda: SimpleNamespace(percent=20, available=80, total=100))
    monkeypatch.setattr(environment.psutil, "pids", lambda: [1, 2])

    def read_worker(_pid):
        observer.worker_pids.add(2)
        return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=10))

    monkeypatch.setattr(environment.psutil, "Process", read_worker)
    observer.run()
    rows = [json.loads(line) for line in observer.path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["worker_rss_bytes"] == {"1": 10}
    assert observer.worker_pids == {1, 2}
