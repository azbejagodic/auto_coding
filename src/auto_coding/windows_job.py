"""Own a Windows subprocess tree before its first thread can run."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


_kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel.CreateJobObjectW.restype = wintypes.HANDLE
_kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel.SetInformationJobObject.restype = wintypes.BOOL
_kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel.TerminateJobObject.restype = wintypes.BOOL
_kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p]
_kernel.QueryInformationJobObject.restype = wintypes.BOOL
_kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_kernel.Thread32First.restype = wintypes.BOOL
_kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_kernel.Thread32Next.restype = wintypes.BOOL
_kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel.OpenThread.restype = wintypes.HANDLE
_kernel.ResumeThread.argtypes = [wintypes.HANDLE]
_kernel.ResumeThread.restype = wintypes.DWORD
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL


def _error():
    return OSError(ctypes.get_last_error(), "Windows process ownership failed")


class WindowsJob:
    def __init__(self):
        self.assigned = False
        self.handle = _kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise _error()
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not _kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = _error()
            self.close()
            raise error

    def assign_and_resume(self, process):
        if not _kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process._handle)):
            raise _error()
        self.assigned = True
        snapshot = _kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise _error()
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = bool(_kernel.Thread32First(snapshot, ctypes.byref(entry)))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = _kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise _error()
                    try:
                        # A newly created suspended process has one thread with
                        # suspend count one. Any other state is left uncertain.
                        if _kernel.ResumeThread(thread) != 1:
                            raise _error()
                    finally:
                        _kernel.CloseHandle(thread)
                    return
                entry.dwSize = ctypes.sizeof(entry)
                found = bool(_kernel.Thread32Next(snapshot, ctypes.byref(entry)))
            raise OSError("Suspended process thread could not be identified")
        finally:
            _kernel.CloseHandle(snapshot)

    def active(self):
        info = _Accounting()
        if not _kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise _error()
        return info.ActiveProcesses

    def terminate_and_confirm(self, timeout=5):
        # The command's return value is insufficient; query the owned job until
        # all associated processes have actually left it.
        try:
            if self.active() and not _kernel.TerminateJobObject(self.handle, 1):
                return False
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.active() == 0:
                    return True
                time.sleep(0.02)
            return self.active() == 0
        except OSError:
            return False

    def close(self):
        if self.handle:
            _kernel.CloseHandle(self.handle)
            self.handle = None
