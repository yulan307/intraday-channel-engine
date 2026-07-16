from __future__ import annotations

import ctypes
import hashlib
from contextlib import AbstractContextManager
from pathlib import Path

from ..domain.errors import LiveProcessAlreadyRunningError, PersistenceError

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_INFINITE = 0xFFFFFFFF


def symbol_mutex_name(environment: str, host: str, port: int, symbol: str) -> str:
    value = f"{environment}|{host}|{port}|{symbol.strip().upper()}"
    return "Local\\IntradayChannelEngineSymbol_" + hashlib.sha256(value.encode()).hexdigest()


def merge_mutex_name(master_database: str | Path) -> str:
    value = str(Path(master_database).resolve()).lower()
    return "Local\\IntradayChannelEngineMerge_" + hashlib.sha256(value.encode()).hexdigest()


class WindowsNamedMutex(AbstractContextManager["WindowsNamedMutex"]):
    """Process-wide Windows mutex.  Symbol locks reject duplicates; merge locks wait."""
    def __init__(self, name: str, *, wait: bool, purpose: str) -> None:
        self.name, self.wait, self.purpose, self.handle = name, wait, purpose, None

    def acquire(self) -> "WindowsNamedMutex":
        if not hasattr(ctypes, "windll"):
            raise PersistenceError("Phase 8 named mutexes require Windows")
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise PersistenceError(f"Unable to create {self.purpose} mutex")
        self.handle = handle
        exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        if exists and not self.wait:
            kernel32.CloseHandle(handle)
            self.handle = None
            raise LiveProcessAlreadyRunningError(f"A Live process for this symbol is already running ({self.purpose})")
        result = kernel32.WaitForSingleObject(handle, _INFINITE if self.wait else 0)
        if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            kernel32.CloseHandle(handle)
            self.handle = None
            raise PersistenceError(f"Unable to acquire {self.purpose} mutex")
        return self

    def release(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "WindowsNamedMutex":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
