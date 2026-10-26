import itertools
import json
from contextlib import ExitStack
import pytest
import preparation_metrics as metrics

pytestmark = pytest.mark.offline_deterministic


def test_nested_spans_preserve_exclusive_time_and_scope(monkeypatch):
    monkeypatch.setattr(metrics.time, "perf_counter", lambda: next(wall))
    monkeypatch.setattr(metrics.time, "process_time", lambda: next(cpu))
    wall = itertools.count()
    cpu = itertools.count()
    with metrics.measurement_scope() as collector:
        with metrics.measure("outer"):
            with metrics.measure("inner"):
                pass
        result = collector.snapshot()
    assert result["stages"]["outer"]["wall_ms"] == 3000
    assert result["stages"]["outer"]["self_wall_ms"] == 2000
    assert result["stages"]["inner"]["self_wall_ms"] == 1000
    assert result["wall_ms"] == 5000
    assert result["unattributed_wall_ms"] == 2000
    assert metrics.snapshot() == {}


def test_repeated_calls_are_counts_not_events_and_storage_is_bounded(monkeypatch):
    def no_disk(*args, **kwargs):
        raise AssertionError("measurement must not write a log")

    monkeypatch.setattr("builtins.open", no_disk)
    with metrics.measurement_scope() as collector:
        for _ in range(1000):
            with metrics.measure("serialization"):
                pass
        for n in range(1000):
            with metrics.measure("untrusted-stage-" + str(n) + "x" * 1000):
                pass
        result = collector.snapshot()
    assert result["stages"]["serialization"]["calls"] == 1000
    assert len(result["stages"]) <= metrics.MAX_STAGES
    assert (
        len(json.dumps(result, indent=2, ensure_ascii=False).encode())
        <= metrics.MAX_SUMMARY_BYTES
    )
    assert result["merged_stage_calls"] > 0


def test_observation_preserves_return_values_exceptions_and_nesting():
    @metrics.measured("function")
    def function(value):
        if isinstance(value, Exception):
            raise value
        return value

    sentinel = object()
    assert function(sentinel) is sentinel
    with metrics.measurement_scope() as collector:
        assert function(sentinel) is sentinel
        error = RuntimeError("original failure")
        with pytest.raises(RuntimeError) as caught:
            function(error)
        assert caught.value is error
        assert collector.snapshot()["stages"]["function"]["calls"] == 2
    assert metrics.snapshot() == {}


def test_deeply_nested_spans_still_obey_storage_cap():
    with metrics.measurement_scope() as collector:
        with ExitStack() as stack:
            for n in range(100):
                stack.enter_context(metrics.measure("nested-" + str(n)))
        result = collector.snapshot()
    assert len(result["stages"]) <= metrics.MAX_STAGES
    assert len(json.dumps(result, indent=2).encode()) <= metrics.MAX_SUMMARY_BYTES
    assert result["merged_stage_calls"] > 0


def test_result_summary_is_bounded_and_preserves_parent_scope():
    @metrics.result_measurements
    def observe():
        with metrics.measure("scalar_identity_observation"):
            return {"status": "pass", "complete": True}

    with metrics.measurement_scope() as parent:
        value = observe()
        assert value["status"] == "pass" and value["complete"] is True
        assert parent.snapshot()["stages"]["vector_verification"]["calls"] == 1
        assert "scalar_identity_observation" not in parent.snapshot()["stages"]
    assert value["performance"]["scope"] == "one_vector_observation"
    assert (
        len(json.dumps(value["performance"], indent=2).encode())
        <= metrics.MAX_SUMMARY_BYTES
    )
    assert metrics.snapshot() == {}
