"""Bounded system observations; never read process command lines or credentials."""

import collections
import ctypes
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
import psutil
from .windows_resources import power, system_counters


def inventory():
    result = {}
    for p in psutil.process_iter(
        ["pid", "ppid", "name", "create_time", "cpu_times", "memory_info"]
    ):
        try:
            d = p.info
            c = d["cpu_times"]
            m = d["memory_info"]
            if c is not None and d["pid"] != 0:
                result[(d["pid"], d["create_time"])] = {
                    "pid": d["pid"],
                    "ppid": d["ppid"],
                    "name": d["name"],
                    "cpu": c.user + c.system,
                    "rss_bytes": m.rss if m else None,
                }
        except psutil.NoSuchProcess, psutil.AccessDenied, OSError:
            pass
    return result


def machine_configuration():
    try:
        services = collections.Counter(s.status() for s in psutil.win_service_iter())
    except psutil.Error, OSError:
        services = None
    try:
        scheme = subprocess.check_output(
            ["powercfg", "/getactivescheme"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        ).strip()
    except OSError, subprocess.SubprocessError:
        scheme = None
    freq = psutil.cpu_freq()
    power_modes = {}
    for supply in ("AC", "DC"):
        try:
            function = getattr(
                ctypes.WinDLL("powrprof"),
                "PowerGetUserConfigured" + supply + "PowerMode",
            )
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_ulong
            buffer = ctypes.create_string_buffer(16)
            error = function(ctypes.byref(buffer))
            power_modes[supply] = (
                {"guid": str(uuid.UUID(bytes_le=buffer.raw))}
                if error == 0
                else {"unavailable_error": error}
            )
        except (AttributeError, OSError) as error:
            power_modes[supply] = {"unavailable": type(error).__name__}
    return {
        "logical_cpus": psutil.cpu_count(),
        "physical_cores": psutil.cpu_count(logical=False),
        "worker_affinity_available_cpus": len(psutil.Process().cpu_affinity()),
        "power": power(),
        "active_windows_power_scheme": scheme,
        "user_configured_power_modes": power_modes,
        "power_mode_limit": "User preferences may be overridden by system conditions; thermal throttling is not measured",
        "services_by_status": dict(services) if services else None,
        "reported_frequency_mhz": freq._asdict() if freq else None,
        "frequency_limit": "Windows psutil frequency is generally nominal, not a measured per-core clock or thermal observation",
        "psutil_version": psutil.__version__,
    }


class Observer:
    def __init__(self, path, interval=2, inventory_interval=15):
        self.path = Path(path)
        self.interval = interval
        self.stop_event = threading.Event()
        self.worker_pids = set()
        self.inventory_interval = inventory_interval

    def start(self):
        self.thread = threading.Thread(
            target=self.run, name="benchmark-environment", daemon=True
        )
        self.thread.start()

    def run(self):
        previous = inventory()
        previous_time = time.perf_counter()
        with self.path.open("a", encoding="utf8") as out:
            while not self.stop_event.wait(self.interval):
                start = time.perf_counter()
                m = psutil.virtual_memory()
                # Callers can replace/register workers while sampling. Use one
                # stable ownership view for this sample and its memory loop.
                worker_pids = self.worker_pids.copy()
                deep = None
                if start - previous_time >= self.inventory_interval:
                    now = inventory()
                    elapsed = start - previous_time
                    deltas = []
                    for key, r in now.items():
                        if key in previous:
                            cpu = max(0, r["cpu"] - previous[key]["cpu"])
                            if cpu:
                                deltas.append(
                                    {
                                        **r,
                                        "cpu_delta_seconds": cpu,
                                        "percent_one_core": cpu / elapsed * 100,
                                    }
                                )
                    parents = {p["pid"]: p["ppid"] for p in now.values()}

                    def owned(pid):
                        seen = set()
                        while pid and pid not in seen:
                            if pid in worker_pids:
                                return True
                            seen.add(pid)
                            pid = parents.get(pid)
                        return False

                    external = [p for p in deltas if not owned(p["pid"])]
                    deep = {
                        "observed_at_monotonic": start,
                        "interval_seconds": elapsed,
                        "processes_with_observed_cpu_activity": len(deltas),
                        "top_external_cpu_processes": sorted(
                            external, key=lambda p: p["cpu_delta_seconds"], reverse=True
                        )[:5],
                        "top_rss_processes": sorted(
                            now.values(),
                            key=lambda p: p["rss_bytes"] or 0,
                            reverse=True,
                        )[:5],
                    }
                    previous, previous_time = now, start
                worker_memory = {}
                for pid in worker_pids:
                    try:
                        worker_memory[str(pid)] = psutil.Process(pid).memory_info().rss
                    except (psutil.Error, OSError):
                        pass
                event = {
                    "at_monotonic": start,
                    "sample_interval_seconds": self.interval,
                    "system_cpu": system_counters(),
                    "power": power(),
                    "system_memory_percent": m.percent,
                    "system_available_bytes": m.available,
                    "system_total_bytes": m.total,
                    "process_count": len(psutil.pids()),
                    "process_inventory": deep,
                    "worker_rss_bytes": worker_memory,
                    "observer_cpu_seconds_since_start": time.process_time(),
                    "sampling_work_wall_seconds": time.perf_counter() - start,
                }
                out.write(json.dumps(event, separators=(",", ":")) + "\n")
                out.flush()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
