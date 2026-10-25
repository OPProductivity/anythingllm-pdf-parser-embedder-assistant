import pytest
import subprocess
import sys
import time

if sys.platform != "win32":
    pytest.skip("Windows process accounting APIs", allow_module_level=True)

pytestmark = pytest.mark.offline_deterministic

from benchmarks.windows_resources import ChildAccounting, Measurement, derived  # noqa: E402 - Windows-only import after module skip


def test_machine_capacity_formula_keeps_multicore_cpu_separate_from_elapsed():
    before = {
        "kernel_including_idle_seconds": 100,
        "user_seconds": 100,
        "idle_seconds": 50,
    }
    after = {
        "kernel_including_idle_seconds": 110,
        "user_seconds": 106,
        "idle_seconds": 58,
    }
    r = derived(1, 1, 3, before, after, 16)
    assert r["app_cpu_seconds"] == 4
    assert r["equivalent_busy_cores"] == 4
    assert r["app_percent_machine_capacity"] == 25
    assert r["system_busy_cpu_seconds"] == 8
    assert r["system_busy_percent"] == 50
    assert r["other_system_busy_cpu_seconds_estimate"] == 4
    assert r["other_system_percent_machine_capacity_estimate"] == 25


def test_short_child_cpu_is_collected_after_exit_without_process_sampling():
    children = ChildAccounting()
    children.install()
    try:
        with Measurement(children) as m:
            # The Windows venv python.exe is a redirector that starts another
            # process. Use a direct executable, as Tesseract uses in the suite.
            subprocess.run(
                [
                    sys._base_executable,
                    "-c",
                    "import time; end=time.process_time()+0.12\nwhile time.process_time()<end: pass",
                ],
                capture_output=True,
                check=True,
            )
        r = m.result()
        assert r["children"]["count"] == 1
        assert r["children"]["cpu_seconds"] >= 0.09
        assert r["children"]["missing_cpu_counters"] == 0
        assert r["children"]["cycles"] > 0
        assert r["memory_before"]["system_total_bytes"] > 0
        assert r["derived"]["app_cpu_seconds"] >= r["children"]["cpu_seconds"]
    finally:
        from benchmarks.windows_resources import REAL_POPEN

        subprocess.Popen = REAL_POPEN


def test_sleep_is_elapsed_time_not_cpu_work():
    children = ChildAccounting()
    with Measurement(children) as m:
        time.sleep(0.08)
    r = m.result()
    assert r["wall_seconds"] >= 0.07
    assert r["parent_cpu_seconds"] < r["wall_seconds"]
