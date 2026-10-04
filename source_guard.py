"""Keep a Windows source revision stable throughout preparation."""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path


@contextmanager
def locked_pdf_source(path):
    canonical = Path(path).resolve(strict=True)
    if os.name != "nt":
        # POSIX flock is advisory; preserve non-Windows development support,
        # without claiming Windows' mandatory sharing guarantee there.
        import fcntl
        with canonical.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            try:
                yield canonical
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    # Shared readers are allowed, but writes/replacement/deletion are denied
    # until all extraction and canonical identity checks have completed.
    handle = kernel.CreateFileW(str(canonical), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield canonical
    finally:
        kernel.CloseHandle(handle)
