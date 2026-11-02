"""Read-only Git and run-storage checks shared by launch callers."""
from __future__ import annotations

from pathlib import Path
import subprocess

from auto_coding.repository_ownership import execution_lock, assert_process_safe
from auto_coding.process_runner import bounded_run
from auto_coding.repository_activity import repository_busy
from auto_coding.test_process_safety import assert_test_process_safe


def validate_repository_root(path, *, run_command=None) -> Path:
    """Return an existing Git root, without changing it or creating storage."""
    run = run_command if run_command is not None else bounded_run
    repo = Path(path).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise ValueError("Target must be a directory.")
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True,
              text=True, encoding="utf-8", errors="surrogateescape", shell=False, timeout=10)
    if top.returncode or Path(top.stdout.strip()).resolve() != repo:
        raise ValueError("Select the Git repository root, not a subdirectory.")
    return repo


def launch_preflight(repo, runs_dir, *, run_command=None, other_active_run=None) -> dict:
    """Report checks for a previously validated repository root.

    New runs require clean=True; resume can retain staged work for reconciliation.
    Callers may supply their existing live activity check; otherwise saved run
    records are checked conservatively. Reservations and process ownership remain
    the launcher's responsibility. No locks are acquired or removed here.
    """
    run = run_command if run_command is not None else bounded_run
    result = {"branch": None, "clean": None, "errors": [], "resume_error": None, "start_error": None}
    if repo is None:
        result["errors"].append("Select a Git repository root.")
        return result
    try:
        assert_test_process_safe(repo)
        assert_process_safe(repo)
    except OSError as exc:
        result["errors"].append(str(exc))
        return result
    repo, runs_dir = Path(repo), Path(runs_dir).expanduser().resolve()
    job_lock = execution_lock(repo, ".job.lock")
    job_busy = job_lock is not None and job_lock.exists()
    if job_busy:
        result["errors"].append("An Auto-Coding runner is active or uncertain for this checkout.")
    if runs_dir == repo or runs_dir.is_relative_to(repo):
        result["start_error"] = "Run storage must be outside the project. Select a different project or configure external run storage."
    active = other_active_run() if other_active_run is not None else repository_busy(repo, runs_dir)
    if active is not False and not job_busy:
        result["errors"].append("An earlier desktop run may still be active. Open its logs and wait for it to stop.")
    try:
        branch = run(["git", "branch", "--show-current"], cwd=repo,
                     capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", shell=False, timeout=10)
        result["branch"] = branch.stdout.strip() if not branch.returncode else None
        if not result["branch"]:
            result["errors"].append("Select a feature branch; detached HEAD or unavailable Git branch.")
        elif result["branch"].lower() in {"main", "master"}:
            result["errors"].append("Runs are blocked on main/master. Switch to a feature branch first.")
        status = run(["git", "status", "--porcelain", "--untracked-files=all"],
                     cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", shell=False, timeout=10)
        result["clean"] = not status.stdout.strip() if not status.returncode else None
        if status.returncode:
            result["errors"].append("Could not check Git working tree.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["errors"].append(f"Git preflight failed: {exc}")
    return result
