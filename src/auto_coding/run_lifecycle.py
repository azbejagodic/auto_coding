"""PID queries and launch metadata, independent of desktop presentation.

PID absence does not establish process-tree cleanup or authorize lock removal.
Repository-wide activity policy belongs to repository_activity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def valid_pid(pid) -> bool:
    # bool is an int in Python, but never a real process identifier.
    return type(pid) is int and pid > 0


def pid_active(pid) -> bool:
    """Query a valid PID; treat inaccessible process status as potentially active."""
    if not valid_pid(pid):
        return False  # Missing/invalid launch PIDs require a separate unknown-state gate.
    try:
        if os.name == "nt":
            # os.kill(pid, 0) uses TerminateProcess on Windows. Query only.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID absent
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # Permission/query failures cannot establish inactivity.


def _metadata_text(logs: Path) -> str:
    """Preserve the confined, bounded evidence reader's metadata diagnostics."""
    root = logs.expanduser().resolve(strict=False)
    try:
        resolved = (root / "desktop-run.json").resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("Evidence path escapes the selected log directory.")
        if not resolved.is_file():
            raise ValueError("Evidence file is missing or is not a regular file.")
        if resolved.stat().st_size > 1024 * 1024:
            raise ValueError("Evidence file exceeds the 1 MB display limit.")
        return resolved.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise ValueError("Evidence file is missing.") from None
    except OSError as exc:
        raise ValueError(f"Evidence could not be read: {exc}") from exc


def read_launch_metadata(logs) -> tuple[dict | None, str | None]:
    """Return metadata and a diagnostic; absent legacy metadata is not an error."""
    if logs is None:
        return None, None
    try:
        logs = Path(logs)
        path = logs / "desktop-run.json"
        try:
            path.lstat()
        except FileNotFoundError:
            return None, None
        value = json.loads(_metadata_text(logs))
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return value, None
    except (OSError, ValueError) as exc:
        return None, f"desktop-run.json is corrupt or unreadable: {exc}"


def write_launch_metadata(logs, *, repo, plan, pid=None, status="stopped", process_history=()):
    """Atomically replace launch metadata; preserve the existing on-disk format."""
    logs = Path(logs)
    value = {"repo": str(repo), "plan": str(plan), "logs": str(logs)}
    if process_history:
        value["process_history"] = process_history
    if pid is not None:
        value["pid"] = pid
    value["status"] = status
    temp = logs / "desktop-run.json.tmp"
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(logs / "desktop-run.json")
