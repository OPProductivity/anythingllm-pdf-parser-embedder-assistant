"""Small cross-process mutexes for rare, integrity-sensitive local writes."""

from __future__ import annotations

import ctypes
import hashlib
import os
from contextlib import contextmanager


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


def mutex_name(namespace: str, identity: str) -> str:
    """Return a private, path-specific Windows mutex name without leaking paths."""
    digest = hashlib.sha256(f"{namespace}\0{identity}".encode("utf-8", "surrogatepass")).hexdigest()
    return f"Local\\AnythingLLMPdfAssistant-{namespace}-{digest}"


@contextmanager
def named_process_lock(namespace: str, identity: str, *, timeout_seconds: float = 5.0):
    """Serialize a short write across assistant processes on Windows.

    The portable assistant is Windows-first.  Other platforms retain the
    caller's in-process lock, which preserves test and developer portability
    without pretending a Windows mutex exists there.
    """
    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateMutexW(None, False, mutex_name(namespace, identity))
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    acquired = False
    try:
        waited = kernel32.WaitForSingleObject(handle, max(0, int(timeout_seconds * 1000)))
        if waited not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            if waited == WAIT_TIMEOUT:
                raise TimeoutError(f"Timed out waiting for the {namespace} write lock.")
            raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed for {namespace}")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
