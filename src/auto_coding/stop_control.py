"""Controller-owned cooperative stop evidence shared by desktop and CLI runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid


def _read(path):
    if path.is_symlink() or path.resolve().parent != path.parent or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Unsafe stop evidence path")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid stop evidence")
    return value


def identity(logs, state=None):
    logs = Path(logs).resolve(strict=True)
    state = _read(logs / "state.json") if state is None else state
    manifest = _read(logs / "run-manifest.json")
    repo = Path(state["repo"]).resolve(strict=True)
    if logs == repo or logs.is_relative_to(repo):
        raise ValueError("Stop storage must be outside the target checkout")
    if (not isinstance(state.get("run_id"), str) or not state["run_id"]
            or manifest.get("run_id") != state["run_id"]
            or manifest.get("target_repo") != str(repo)
            or manifest.get("plan_sha256") != state.get("plan_sha256")):
        raise ValueError("Stop run identity differs from the manifest")
    generation = state.get("stop_generation", 0)
    if type(generation) is not int or generation < 0:
        raise ValueError("Invalid stop generation")
    return {"run_id": state["run_id"], "generation": generation,
            "logs": str(logs), "repo": str(repo), "plan_sha256": state["plan_sha256"]}


def pending(logs, state=None):
    binding = identity(logs, state)
    path = Path(binding["logs"]) / f"stop-request-{binding['generation']:08d}.json"
    try:
        record = _read(path)
    except FileNotFoundError:
        return None
    if record != binding:
        raise ValueError("Stop request belongs to another run")
    return record


def request(logs, expected):
    """Persist an idempotent request, never accepting a client-supplied path."""
    binding = identity(logs)
    if expected != binding:
        raise ValueError("Selected active run changed; refresh before requesting stop")
    state = _read(Path(binding["logs"]) / "state.json")
    if state.get("status") == "completed":
        raise ValueError("Completed runs cannot be stopped")
    if pending(logs):
        return binding
    root = Path(binding["logs"])
    target = root / f"stop-request-{binding['generation']:08d}.json"
    temporary = root / (".stop-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(binding, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Both operations publish atomically without overwriting existing evidence.
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    if pending(logs) != binding:
        raise ValueError("Stop request could not be verified")
    return binding
