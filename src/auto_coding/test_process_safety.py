"""Persistent test-process gates, including for unmanaged Git repositories.

Only the executing caller can remove its gate after observing verified cleanup.
There is deliberately no PID-, time-, or missing-lock-based recovery operation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid


class TestProcessSafetyError(OSError):
    pass


def test_gate_path(repo) -> Path | None:
    if repo is None:
        return None
    repo = Path(repo).resolve()
    dotgit = repo / ".git"
    try:
        mode = dotgit.stat().st_mode
    except FileNotFoundError:
        try:
            dotgit.lstat()
        except FileNotFoundError:
            return None  # The normal repository validator handles new/non-Git targets.
        raise TestProcessSafetyError("Git metadata is unavailable; test-process safety cannot be checked.")
    if stat.S_ISDIR(mode):
        directory = dotgit.resolve()
    elif stat.S_ISREG(mode):
        with dotgit.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or not raw.startswith(b"gitdir: "):
            raise TestProcessSafetyError("Git directory pointer is ambiguous; inspect before execution.")
        target = raw[8:].decode("utf-8").strip()
        if not target or "\n" in target or "\r" in target:
            raise TestProcessSafetyError("Git directory pointer is ambiguous; inspect before execution.")
        directory = (repo / target).resolve(strict=True)
        if not directory.is_dir():
            raise TestProcessSafetyError("Git directory pointer does not identify a directory.")
    else:
        raise TestProcessSafetyError("Git metadata is ambiguous; inspect before execution.")
    return directory / "auto-coding-test-process.lock"


def assert_test_process_safe(repo) -> None:
    path = test_gate_path(repo)
    if path is None:
        return
    for record in (path.parent / "auto-coding-test-process-uncertain.json", path):
        try:
            record.lstat()
        except FileNotFoundError:
            continue
        raise TestProcessSafetyError(
            f"Unresolved test-process cleanup blocks execution and publication. Inspect {record}; "
            "an absent PID, elapsed time or a missing workspace lock is not cleanup evidence.")


class TestProcessGate:
    def __init__(self, repo, context):
        assert_test_process_safe(repo)
        self.path = test_gate_path(repo)
        if self.path is None:
            raise TestProcessSafetyError("A Git repository is required to protect test execution.")
        self.context = context
        self.token = uuid.uuid4().hex
        try:
            self.path.mkdir()
        except FileExistsError:
            raise TestProcessSafetyError(f"Unresolved test-process cleanup: inspect {self.path}.") from None
        opened = False
        try:
            stream = (self.path / "execution.json").open("x", encoding="utf-8")
            opened = True
            with stream:
                json.dump(context | {"token": self.token, "cleanup_confirmed": None}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # This caller created the gate and has not invoked a test yet.
            try:
                if opened:
                    (self.path / "execution.json").unlink()
                self.path.rmdir()
            except OSError:
                pass  # A remaining gate must be inspected, never automatically expired.
            raise

    def keep_uncertain(self):
        # Keep durable evidence independent of the execution/workspace lock.
        # Even losing those locks must not authorize a fresh run with other logs.
        path = self.path.parent / "auto-coding-test-process-uncertain.json"
        try:
            stream = path.open("x", encoding="utf-8")
        except FileExistsError:
            return  # Existing uncertainty is never overwritten or acknowledged here.
        with stream:
            json.dump(self.context | {"cleanup_confirmed": False}, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def finish_confirmed(self):
        record = self.path / "execution.json"
        if record.is_symlink():
            raise TestProcessSafetyError("Test-process gate ownership changed; inspect before retrying.")
        value = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("token") != self.token:
            raise TestProcessSafetyError("Test-process gate ownership changed; inspect before retrying.")
        record.unlink()
        self.path.rmdir()
