"""Read-only, bounded reader for Auto-Coding run logs."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MAX_JSON = 4 * 1024 * 1024
MAX_EVIDENCE = 1024 * 1024


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            return None
        return stamp.astimezone(timezone.utc)
    except ValueError:
        return None


def run_summary(data, manifest):
    """Derive display evidence only; this never authorizes launch or recovery."""
    state = data.get("state") or {}
    events = [e for e in data["events"] if e.get("actor") == "controller"
              and (not state.get("run_id") or e.get("run_id") == state["run_id"])]
    if state.get("run_id") and manifest.get("run_id") and state["run_id"] != manifest["run_id"]:
        data["event_errors"].append("Run manifest and state identities do not match.")
    ids = manifest.get("task_ids")
    completed = state.get("completed")
    valid_completed = (isinstance(completed, list)
                       and all(isinstance(i, str) and i for i in completed)
                       and len(set(completed)) == len(completed))
    current = state.get("current_task")
    if data["task_count"] is None:
        for event in events:
            metadata = event.get("metadata")
            count = metadata.get("task_count") if isinstance(metadata, dict) else None
            if event.get("event") == "plan_validated" and type(count) is int and count > 0:
                data["task_count"] = count
    position = None
    if ids is not None and valid_completed:
        if completed != ids[:len(completed)]:
            data["event_errors"].append("Completed task IDs do not match the run manifest.")
            valid_completed = False
        elif current in ids:
            position = ids.index(current) + 1
        elif current is None and completed == ids:
            position = len(ids)
    elif valid_completed and isinstance(current, str) and current:
        position = completed.index(current) + 1 if current in completed else len(completed) + 1
        if data["task_count"] is not None and position > data["task_count"]:
            position = None
            valid_completed = False
    start = _utc(manifest.get("start_time_utc")) or _utc(state.get("created_utc"))
    if start is None:
        start = next((_utc(e.get("timestamp_utc")) for e in events
                      if e.get("event") == "run_started" and _utc(e.get("timestamp_utc"))), None)
    lifecycle = [e for e in events if e.get("event") in (
        "run_started", "resume_started", "stopped", "run_completed",
        "final_verification_started", "final_verification_finished")]
    last = lifecycle[-1] if lifecycle else {}
    terminal = (last.get("event") in ("stopped", "run_completed")
                or last.get("event") == "final_verification_finished" and last.get("outcome") == "failed")
    end = _utc(last.get("timestamp_utc")) if terminal else None
    verification = state.get("final_verification_result")
    verification = verification if isinstance(verification, dict) else {}
    tests = verification.get("tests")
    verified = (verification.get("status") == "passed" and isinstance(tests, list) and bool(tests)
                and all(isinstance(t, dict) and t.get("exit_code") == 0
                        and t.get("timed_out") is False and t.get("cleanup_confirmed") is True
                        and isinstance(t.get("output"), str)
                        and evidence_status(data["root"], t["output"]) == "available" for t in tests))
    complete = (state.get("status") == "completed" and verified and ids is not None
                and completed == ids and not state.get("pending_checkpoint")
                and last.get("event") == "run_completed" and last.get("outcome") == "success"
                and not data["state_error"] and not data["event_errors"])
    metadata = last.get("metadata")
    error = (verification.get("outcome") if verification.get("status") == "failed" else
             metadata.get("reason") if last.get("event") == "stopped" and isinstance(metadata, dict) else None)
    return {"current": position, "completed": len(completed) if valid_completed else None,
            "completion_confirmed": complete, "verification_status": verification.get("status"),
            "error": error, "execution_open": bool(lifecycle) and not terminal,
            "start_utc": start.isoformat() if start else None,
            "duration_seconds": (end - start).total_seconds() if start and end and end >= start else None,
            "terminal_recorded": terminal}


def _json_file(path: Path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError("file exceeds 4 MB limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _confined_log_file(root: Path, name: str) -> Path:
    """Resolve a standard log filename without following links outside root."""
    base = root.resolve(strict=True)
    candidate = root / name
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError(f"{name} is not a regular file confined to the selected log directory")
    return resolved


def evidence_status(log_root: str | Path, relative_path: str) -> str:
    """Check evidence existence without opening or exposing its contents."""
    root = Path(log_root).expanduser().resolve(strict=False)
    try:
        rel = Path(relative_path)
        if not relative_path or rel.is_absolute() or ".." in rel.parts:
            return "unsafe path"
        resolved = root.joinpath(rel).resolve(strict=True)
        if not resolved.is_relative_to(root):
            return "unsafe path"
        if not resolved.is_file():
            return "missing"
        return "available"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"


def read_run(log_root: str | Path) -> dict:
    """Return a JSON-safe snapshot; malformed sources are reported as plain text."""
    root = Path(log_root).expanduser()
    manifest_value = {}
    result = {"root": str(root), "exists": root.is_dir(), "state": None,
              "state_error": None, "events": [], "event_errors": [], "empty": True,
              "files": [], "task_count": None}
    if not root.is_dir():
        result["state_error"] = "Log directory is missing or unavailable."
        return result
    try:
        manifest = root / "run-manifest.json"
        if manifest.exists() or manifest.is_symlink():
            value = _json_file(_confined_log_file(root, "run-manifest.json"))
            if (not isinstance(value, dict) or not isinstance(value.get("task_ids"), list)
                    or not all(isinstance(i, str) and i for i in value["task_ids"])
                    or len(set(value["task_ids"])) != len(value["task_ids"])):
                raise ValueError("manifest must contain task_ids")
            manifest_value = value
            result["task_count"] = len(value["task_ids"])
    except (OSError, UnicodeError, ValueError) as exc:
        result["event_errors"].append(f"run-manifest.json is corrupt or unreadable: {exc}")
    try:
        # Legacy runs have no event links. List names only, never read bodies
        # until the user selects a confined file. Runner evidence is flat.
        for path in root.iterdir():
            if len(result["files"]) >= 500:
                result["event_errors"].append("File list limited to 500 entries; event evidence links remain available.")
                break
            if path.is_file() or path.is_symlink():
                result["files"].append({"label": "File", "path": path.name,
                                        "status": evidence_status(root, path.name)})
        result["files"].sort(key=lambda item: item["path"])
    except OSError as exc:
        result["event_errors"].append(f"Could not list evidence files: {exc}")
    state_path = root / "state.json"
    if state_path.exists() or state_path.is_symlink():
        try:
            state = _json_file(_confined_log_file(root, "state.json"))
            if not isinstance(state, dict):
                raise ValueError("state must be a JSON object")
            result["state"] = state
        except (OSError, UnicodeError, ValueError) as exc:
            result["state_error"] = f"state.json is corrupt or unreadable: {exc}"
    timeline = root / "events.jsonl"
    if timeline.exists() or timeline.is_symlink():
        try:
            timeline = _confined_log_file(root, "events.jsonl")
            if timeline.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("events.jsonl exceeds 32 MB limit")
            with timeline.open("r", encoding="utf-8") as stream:
                for line_no, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("record must be a JSON object")
                        event["_source_order"] = line_no
                        evidence = event.get("evidence")
                        files = []
                        def collect(label, value):
                            if isinstance(value, str):
                                files.append((label, value))
                            elif isinstance(value, list):
                                for item in value:
                                    collect(label, item)
                        if isinstance(evidence, dict):
                            for key, value in evidence.items():
                                collect(key, value)
                        event["_evidence_status"] = [
                            {"label": key, "path": value,
                             "status": evidence_status(root, value)}
                            for key, value in files
                        ]
                        result["events"].append(event)
                    except (ValueError, TypeError) as exc:
                        result["event_errors"].append(f"Line {line_no}: corrupt event ({exc})")
            # Stable sort means duplicate/missing seq entries retain source ordering.
            result["events"].sort(key=lambda e: (e.get("seq") if isinstance(e.get("seq"), int) else 2**63,
                                                  e["_source_order"]))
        except (OSError, UnicodeError, ValueError) as exc:
            result["event_errors"].append(f"events.jsonl could not be read: {exc}")
    result["empty"] = not result["state"] and not result["events"] and not result["event_errors"] and not result["state_error"]
    result["summary"] = run_summary(result, manifest_value)
    return result


def read_evidence(log_root: str | Path, relative_path: str) -> dict:
    """Read a selected evidence file only when its resolved path stays under root."""
    root = Path(log_root).expanduser().resolve(strict=False)
    try:
        rel = Path(relative_path)
        if not relative_path or rel.is_absolute() or ".." in rel.parts:
            return {"ok": False, "text": "Unsafe evidence path rejected."}
        candidate = root.joinpath(rel)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return {"ok": False, "text": "Evidence path escapes the selected log directory."}
        if not resolved.is_file():
            return {"ok": False, "text": "Evidence file is missing or is not a regular file."}
        size = resolved.stat().st_size
        if size > MAX_EVIDENCE:
            return {"ok": False, "text": "Evidence file exceeds the 1 MB display limit."}
        return {"ok": True, "text": resolved.read_text(encoding="utf-8", errors="replace")}
    except FileNotFoundError:
        return {"ok": False, "text": "Evidence file is missing."}
    except OSError as exc:
        return {"ok": False, "text": f"Evidence could not be read: {exc}"}
