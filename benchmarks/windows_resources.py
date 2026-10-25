"""Benchmark-only Windows counters; no application evidence/result reuse."""

import ctypes
import hashlib
from ctypes import wintypes
import os
import subprocess
import threading
import time

import psutil

KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
    ctypes.POINTER(wintypes.FILETIME)
] * 4
KERNEL.GetProcessTimes.restype = wintypes.BOOL
KERNEL.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
KERNEL.GetSystemTimes.restype = wintypes.BOOL
KERNEL.QueryProcessCycleTime.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_ulonglong),
]
KERNEL.QueryProcessCycleTime.restype = wintypes.BOOL
KERNEL.GetCurrentProcess.restype = wintypes.HANDLE


def seconds(value):
    return ((value.dwHighDateTime << 32) + value.dwLowDateTime) / 10_000_000


def process_counters(handle):
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    ok = KERNEL.GetProcessTimes(
        handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)
    )
    cycles = ctypes.c_ulonglong()
    has_cycles = KERNEL.QueryProcessCycleTime(handle, ctypes.byref(cycles))
    return {
        "cpu_seconds": seconds(k) + seconds(u) if ok else None,
        "cycles": cycles.value if has_cycles else None,
    }


def system_counters():
    idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
    if not KERNEL.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    # Windows kernel time includes idle. Interrupt/DPC counters are not added
    # separately: doing that would double-count part of kernel activity.
    return {
        "idle_seconds": seconds(idle),
        "kernel_including_idle_seconds": seconds(kernel),
        "user_seconds": seconds(user),
    }


class PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ac", ctypes.c_ubyte),
        ("battery_flag", ctypes.c_ubyte),
        ("percent", ctypes.c_ubyte),
        ("saver", ctypes.c_ubyte),
        ("remaining", ctypes.c_ulong),
        ("full", ctypes.c_ulong),
    ]


def power():
    p = PowerStatus()
    if not KERNEL.GetSystemPowerStatus(ctypes.byref(p)):
        return {"available": False}
    return {
        "available": True,
        "ac": None if p.ac == 255 else int(p.ac),
        "battery_percent": None if p.percent == 255 else int(p.percent),
        "battery_saver": int(p.saver),
    }


def memory():
    m = psutil.virtual_memory()
    p = psutil.Process()
    info = p.memory_info()
    return {
        "process_rss_bytes": info.rss,
        "process_private_bytes": getattr(info, "private", None),
        "process_peak_rss_since_worker_start_bytes": getattr(info, "peak_wset", None),
        "system_total_bytes": m.total,
        "system_available_bytes": m.available,
        "system_percent": m.percent,
        "process_count": len(psutil.pids()),
        "threads": p.num_threads(),
    }


def derived(wall, parent_cpu, child_cpu, system_before, system_after, logical_cpus):
    app = parent_cpu + child_cpu
    capacity = wall * logical_cpus
    result = {
        "app_cpu_seconds": app,
        "equivalent_busy_cores": app / wall if wall else None,
        "app_percent_machine_capacity": 100 * app / capacity if capacity else None,
    }
    if system_before and system_after:
        total = sum(
            system_after[k] - system_before[k]
            for k in ("kernel_including_idle_seconds", "user_seconds")
        )
        idle = system_after["idle_seconds"] - system_before["idle_seconds"]
        busy = total - idle
        external = busy - app
        result.update(
            system_busy_cpu_seconds=busy,
            system_total_cpu_seconds=total,
            system_busy_percent=100 * busy / total if total else None,
            other_system_busy_cpu_seconds_estimate=external,
            other_system_percent_machine_capacity_estimate=100 * external / capacity
            if capacity
            else None,
        )
    return result


REAL_POPEN = subprocess.Popen


class ChildAccounting:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = []

    def install(self):
        owner = self

        class TrackedPopen(REAL_POPEN):
            def __init__(self, *args, **kwargs):
                self._accounted = False
                self._account_start = time.perf_counter()
                super().__init__(*args, **kwargs)

            def communicate(self, *args, **kwargs):
                payload = args[0] if args else kwargs.get("input")
                if isinstance(payload, bytes):
                    self._input_digest = hashlib.sha256(payload).hexdigest()
                result = super().communicate(*args, **kwargs)
                # Read the live OS handle before Popen closes it. Sampling the
                # process tree alone misses short-lived OCR subprocesses.
                if not self._accounted:
                    self._accounted = True
                    counters = process_counters(int(self._handle))
                    with owner.lock:
                        owner.records.append(
                            {
                                **counters,
                                "name": os.path.basename(self.args[0]),
                                "wall_seconds": time.perf_counter()
                                - self._account_start,
                                "returncode": self.returncode,
                                "input_sha256": getattr(self, "_input_digest", None),
                                "output_sha256": hashlib.sha256(result[0]).hexdigest()
                                if isinstance(result[0], bytes)
                                else None,
                                "ocr_arguments": list(self.args[1:])
                                if "tesseract"
                                in os.path.basename(self.args[0]).casefold()
                                else None,
                            }
                        )
                return result

        subprocess.Popen = TrackedPopen

    def reset(self):
        with self.lock:
            self.records.clear()

    def summary(self):
        with self.lock:
            records = list(self.records)
        return {
            "count": len(records),
            "cpu_seconds": sum(r["cpu_seconds"] or 0 for r in records),
            "cycles": sum(r["cycles"] or 0 for r in records),
            "missing_cpu_counters": sum(r["cpu_seconds"] is None for r in records),
            "missing_cycle_counters": sum(r["cycles"] is None for r in records),
            "processes": records,
        }


class Measurement:
    def __init__(self, children):
        self.children = children

    def __enter__(self):
        self.children.reset()
        self.memory_before = memory()
        self.power_before = power()
        self.system_before = system_counters()
        self.parent_before = process_counters(KERNEL.GetCurrentProcess())
        self.start = time.perf_counter()
        return self

    def __exit__(self, *error):
        self.wall = time.perf_counter() - self.start
        self.parent_after = process_counters(KERNEL.GetCurrentProcess())
        self.system_after = system_counters()
        self.power_after = power()
        self.memory_after = memory()
        self.child = self.children.summary()
        self.parent_cpu = (
            self.parent_after["cpu_seconds"] - self.parent_before["cpu_seconds"]
        )

    def result(self):
        cpus = psutil.cpu_count(logical=True) or 1
        return {
            "wall_seconds": self.wall,
            "parent_cpu_seconds": self.parent_cpu,
            "parent_cpu_cycles": self.parent_after["cycles"]
            - self.parent_before["cycles"]
            if self.parent_after["cycles"] is not None
            and self.parent_before["cycles"] is not None
            else None,
            "children": self.child,
            "logical_cpus": cpus,
            "system_before": self.system_before,
            "system_after": self.system_after,
            "derived": derived(
                self.wall,
                self.parent_cpu,
                self.child["cpu_seconds"],
                self.system_before,
                self.system_after,
                cpus,
            ),
            "power_before": self.power_before,
            "power_after": self.power_after,
            "memory_before": self.memory_before,
            "memory_after": self.memory_after,
            "started_monotonic": self.start,
            "finished_monotonic": self.start + self.wall,
        }
