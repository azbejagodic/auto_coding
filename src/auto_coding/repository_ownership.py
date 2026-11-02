"""Paths and durable uncertainty for the existing repository ownership gates."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re


def managed_lock(repo, suffix=".lock"):
    repo = Path(repo).resolve() if repo is not None else None
    if repo and repo.parent.name == "workspaces" and re.fullmatch(r"[0-9a-f]{64}", repo.name):
        return repo.parent / (repo.name + suffix)
    return None


def execution_lock(repo, suffix=".lock"):
    """Keep managed paths; use stable sibling gates for other checkout paths.

    Sibling paths remain stable before/after --create-repo initializes Git and
    never dirty the target. resolve/normcase unify aliases on Windows.
    """
    if repo is None:
        return None
    repo = Path(repo).resolve()
    managed = managed_lock(repo, suffix)
    if managed is not None:
        return managed
    identity = hashlib.sha256(os.path.normcase(str(repo)).encode("utf-8")).hexdigest()
    return repo.parent / (".auto-coding-" + identity + suffix)


def assert_process_safe(repo):
    record = execution_lock(repo, ".process-uncertain.json")
    if record is None:
        return
    try:
        record.lstat()
    except FileNotFoundError:
        return
    raise OSError(f"Process cleanup remains unverified; inspect {record}. "
                  "Missing PIDs, missing locks or elapsed time are not cleanup evidence.")


def preserve_process_uncertainty(repo, logs):
    """Do not overwrite or acknowledge an existing uncertainty record."""
    record = execution_lock(repo, ".process-uncertain.json")
    try:
        stream = record.open("x", encoding="utf-8")
    except FileExistsError:
        return
    with stream:
        json.dump({"repo": str(Path(repo).resolve()), "logs": str(logs),
                   "cleanup_confirmed": False}, stream)
        stream.flush()
        os.fsync(stream.fileno())
