"""Background runner spawning for validated, serialized launch callers."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys

from auto_coding.github_workspace import reserve_job, cancel_reservation
from auto_coding.run_lifecycle import write_launch_metadata


@dataclass
class LaunchState:
    """Mutable in-memory launch status; compatible caller objects can be used too."""

    process: object | None = None
    exit_code: int | None = None
    error: str | None = None
    _launch_uncertain: bool = False
    process_history: list = field(default_factory=list)


def launch_runner(state, *, repo, plan, logs, attempts, resume=False, publication=None,
                  popen=None, persist_metadata=None, reserve=None, cancel=None, fail=None) -> dict:
    """Reserve, persist intent, spawn, then persist PID without hiding live children.

    The caller owns preflight, existing-run checks and checkout serialization
    (e.g. checkout_lock for managed repositories), and supplies an existing logs
    directory. publication is the caller's already validated/locked auto-push
    configuration. State is updated as spawning progresses, including on failure;
    callbacks retain the controller's existing test and metadata hooks.
    """
    spawn = popen if popen is not None else subprocess.Popen
    reserve = reserve if reserve is not None else reserve_job
    cancel = cancel if cancel is not None else cancel_reservation
    if persist_metadata is None:
        def persist_metadata(logs, pid=None, status="stopped"):
            write_launch_metadata(logs, repo=repo, plan=plan, pid=pid, status=status,
                                  process_history=state.process_history)
    logs = Path(logs)
    src = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    # A reservation belongs only to this launch, never to a parent runner.
    env.pop("AUTOCODING_JOB_RESERVATION", None)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, "-B", "-m", "auto_coding.simple_run", "--repo", str(repo),
            "--logs", str(logs), "--attempts", str(attempts)]
    if resume:
        argv.append("--resume")
    if publication:
        argv.extend(["--auto-push-repository", publication["repository"],
                     "--auto-push-branch", publication["branch"]])
    argv.append(str(plan))
    reservation = None
    spawned = False
    try:
        reservation = reserve(repo)
        if reservation is not None:
            env["AUTOCODING_JOB_RESERVATION"] = reservation
        # Intent must survive a successful spawn followed by a PID-write failure.
        persist_metadata(logs, status="launching")
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        output_path = logs / "runner-output.txt"
        if not output_path.resolve().is_relative_to(logs.resolve()):
            raise OSError("Runner output path escapes the selected logs directory.")
        with output_path.open("ab") as output:
            state.process = spawn(argv, cwd=src, env=env, shell=False,
                                  stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT, **options)
            spawned = True
        state.exit_code = None
        state.error = None
        try:
            persist_metadata(logs, pid=getattr(state.process, "pid", None), status="running")
            state._launch_uncertain = False
        except OSError as exc:
            state._launch_uncertain = True
            state.error = f"Runner is active (PID {getattr(state.process, 'pid', 'unknown')}), but its PID could not be saved: {exc}. Keep this window open and do not resume this run."
        return {"ok": True, "argv": argv, "error": state.error}
    except OSError as exc:
        if not spawned:
            cancel(repo, reservation)
        if state.process is None and logs is not None:
            try:
                persist_metadata(logs, status="stopped")
            except OSError:
                # Keep unresolved launch intent; never risk a duplicate child.
                state._launch_uncertain = True
        message = f"Could not start the local runner: {type(exc).__name__}: {exc}"
        if fail is not None:
            return fail(message)
        state.error = message
        return {"ok": False, "error": message}
