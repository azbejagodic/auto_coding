"""Append-only timeline records for the Auto-Coding runner.

Events are UTF-8 JSONL in the external log directory. Each record has schema_version,
run_id, per-run seq, UTC timestamp, actor, event, outcome, and optional task/evidence
metadata. Consumers can stream ``logs/events.jsonl`` without starting Codex.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import os
import tempfile


class EventLogError(OSError):
    """The timeline could not be safely read or durably appended."""


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.seq = self._recover()

    def _recover(self) -> int:
        if not self.path.exists():
            return 0
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            # Preserve each torn tail under a unique name. Never overwrite earlier
            # crash evidence. Replace the timeline atomically after preserving it.
            cut = data.rfind(b"\n") + 1
            tail = data[cut:]
            if tail:
                index = 1
                while True:
                    sidecar = self.path.with_name(f"events.incomplete-{index:04d}")
                    if not sidecar.exists():
                        break
                    index += 1
                with sidecar.open("xb") as stream:
                    stream.write(tail)
                    stream.flush()
                    os.fsync(stream.fileno())
            data = data[:cut]
            # A crash may leave a prior recovery temporary behind. Use a unique
            # same-directory file so that stale crash evidence cannot block the
            # next recovery attempt.
            fd, temporary_name = tempfile.mkstemp(prefix=self.path.name + ".recovery-",
                                                   dir=self.path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except OSError:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        seq = 0
        for line in data.splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("run_id") != self.run_id:
                    raise ValueError("run id mismatch")
                value = event.get("seq")
                if not isinstance(value, int) or value != seq + 1:
                    raise ValueError("sequence gap")
                seq = value
            except (ValueError, TypeError) as exc:
                raise EventLogError(f"Invalid prior event timeline: {exc}") from exc
        return seq

    def emit(self, actor: str, event: str, outcome: str, *, task_id=None,
             attempt=None, evidence=None, metadata=None) -> dict:
        record = {"schema_version": 1, "run_id": self.run_id,
                  "seq": self.seq + 1,
                  "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "task_id": task_id, "attempt": attempt, "actor": actor,
                  "event": event, "outcome": outcome}
        if evidence:
            record["evidence"] = evidence
        if metadata:
            record["metadata"] = metadata
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise EventLogError(f"Could not persist event timeline: {exc}") from exc
        self.seq += 1
        return record
