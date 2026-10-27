"""Bounded, read-only export of one flat runner log directory. No GUI or processes."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_ENTRIES = 500
MAX_RECORDS = 500
METADATA_BUDGET = 2 * 1024 * 1024
FIXED = {"state.json", "run-manifest.json", "desktop-run.json", "events.jsonl",
         "review-schema.json", "runner-output.txt"}
EVIDENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}-[0-9]{2,}-(?:[ab]-(?:prompt\.txt|answer\.txt|events\.jsonl|verdict\.json)|diff\.txt|test-[0-9]+\.txt)\Z")
WARNING = ("Inspect before sharing. Identifiers and filenames can be private. The standard report "
           "selects limited structured fields; it does not promise perfect redaction of arbitrary text. "
           "Raw logs can contain private data/API keys in Codex prompts, review text and subprocess output.")


def _regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400)


def _identity(info):
    # Windows path stat and descriptor stat can disagree on deprecated st_ctime
    # (creation vs change time). Size, inode and nanosecond mtime are consistent.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name != "nt" else None)


def _root_identity(root):
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Selected run redirects elsewhere or is unavailable")
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Linked run directories are forbidden")
    return info.st_dev, info.st_ino


def _read(root, name, root_id):
    """Do not publish bytes unless both descriptor and path remain the same regular file.

    O_NOFOLLOW/O_NONBLOCK also prevent link/FIFO races on platforms providing them.
    Appending/replacing writers cause omission; this is not a cross-file snapshot.
    """
    if _root_identity(root) != root_id:
        raise ValueError("Run directory changed during export")
    path = root / name
    before = path.lstat()
    if not _regular(before) or path.resolve(strict=True) != path:
        raise ValueError("Nonregular, linked or redirected file")
    if before.st_size > MAX_FILE:
        raise ValueError("Individual file size limit exceeded")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _regular(opened) or _identity(before) != _identity(opened):
            raise ValueError("File changed before reading")
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > MAX_FILE or _identity(before) != _identity(after)
            or _identity(before) != _identity(path.lstat())
            or path.resolve(strict=True) != path or _root_identity(root) != root_id):
        raise ValueError("File changed during reading; retry when run is stopped")
    return data


def _identifier(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        if not re.search(r"(?:^sk-|token|secret|password|api.key)", value, re.I):
            return value
    return None


def _time(value):
    if isinstance(value, str) and len(value) <= 40:
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return None


ENUMS = {
    "status": {"stopped", "completed", "running", "checkpointing", "launching"},
    "decision": {"APPROVED", "REJECTED", "approved", "rejected"},
    "actor": {"controller", "Codex A", "Codex B", "tests", "test", "Git"},
    "outcome": {"started", "success", "failed", "retry", "pending", "approved", "rejected", "observed", "timed_out"},
    "event": {"run_started", "run_resumed", "run_completed", "stopped", "task_started",
              "test_started", "test_finished", "review_started", "review_finished",
              "developer_started", "developer_finished", "checkpoint_started", "checkpoint_finished",
              "correction_requested", "push_started", "push_finished", "resume_started",
              "resume_reconciled", "plan_validated", "process_started", "process_finished",
              "review_process_finished", "checkpoint_recovered", "checkpoint_committed",
              "checkpoint_stopped", "task_completed"},
}


def _fields(record):
    if not isinstance(record, dict):
        return {}
    result = {}
    for key, value in record.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
        elif key in {"run_id", "task", "task_id", "current_task"}:
            result[key] = _identifier(value)
        elif key in {"created_utc", "start_time_utc", "timestamp_utc", "updated_utc"}:
            result[key] = _time(value)
        elif key in {"seq", "attempt", "index", "exit_code", "a_exit", "duration_seconds", "task_count"}:
            if type(value) in {int, float} and -1e9 <= value <= 1e9:
                result[key] = value
        elif key == "timed_out" and type(value) is bool:
            result[key] = value
    return result


def _summary(name, data):
    if name == "events.jsonl":
        lines = data.splitlines()
        if len(lines) > MAX_RECORDS:
            raise ValueError("Timeline record limit exceeded")
        result = []
        for line in lines:
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Invalid timeline record")
                result.append({**_fields(record), "results": _fields(record.get("metadata"))})
        return result
    record = json.loads(data)
    if not isinstance(record, dict):
        raise ValueError("Expected structured object")
    result = _fields(record)
    for key in ("task_ids", "completed"):
        if isinstance(record.get(key), list):
            result[key] = [_identifier(v) for v in record[key][:MAX_RECORDS]]
            result[key + "_truncated"] = len(record[key]) > MAX_RECORDS
    if name == "state.json" and isinstance(record.get("history"), list):
        result["history"] = []
        for item in record["history"][:MAX_RECORDS]:
            if isinstance(item, dict):
                result["history"].append({**_fields(item), "review": _fields(item.get("review")),
                    "tests": [_fields(t) for t in item.get("tests", [])[:MAX_RECORDS]]
                    if isinstance(item.get("tests"), list) else []})
        result["history_truncated"] = len(record["history"]) > MAX_RECORDS
    return result


def validate_destination(destination, root, excluded_roots=()):
    destination = Path(destination).absolute()
    if destination.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip filename")
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValueError("Destination directory redirects elsewhere")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists. Choose a new ZIP filename; nothing was overwritten.")
    for folder in (Path(root).absolute(), *(Path(p).resolve() for p in excluded_roots if p)):
        if destination.is_relative_to(folder):
            raise ValueError("Export must be outside run logs and repository checkouts")
    for parent in destination.parents:
        if os.path.lexists(parent / ".git"):
            raise ValueError("Export must be outside every repository checkout")
    return destination


def export_debug(root, destination, mode="standard", *, excluded_roots=()):
    """Create a new ZIP without overwriting. Native confirmation belongs to the bridge.

    Flat runner filenames only: no recursion, evidence references, source tree walks,
    subprocesses, Git, or network. Limits apply to uncompressed bytes as well as ZIP size.
    """
    if mode not in {"standard", "raw"}:
        raise ValueError("Unknown export choice")
    root = Path(root).absolute()
    root_id = _root_identity(root)
    destination = validate_destination(destination, root, excluded_roots)
    report = {"format_version": 1, "mode": mode, "warning": WARNING,
              "exported_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "directory_label": _identifier(root.name), "inventory": [], "summaries": {},
              "limits": {"file_bytes": MAX_FILE, "total_bytes": MAX_TOTAL,
                         "entries": MAX_ENTRIES, "records_per_summary": MAX_RECORDS},
              "note": "Independent stable file reads, not a transactional run snapshot. Free-form fields omitted in standard mode. Lists limited to records_per_summary."}
    total = 0
    scanned = 0
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix="debug-export-", suffix=".tmp", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w+b") as output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with os.scandir(root) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES - 1:
                        raise ValueError("Directory entry limit exceeded; no ZIP created (maximum 499 run entries)")
                    name = entry.name
                    # Names only in inventory; never follow even a permitted-looking link.
                    row = {"name": name[:240], "action": "omitted"}
                    report["inventory"].append(row)
                    if name not in FIXED and not EVIDENCE.fullmatch(name):
                        row["reason"] = "Not an allowed flat runner filename (directories, hidden/sensitive files and source trees excluded)"
                        continue
                    try:
                        # Windows DirEntry's cached stat may omit the link count.
                        info = (root / name).lstat()
                        row["bytes"] = info.st_size
                        if not _regular(info):
                            raise ValueError("Nonregular, linked or redirected file")
                        if info.st_size > MAX_FILE:
                            raise ValueError("Individual file size limit exceeded")
                        structured = name in {"state.json", "run-manifest.json", "desktop-run.json", "events.jsonl"} or name.endswith("-b-verdict.json")
                        if mode == "standard" and not structured:
                            row["reason"] = "Raw content excluded from standard report"
                            continue
                        if total + info.st_size > MAX_TOTAL - METADATA_BUDGET:
                            raise ValueError("Total input/archive budget exceeded")
                        data = _read(root, name, root_id)
                        total += len(data)
                        if mode == "standard":
                            report["summaries"][name] = _summary(name, data)
                            row["action"] = "selected structured fields only"
                            row["reason"] = "Other fields and free-form payloads excluded; lists bounded"
                    except (OSError, ValueError, UnicodeError, RuntimeError, RecursionError) as exc:
                        # Decode errors may contain source bytes; never echo them.
                        detail = "Invalid text encoding" if isinstance(exc, UnicodeError) else str(exc)[:240]
                        row["reason"] = "Unreadable, unstable, invalid or over limit: " + type(exc).__name__ + ": " + detail
                        continue
                    if mode == "raw":
                        # ZIP write failures are fatal, unlike source-read omissions.
                        archive.writestr("raw/" + name, data)
                        row["action"] = "included raw"
            for name in ("state.json", "run-manifest.json", "events.jsonl"):
                if not any(row["name"] == name for row in report["inventory"]):
                    report["inventory"].append({"name": name, "action": "omitted", "reason": "Missing (legacy or incomplete run)"})
            # Directory names are labels, never evidence of the actual run identity.
            manifest = report["summaries"].get("run-manifest.json", {})
            state = report["summaries"].get("state.json", {})
            report["run_id"] = manifest.get("run_id") or state.get("run_id")
            report["status"] = state.get("status")
            if mode == "raw":
                report["metadata_note"] = "Run identity/status are in raw/run-manifest.json and raw/state.json when available; raw files are not interpreted."
            encoded = json.dumps(report, indent=2, allow_nan=False).encode("utf-8")
            if len(encoded) > METADATA_BUDGET:
                raise ValueError("Report metadata budget exceeded; no ZIP created")
            archive.writestr("debug-report.json", encoded)
            archive.close()
            output.flush()
            os.fsync(output.fileno())
            built = os.fstat(output.fileno())
        # Windows finalizes last-write timestamps on closing a writable handle.
        published_info = temporary.lstat()
        if not _regular(published_info) or _identity(published_info)[:3] != _identity(built)[:3]:
            raise ValueError("Temporary archive changed before publication")
        if built.st_size > MAX_TOTAL:
            raise ValueError("Archive size limit exceeded; no ZIP created")
        if _root_identity(root) != root_id:
            raise ValueError("Run directory changed during export; no ZIP created")
        # Atomic no-clobber publication. Unlike replace(), a concurrent destination
        # creation can never be overwritten. Temp and destination share a filesystem.
        validate_destination(destination, root, excluded_roots)
        os.link(temporary, destination)
        return {"ok": True, "path": str(destination), "mode": mode,
                "inventory": report["inventory"], "warning": WARNING,
                "contents": ["debug-report.json"] + (["raw/ (allowed stable runner files)"] if mode == "raw" else [])}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
