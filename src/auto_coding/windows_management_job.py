"""Windows-only process accounting for the sealed Sandbox-management commands.

No breakaway is allowed. Unlike an untrusted test Job, this Job must not kill a
shared Microsoft broker when its last controller handle closes. Only authenticated
broker survivors may qualify as management quiescence; other survivors fail closed.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import time

from auto_coding.windows_job import WindowsJob, _kernel, _ExtendedLimits, _error
from auto_coding.trusted_sandbox_management import authenticated_broker, management_creation_flags


class _Ids(ctypes.Structure):
    _fields_ = [('assigned', wintypes.DWORD), ('listed', wintypes.DWORD),
                ('ids', ctypes.c_size_t * 512)]


_kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel.OpenProcess.restype = wintypes.HANDLE
_kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
_kernel.GetProcessTimes.restype = wintypes.BOOL
_kernel.GetPackageFullName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
_kernel.GetPackageFullName.restype = wintypes.LONG
_kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_kernel.IsProcessInJob.restype = wintypes.BOOL
_kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel.GetExitCodeProcess.restype = wintypes.BOOL
_kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel.TerminateProcess.restype = wintypes.BOOL
_kernel.GetCurrentProcess.restype = wintypes.HANDLE


class ManagementJob(WindowsJob):
    def creation_flags(self):
        contained = wintypes.BOOL()
        if not _kernel.IsProcessInJob(_kernel.GetCurrentProcess(), None, ctypes.byref(contained)):
            raise _error()
        limits = _ExtendedLimits()
        if contained.value and not _kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
            raise _error()
        self.ancestor_flags = limits.BasicLimitInformation.LimitFlags
        return management_creation_flags(bool(contained.value), self.ancestor_flags)

    def __init__(self, package):
        super().__init__()
        # Clear only this new management Job's kill-on-close, before assignment.
        # Zero flags also forbid both explicit and silent process breakaway.
        limits = _ExtendedLimits()
        if not _kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise _error()
        self.package = package
        self.processes = {}
        self.process_handles = {}

    def members(self):
        values = _Ids()
        if (not _kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(values), ctypes.sizeof(values), None)
                or values.assigned != values.listed or values.listed > 512):
            raise OSError('Incomplete management Job membership')
        return [int(pid) for pid in values.ids[:values.listed]]

    def identity(self, pid):
        handle = _kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            # A process may have exited between Job enumeration and OpenProcess.
            if pid not in self.members(): return None
            raise _error()
        retained = False
        try:
            contained = wintypes.BOOL()
            if not _kernel.IsProcessInJob(handle, self.handle, ctypes.byref(contained)) or not contained.value:
                raise OSError('Process identity is not owned by the management Job')
            image = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(image))
            if not _kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                raise _error()
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                raise _error()
            length = wintypes.UINT()
            code = _kernel.GetPackageFullName(handle, ctypes.byref(length), None)
            package = None
            if code == 122:
                name = ctypes.create_unicode_buffer(length.value)
                if _kernel.GetPackageFullName(handle, ctypes.byref(length), name) != 0: raise _error()
                package = name.value
            elif code != 15700:  # APPMODEL_ERROR_NO_PACKAGE for direct package executables.
                raise OSError('Cannot establish process package identity')
            value = {'pid': pid, 'image': image.value,
                     'createdFileTime': (created.dwHighDateTime << 32) | created.dwLowDateTime,
                     'packageFullName': package, 'jobMembershipVerified': True}
            self.processes[(pid, value['createdFileTime'])] = value
            key = (pid, value['createdFileTime'])
            if key not in self.process_handles:
                self.process_handles[key] = handle
                retained = True
            return value
        finally:
            if not retained: _kernel.CloseHandle(handle)

    def close(self):
        for handle in getattr(self, 'process_handles', {}).values(): _kernel.CloseHandle(handle)
        self.process_handles = {}
        super().close()

    def broker(self, value):
        if value is None: return False
        valid = authenticated_broker(value, self.package)
        if valid:
            value.update(imageSha256=self.package['brokerSha256'], authentication='signed-msix-payload+owned-job')
        return valid

    def survivors(self, root_pid):
        values = [self.identity(pid) for pid in self.members() if pid != root_pid]
        values = [value for value in values if value is not None]
        if any(not self.broker(value) for value in values):
            return None
        return values

    def cancel_transients(self, process):
        # Kill exact owned command handles, never the shared broker or a whole Job.
        if process.poll() is None: process.kill()
        process.wait(timeout=5)
        for pid in self.members():
            value = self.identity(pid)
            if value is None or self.broker(value): continue
            if str(Path(value['image']).resolve()).casefold() != str(Path(self.package['cli']).resolve()).casefold():
                return False
            handle = _kernel.OpenProcess(0x0001 | 0x1000, False, pid)
            if not handle: return False
            try:
                contained = wintypes.BOOL()
                if not _kernel.IsProcessInJob(handle, self.handle, ctypes.byref(contained)) or not contained.value:
                    return False
                # Holding this handle prevents terminating a recycled PID.
                if not _kernel.TerminateProcess(handle, 124): return False
            finally: _kernel.CloseHandle(handle)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None and self.survivors(process.pid) is not None: return True
            time.sleep(.02)
        return False
