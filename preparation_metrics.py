"""Bounded aggregate preparation measurements, with no per-operation log."""

from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import json
import time

IMPORT_STARTED = time.perf_counter()
_ACTIVE = ContextVar("pdf_preparation_metrics", default=None)
_STACK = ContextVar("pdf_preparation_metric_stack", default=())
MAX_STAGES = 20
MAX_SUMMARY_BYTES = 3840


class Measurements:
    def __init__(self, scope):
        self.scope = scope
        self.started = time.perf_counter()
        self.cpu_started = time.process_time()
        self.rows = {}
        self.merged_calls = 0

    def key(self, name):
        name = str(name)[:48]
        if name not in self.rows and len(self.rows) >= MAX_STAGES - 1:
            self.merged_calls += 1
            name = "other_measured"
        self.rows.setdefault(
            name,
            {"calls": 0, "wall_ms": 0.0, "self_wall_ms": 0.0, "python_cpu_ms": 0.0},
        )
        return name

    def snapshot(self):
        now = time.perf_counter()
        result = {
            "schema": 1,
            "scope": self.scope,
            "wall_ms": round(max(0, now - self.started) * 1000),
            "python_cpu_ms": round(
                max(0, time.process_time() - self.cpu_started) * 1000
            ),
            "stages": {
                name: {
                    k: round(v * 1000) if k != "calls" else v for k, v in row.items()
                }
                for name, row in self.rows.items()
            },
            "merged_stage_calls": self.merged_calls,
        }
        result["unattributed_wall_ms"] = max(
            0,
            result["wall_ms"]
            - sum(r["self_wall_ms"] for r in result["stages"].values()),
        )
        # A fixed-size numeric summary cannot grow with pages, retries or text.
        # Keep totals if unusual values exceed even the conservative byte cap.
        while (
            len(json.dumps(result, indent=2, ensure_ascii=False).encode())
            > MAX_SUMMARY_BYTES
            and result["stages"]
        ):
            result["stages"].pop(next(reversed(result["stages"])))
            result["merged_stage_calls"] += 1
            result["unattributed_wall_ms"] = max(
                0,
                result["wall_ms"]
                - sum(r["self_wall_ms"] for r in result["stages"].values()),
            )
        return result


@contextmanager
def measurement_scope(scope="one_pdf_before_summary_write"):
    measurements = Measurements(scope)
    token = _ACTIVE.set(measurements)
    stack_token = _STACK.set(())
    try:
        yield measurements
    finally:
        _STACK.reset(stack_token)
        _ACTIVE.reset(token)


def document_measurements(function):
    @wraps(function)
    def call(*args, **kwargs):
        with measurement_scope():
            return function(*args, **kwargs)

    return call


def result_measurements(function):
    """Attach one bounded aggregate to an existing vector-observation result."""

    @wraps(function)
    def call(*args, **kwargs):
        with measure("vector_verification"):
            with measurement_scope("one_vector_observation") as collector:
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    result["performance"] = collector.snapshot()
                return result

    return call


@contextmanager
def measure(stage):
    collector = _ACTIVE.get()
    if collector is None:
        yield
        return
    name = collector.key(stage)
    frame = [time.perf_counter(), time.process_time(), 0.0]
    stack = _STACK.get()
    token = _STACK.set((*stack, frame))
    try:
        yield
    finally:
        elapsed = max(0, time.perf_counter() - frame[0])
        cpu = max(0, time.process_time() - frame[1])
        _STACK.reset(token)
        if stack:
            stack[-1][2] += elapsed
        row = collector.rows.setdefault(
            name,
            {"calls": 0, "wall_ms": 0.0, "self_wall_ms": 0.0, "python_cpu_ms": 0.0},
        )
        row["calls"] += 1
        row["wall_ms"] += elapsed
        row["self_wall_ms"] += max(0, elapsed - frame[2])
        row["python_cpu_ms"] += cpu


def measured(stage):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            if _ACTIVE.get() is None:
                return function(*args, **kwargs)
            name = stage(args, kwargs) if callable(stage) else stage
            with measure(name):
                return function(*args, **kwargs)

        return call

    return decorate


def snapshot():
    collector = _ACTIVE.get()
    return collector.snapshot() if collector is not None else {}
