"""Whether a process is still running, so the Activity page can drop the notes of one that was killed."""

from __future__ import annotations

import os
import sys

_STILL_ACTIVE = 259  # GetExitCodeProcess's code for a process that has not exited
_QUERY_LIMITED_INFORMATION = 0x1000
_ACCESS_DENIED = 5


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)  # signal 0 only checks; on Windows os.kill would end the process instead
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but running
    return True


def _alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return kernel32.GetLastError() == _ACCESS_DENIED  # running, just not ours to open
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
