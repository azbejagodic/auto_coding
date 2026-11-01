"""Read-only repository activity checks, independent of desktop controllers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat


def _run_records(root: Path) -> list[Path]:
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        try:
            root.lstat()
        except FileNotFoundError:
            return []  # Run storage need not have been created yet.
        raise OSError("Run storage cannot be inspected")  # e.g. a dangling link.
    records = []
    with entries:
        for entry in entries:
            if not entry.name.startswith("run-"):
                continue
            if not stat.S_ISDIR(entry.stat().st_mode):
                continue
            # Explicit enumeration lets permission errors and disappearing
            # directories fail closed instead of being suppressed by glob().
            with os.scandir(entry.path) as files:
                for item in files:
                    if item.name == "desktop-run.json":
                        records.append(Path(item.path))
                        break
    return records


def repository_busy(repo, runs_dir, *, selected_repo=None, selected_logs=None,
                    selected_activity=None) -> bool:
    """Check saved runs plus optional live context supplied by a caller.

    selected_activity receives the resolved target repository and must return
    False to establish inactivity. No PID is used to dismiss active metadata:
    a missing parent may have left descendants. This function never clears locks.
    """
    try:
        repo = Path(repo).resolve()
        logs = Path(selected_logs) if selected_logs is not None else None
        if selected_repo is not None and Path(selected_repo).resolve() == repo:
            if selected_activity is None or selected_activity(repo) is not False:
                return True
        discovered = _run_records(Path(runs_dir))
        paths = list(discovered)
        if logs is not None and logs / "desktop-run.json" not in paths:
            paths.append(logs / "desktop-run.json")
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                if logs is not None and path.parent == logs and path not in discovered:
                    # Selected legacy logs may legitimately have no launch
                    # metadata. A missing directory or vanished observed record
                    # cannot establish inactivity.
                    if logs.is_dir():
                        continue
                return True
            if not isinstance(item, dict):
                return True
            if "repo" in item and item["repo"] is not None and not isinstance(item["repo"], str):
                return True
            if item.get("repo") and Path(item["repo"]).resolve() != repo:
                continue
            if item.get("status") not in {"stopped", "completed"}:
                return True
        return False
    except Exception:
        # Filesystem, metadata and live-context failures cannot prove quiescence.
        return True
