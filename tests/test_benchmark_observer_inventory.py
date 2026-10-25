"""Detailed inventories must not burden each cheap sample or rank idle as work."""

import json
import sys
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip("Windows benchmark observer", allow_module_level=True)

from benchmarks import environment as observer  # noqa: E402 - Windows-only module

pytestmark = pytest.mark.offline_deterministic


def test_inventory_excludes_the_windows_idle_pseudo_process(monkeypatch):
    processes = [
        SimpleNamespace(
            info={
                "pid": pid,
                "ppid": 0,
                "name": name,
                "create_time": 1,
                "cpu_times": SimpleNamespace(user=2, system=3),
                "memory_info": SimpleNamespace(rss=100),
            }
        )
        for pid, name in [(0, "System Idle Process"), (7, "ordinary.exe")]
    ]
    monkeypatch.setattr(
        observer.psutil, "process_iter", lambda attributes: iter(processes)
    )
    assert [r["pid"] for r in observer.inventory().values()] == [7]


def test_cheap_samples_do_not_repeat_full_inventories(monkeypatch, tmp_path):
    times = iter([0, 0.1, 0.2, 16, 16.1])
    monkeypatch.setattr(observer.time, "perf_counter", lambda: next(times))
    calls = []

    def inventory():
        calls.append(1)
        return {
            (7, 1): {
                "pid": 7,
                "ppid": 0,
                "name": "ordinary.exe",
                "cpu": len(calls),
                "rss_bytes": 100,
            }
        }

    monkeypatch.setattr(observer, "inventory", inventory)
    monkeypatch.setattr(observer, "power", lambda: {})
    monkeypatch.setattr(observer, "system_counters", lambda: {})
    monkeypatch.setattr(
        observer.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=50, available=100, total=200),
    )
    monkeypatch.setattr(observer.psutil, "pids", lambda: [7, 8])

    class Stop:
        count = 0

        def wait(self, interval):
            self.count += 1
            return self.count > 2

    job = observer.Observer(
        tmp_path / "observations.jsonl", interval=2, inventory_interval=15
    )
    job.stop_event = Stop()
    job.run()
    rows = [json.loads(x) for x in job.path.read_text().splitlines()]
    assert len(calls) == 2  # initial and one detailed interval, not every sample
    assert rows[0]["process_inventory"] is None
    assert rows[1]["process_inventory"]["top_external_cpu_processes"][0]["pid"] == 7
    assert all(r["process_count"] == 2 for r in rows)
