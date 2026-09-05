"""Bounded Windows-only local diagnostics; never imported by the OCR pipeline.

Run: python -m diagnostic_runner --timeout 120 --output tmp/probe-unique -- python probe.py
Use only disposable/read-only local commands, not live mutation probes or servers.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as w
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t), ("active_limit", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                ("scheduling", w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _Job:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in [
            ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ]:
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.k.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


# Wait for the parent's permission before spawning anything. Assignment to the
# job therefore happens before the diagnostic can create descendants.
_BOOTSTRAP = "import json,subprocess,sys; c=json.loads(sys.stdin.readline()); sys.exit(subprocess.call(c,stdin=subprocess.DEVNULL))"


def run_diagnostic(command, output, *, timeout_seconds, cwd=None):
    if os.name != "nt":
        raise OSError("This diagnostic containment runner requires Windows Job Objects")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("A finite positive timeout is required")
    if not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("An explicit command argument list is required")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)  # Never overwrite earlier evidence.
    report_path = root / "result.json"
    report = {"status": "running", "timeout_seconds": timeout_seconds,
              "exit_code": None, "cleanup": "pending"}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    started = time.monotonic()
    process = None
    job = None
    try:
        job = _Job()
        # Files avoid pipe backpressure and unbounded capture_output RAM use.
        with (root / "stdout.log").open("wb") as out, (root / "stderr.log").open("wb") as err:
            process = subprocess.Popen([sys.executable, "-c", _BOOTSTRAP], cwd=cwd,
                                       stdin=subprocess.PIPE, stdout=out, stderr=err,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
            process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
            process.stdin.close()
            try:
                report["exit_code"] = process.wait(timeout=max(.001, timeout_seconds - (time.monotonic() - started)))
                report["status"] = "completed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                report["status"] = "timed_out"
    except BaseException as exc:
        report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "runner_error"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            if job is not None:
                job.close()  # Includes leftover children after a successful root exit.
            if process is not None:
                if process.poll() is None:
                    process.kill()  # Also covers failed job assignment, before release.
                process.wait(timeout=10)
            report["cleanup"] = "job_closed"
        except BaseException:
            report["cleanup"] = "failed"
            raise
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    result = run_diagnostic(command, args.output, timeout_seconds=args.timeout)
    print(json.dumps(result))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
