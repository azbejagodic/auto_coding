"""Read-only Git and run-storage checks shared by launch callers."""
from __future__ import annotations

from pathlib import Path
import subprocess

from auto_coding.repository_ownership import execution_lock, assert_process_safe
from auto_coding.process_runner import bounded_run
from auto_coding.repository_activity import repository_busy
from auto_coding.test_process_safety import assert_test_process_safe


PROTECTED_BRANCH_KEY = "autocoding.protectedBranch"


def parse_protected_branches(output):
    if not output:
        return ()
    if len(output) > 8192 or not output.endswith("\0"):
        raise ValueError("Configured protected branches are incomplete or oversized")
    names = output[:-1].split("\0")
    if len(names) > 32 or any(not name or name != name.strip() or len(name) > 1024
                              or any(ord(char) < 32 or ord(char) == 127 for char in name) for name in names):
        raise ValueError("Invalid configured protected branch name")
    return tuple(names)


def _policy_command(run, repo, argv):
    try:
        return run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                   errors="surrogateescape", shell=False, timeout=10)
    except BaseException as exc:
        cleanup = getattr(exc, "cleanup_confirmed", None)
        if cleanup is not True and not (cleanup is None and getattr(exc, "process_started", None) is False):
            from auto_coding.github_workspace import mark_process_uncertain
            mark_process_uncertain()
        raise


def configured_protected_branches(repo, *, run_command=None):
    run = run_command if run_command is not None else bounded_run
    result = _policy_command(run, repo, ["git", "config", "--local", "--null", "--get-all", PROTECTED_BRANCH_KEY])
    if result.returncode == 1 and not result.stdout and not getattr(result, "stderr", None):
        return ()  # Git's explicit 'key not present' result.
    if result.returncode:
        raise OSError("Could not read repository-local protected branches")
    names = parse_protected_branches(result.stdout)
    for name in names:
        valid = _policy_command(run, repo, ["git", "check-ref-format", "refs/heads/" + name])
        if name.startswith("-") or valid.returncode:
            raise ValueError("Configured protected branch name is not a valid literal Git branch")
    return names


def validate_feature_branch(branch, *, protected_branches=()):
    if not branch:
        raise ValueError("Select a feature branch; detached HEAD or unavailable Git branch.")
    if branch.lower() in {"main", "master"}:
        raise ValueError("Runs are blocked on main/master. Switch to a feature branch first.")
    if branch.casefold() in {name.casefold() for name in protected_branches}:
        raise ValueError(f"Branch {branch!r} is protected by repository-local {PROTECTED_BRANCH_KEY}; use a development branch.")
    return branch


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
        try:
            validate_feature_branch(result["branch"])
        except ValueError as exc:
            result["errors"].append(str(exc))
        status = run(["git", "status", "--porcelain", "--untracked-files=all"],
                     cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", shell=False, timeout=10)
        result["clean"] = not status.stdout.strip() if not status.returncode else None
        if status.returncode:
            result["errors"].append("Could not check Git working tree.")
        if not result["errors"]:
            try:
                validate_feature_branch(result["branch"], protected_branches=configured_protected_branches(repo, run_command=run))
            except ValueError as exc:
                result["errors"].append(str(exc))
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["errors"].append(f"Git preflight failed: {exc}")
    return result
