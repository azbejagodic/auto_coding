"""Read-only, bounded reader for Auto-Coding run logs."""
from __future__ import annotations

import json
from pathlib import Path

MAX_JSON = 4 * 1024 * 1024
MAX_EVIDENCE = 1024 * 1024


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
    result = {"root": str(root), "exists": root.is_dir(), "state": None,
              "state_error": None, "events": [], "event_errors": [], "empty": True}
    if not root.is_dir():
        result["state_error"] = "Log directory is missing or unavailable."
        return result
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
