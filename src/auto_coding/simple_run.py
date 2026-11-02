"""Codex A -> tests -> Codex B coordinator; local checkpoints by default.

Explicit opt-in publication runs synchronously after validated approved commits.
"""
from __future__ import annotations

import argparse
import hashlib
import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Callable
from auto_coding.event_log import EventLog, EventLogError
from auto_coding.github_workspace import (managed_job, WorkspaceError, cancel_reservation,
                                         mark_process_uncertain, GitHubWorkspaceManager,
                                         normalize_repository)
from auto_coding.process_runner import bounded_run, OutputLimitExceeded
from auto_coding.metrics import codex_usage, write_metrics
from auto_coding.test_process_safety import TestProcessGate, assert_test_process_safe
from auto_coding.repository_ownership import assert_process_safe
from auto_coding.launch_preflight import validate_feature_branch
from auto_coding.sensitive_paths import is_sensitive_path


MAX_PLAN = 512 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MAX_REVIEW_PROCESS_ATTEMPTS = 3
REVIEW_RETRY_DELAYS = (5.0, 15.0)
# Kept separate from other process clocks so tests can control review waits.
_review_clock = time.time
_review_sleep = time.sleep
_execution_clock = time.monotonic
MAX_VERDICT_BYTES = 128 * 1024
MAX_REVIEW_ISSUE_CHARS = 2000
MAX_REVIEW_ISSUES_TOTAL = 9000
MAX_REVIEW_SUMMARY_CHARS = 4000
MAX_CORRECTION_CONTEXT = 14000
MAX_TEST_FEEDBACK = 12000
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVED", "REJECTED"]},
        "issues": {"type": "array", "items": {"type": "string", "minLength": 1,
                                             "maxLength": MAX_REVIEW_ISSUE_CHARS}, "maxItems": 20},
        "summary": {"type": "string", "minLength": 1, "maxLength": MAX_REVIEW_SUMMARY_CHARS},
    },
    "required": ["decision", "issues", "summary"],
    "additionalProperties": False,
}


class PilotError(Exception):
    pass


class _CallResult(tuple):
    """Keep the public three-value result and carry verified cleanup state."""

    def __new__(cls, code, output, timed_out, cleanup_confirmed=None, process_started=None):
        result = super().__new__(cls, (code, output, timed_out))
        result.cleanup_confirmed = cleanup_confirmed
        result.process_started = process_started
        return result


def load_tasks(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_PLAN:
        raise PilotError("Task file must be nonempty and smaller than 512 KB")
    fingerprint = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() in {".txt", ".md"}:
        prompt = raw.decode("utf-8-sig").strip()
        if not prompt:
            raise PilotError("Task file is empty")
        return [{"id": "task-1", "prompt": prompt, "test_commands": []}], fingerprint
    if path.suffix.lower() != ".json":
        raise PilotError("Use TXT, MD, or JSON for the pilot")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"tasks"} or not isinstance(value["tasks"], list):
        raise PilotError("JSON must contain only a tasks array")
    if not 1 <= len(value["tasks"]) <= 30:
        raise PilotError("Expected between 1 and 30 tasks")
    result, ids = [], set()
    for task in value["tasks"]:
        if not isinstance(task, dict) or not {"id", "prompt"} <= set(task) <= {"id", "prompt", "test_commands"}:
            raise PilotError("Each task requires id and prompt; test_commands is optional")
        ident, prompt = task["id"], task["prompt"]
        if not isinstance(ident, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", ident) is None or ident in ids:
            raise PilotError("Task IDs must be unique and simple")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 60000:
            raise PilotError("Invalid task prompt")
        commands = task.get("test_commands", [])
        if (not isinstance(commands, list) or len(commands) > 10
                or any(not isinstance(cmd, list) or not 1 <= len(cmd) <= 32 or
                       any(not isinstance(item, str) or not item or len(item) > 4096 for item in cmd)
                       for cmd in commands)):
            raise PilotError("Test commands must be arrays of argv lists, never shell strings")
        ids.add(ident)
        result.append({"id": ident, "prompt": prompt.strip(), "test_commands": commands})
    return result, fingerprint


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def call(argv: list[str], cwd: Path, timeout: int, *, prompt: str | None = None) -> tuple[int, str, bool]:
    """Capture complete bounded tool output; no shell execution."""
    try:
        result = bounded_run(argv, cwd=str(cwd), input=prompt, text=True,
                                encoding="utf-8", errors="surrogateescape" if argv[0] == "git" else "replace",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False, shell=False)
        return _CallResult(result.returncode, result.stdout or "", False, process_started=True)
    except subprocess.TimeoutExpired as exc:
        confirmed = getattr(exc, "cleanup_confirmed", False) is True
        if not confirmed:
            mark_process_uncertain()
        return _CallResult(124, "PROCESS TIMED OUT", True, confirmed, True)
    except OutputLimitExceeded as exc:
        confirmed = getattr(exc, "cleanup_confirmed", False) is True
        if not confirmed:
            mark_process_uncertain()
        return _CallResult(125, "PROCESS OUTPUT EXCEEDED SAFE LIMIT", False, confirmed, True)
    except OSError as exc:
        confirmed = getattr(exc, "cleanup_confirmed", None)
        if confirmed is False:
            mark_process_uncertain()
        return _CallResult(127, f"Process could not start: {type(exc).__name__}", False, confirmed,
                           getattr(exc, "process_started", None))
    except BaseException as exc:
        if getattr(exc, "cleanup_confirmed", False) is not True:
            mark_process_uncertain()
        raise


def execution_metadata(result, execution_id: int, started: float, *, codex=True, finished=None) -> dict:
    """Only numeric observations enter metrics; never prompts, argv or model text."""
    duration = round((finished if finished is not None else _execution_clock()) - started, 6)
    usage = None
    if codex:
        try:
            usage = codex_usage(result[1])
        except Exception as exc:
            print(f"WARNING: token usage unavailable: {type(exc).__name__}", file=sys.stderr)
    return {"execution_id": execution_id,
            "duration_seconds": duration,
            "process_started": getattr(result, "process_started", result[0] != 127),
            "usage": usage}


def cleanup_unverified(result):
    cleanup = getattr(result, "cleanup_confirmed", None)
    return (cleanup is not None and cleanup is not True) or (result[2] and cleanup is not True)


def record_process_uncertainty(state, state_path, actor, execution):
    state["uncertain_process"] = {"actor": actor, "task": execution["task_id"],
        "attempt": execution["attempt"], "execution_id": execution["seq"], "cleanup_confirmed": False}
    state["status"] = "stopped"
    try:
        mark_process_uncertain()
    finally:
        save_json(state_path, state)
    print(f"STOPPED: {actor} execution {execution['seq']} cleanup is unverified; "
          "workspace execution and publication remain blocked.", flush=True)


def record_test_uncertainty(state, state_path, gate):
    context = gate.context
    mark_process_uncertain()
    state["uncertain_test"] = context | {"cleanup_confirmed": False}
    state["status"] = "stopped"
    print(f"STOPPED: task {context['task']}, attempt {context['attempt']}, test {context['index']} "
          f"(execution {context['execution_id']}): process cleanup is unverified. "
          "No further execution or approval is allowed; inspect the test process tree and safety gate.", flush=True)
    try:
        gate.keep_uncertain()
    finally:
        save_json(state_path, state)


def safe_repo(repo: Path, create: bool) -> Path:
    if not repo.exists():
        if not create:
            raise PilotError("Target does not exist; use --create-repo for a new disposable project")
        repo.mkdir(parents=True)
    repo = repo.resolve(strict=True)
    if not repo.is_dir():
        raise PilotError("Target must be a directory")
    git = call(["git", "rev-parse", "--show-toplevel"], repo, 15)
    if git[0] != 0:
        if create and not any(repo.iterdir()):
            created = call(["git", "init", "-b", "feature/auto-coding"], repo, 30)
            if created[0] != 0:
                raise PilotError("Could not initialize disposable Git repository")
        else:
            raise PilotError("Target must be a Git repository (or empty with --create-repo)")
    root = call(["git", "rev-parse", "--show-toplevel"], repo, 15)
    if root[0] != 0 or Path(root[1].strip()).resolve() != repo:
        raise PilotError("Target must be the Git repository root")
    return repo


def codex_argv(*, readonly: bool, last_message: Path, schema: Path | None, model: str | None) -> list[str]:
    cmd = ["codex", "--ask-for-approval", "never", "exec", "--sandbox",
           "read-only" if readonly else "workspace-write", "--json",
           "--output-last-message", str(last_message)]
    if schema is not None:
        cmd.extend(["--output-schema", str(schema)])
    if model:
        cmd.extend(["--model", model])
    return [*cmd, "-"]


def files_overview(repo: Path) -> str:
    result = call(["git", "status", "--short", "--untracked-files=all"], repo, 15)
    if result[0] != 0:
        raise PilotError("Could not check working tree")
    return result[1][:30000]


def git(repo: Path, args: list[str], timeout: int = 30) -> str:
    rc, output, timed = call(["git", *args], repo, timeout)
    if rc or timed:
        raise PilotError(f"Git {' '.join(args[:3])} failed: {output[-1500:]}")
    return output.strip()


def git_patch(repo: Path, args: list[str]) -> str:
    """Read an entire patch without trimming meaningful trailing whitespace."""
    rc, output, timed = call(["git", *args], repo, 30)
    if rc or timed:
        raise PilotError(f"Git patch command failed: {args[:3]}")
    return output


def require_run_branch(repo, expected=None):
    branch = git(repo, ["branch", "--show-current"])
    try:
        validate_feature_branch(branch)
    except ValueError as exc:
        raise PilotError(str(exc)) from exc
    if expected is not None and branch != expected:
        raise PilotError("Git branch changed from the run's selected feature branch; inspect before continuing")
    return branch


def head(repo: Path) -> str | None:
    rc, output, timed = call(["git", "rev-parse", "--verify", "HEAD"], repo, 15)
    if timed or rc not in (0, 128):
        raise PilotError("Could not determine Git HEAD safely")
    return output.strip() if rc == 0 else None


def git_clean(repo: Path) -> bool:
    return not files_overview(repo).strip()


# Build/test caches do not belong in task reviews or commits. This list deliberately
# does NOT include secrets such as .env; those must continue to stop the runner.
_GENERATED_GIT_EXCLUDES = (
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", ".venv/", "node_modules/", ".coverage", ".coverage.*",
)
_GENERATED_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "node_modules",
})


def is_generated_path(item: str) -> bool:
    components = item.replace("\\", "/").lower().split("/")
    base = components[-1]
    return (any(c in _GENERATED_DIRS for c in components[:-1])
            or base.endswith((".pyc", ".pyo"))
            or base == ".coverage" or base.startswith(".coverage."))


def ignore_generated_locally(repo: Path) -> None:
    """Ignore standard generated files locally; do not modify project .gitignore."""
    git_path = Path(git(repo, ["rev-parse", "--git-path", "info/exclude"]))
    if not git_path.is_absolute():
        git_path = repo / git_path
    git_path.parent.mkdir(parents=True, exist_ok=True)
    marker = "# Auto-Coding pilot generated artifacts (local only)"
    old = git_path.read_text(encoding="utf-8") if git_path.exists() else ""
    if marker not in old:
        with git_path.open("a", encoding="utf-8") as stream:
            stream.write(("\n" if old and not old.endswith("\n") else "")
                         + marker + "\n" + "\n".join(_GENERATED_GIT_EXCLUDES) + "\n")


def reject_sensitive_paths(paths):
    for item in paths:
        if is_sensitive_path(item):
            raise PilotError(f"Sensitive file in changes: {item}; inspect manually")


def assert_staged_paths_safe(repo):
    paths = git(repo, ["diff", "--cached", "--name-only", "-z"]).split("\0")
    reject_sensitive_paths(item for item in paths if item)


def staging_summary(repo: Path) -> tuple[str, str, str]:
    """Stage project changes only; omit build caches, never omit secret files."""
    ignore_generated_locally(repo)
    git(repo, ["add", "-A"])
    paths = [p for p in git(repo, ["diff", "--cached", "--name-only", "-z"]).split("\0") if p]
    reject_sensitive_paths(paths)  # Never unstage a sensitive file as a generated artifact.
    for item in paths:
        if is_generated_path(item):
            # Handle the previous V2 failure, where a cache file is already staged.
            # Never silently unstage a file already present in a previous commit.
            if head(repo) is not None:
                exists, _, timed = call(["git", "cat-file", "-e", f"HEAD:{item}"], repo, 15)
                if timed or exists not in (0, 1, 128):
                    raise PilotError("Could not verify tracked generated file safely")
                if exists == 0:
                    raise PilotError(f"Previously tracked generated file changed: {item}; inspect manually")
            git(repo, ["rm", "-q", "-f", "--cached", "--", item])
    paths = [p for p in git(repo, ["diff", "--cached", "--name-only", "-z"]).split("\0") if p]
    if not paths:
        raise PilotError("No changed or created project files in this task")
    reject_sensitive_paths(paths)
    # Diff is used for both the reviewer and an approval-to-commit integrity check.
    diff = git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])
    diff_hash = hashlib.sha256(diff.encode("utf-8", errors="surrogateescape")).hexdigest()
    stat = git(repo, ["diff", "--cached", "--stat"])
    return stat, diff, diff_hash


def checked_review(verdict_path: Path) -> dict:
    try:
        with verdict_path.open("rb") as stream:
            raw = stream.read(MAX_VERDICT_BYTES + 1)
        if len(raw) > MAX_VERDICT_BYTES:
            raise ValueError("Oversized verdict")
        verdict = json.loads(raw.decode("utf-8"))
        if (not isinstance(verdict, dict) or set(verdict) != {"decision", "issues", "summary"}
                or verdict["decision"] not in ("APPROVED", "REJECTED")
                or not isinstance(verdict["issues"], list) or len(verdict["issues"]) > 20
                or any(not isinstance(x, str) or not x.strip()
                       or len(x) > MAX_REVIEW_ISSUE_CHARS for x in verdict["issues"])
                or sum(len(x) for x in verdict["issues"]) > MAX_REVIEW_ISSUES_TOTAL
                or (verdict["decision"] == "REJECTED" and not verdict["issues"])
                or (verdict["decision"] == "APPROVED" and verdict["issues"])
                or not isinstance(verdict["summary"], str) or not verdict["summary"].strip()
                or len(verdict["summary"]) > MAX_REVIEW_SUMMARY_CHARS):
            raise ValueError("Invalid verdict")
        return verdict
    except (ValueError, OSError, UnicodeError) as exc:
        raise PilotError("Invalid or missing Codex B verdict; see event logs") from exc


def local_commit(repo: Path, ident: str) -> str:
    assert_test_process_safe(repo)
    assert_process_safe(repo)
    require_run_branch(repo)
    assert_staged_paths_safe(repo)
    # Local checkpoints only. Never push. The user can set their Git identity;
    # if it is absent, an explicitly non-public local-only identity is used.
    name = git(repo, ["config", "--get", "user.name"]) if call(["git", "config", "--get", "user.name"], repo, 15)[0] == 0 else "Auto-Coding (local)"
    email = git(repo, ["config", "--get", "user.email"]) if call(["git", "config", "--get", "user.email"], repo, 15)[0] == 0 else "auto-coding@localhost.invalid"
    git(repo, ["-c", f"user.name={name}", "-c", f"user.email={email}",
               "commit", "-m", f"auto-coding: {ident}", "--no-gpg-sign"], 120)
    result = head(repo)
    if not result:
        raise PilotError("Could not identify newly created checkpoint commit")
    return result


def verify_approved_commit(repo: Path, pending: dict, current: str | None, *, require_clean=True) -> None:
    """Accept only the exact approved patch on the exact recorded parent.

    A matching commit subject is not proof of approval. Check both normal
    commits and crash recovery before reporting a checkpoint as committed.
    """
    if (not isinstance(current, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", current)
            or (require_clean and (current != head(repo) or not git_clean(repo)))):
        raise PilotError("Checkpoint state ambiguous: inspect repository and logs")
    names = git(repo, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", current]).split("\0")
    reject_sensitive_paths(item for item in names if item)
    message = git(repo, ["log", "-1", "--format=%s", current])
    lineage = git(repo, ["rev-list", "--parents", "-n", "1", current]).split()
    base = pending.get("base_head")
    expected_lineage = [current, base] if base else [current]
    committed_diff = git_patch(repo, ["show", "--format=", "--binary", "--no-ext-diff", current])
    committed_sha256 = hashlib.sha256(committed_diff.encode("utf-8", errors="surrogateescape")).hexdigest()
    if (message != f"auto-coding: {pending['task']}" or lineage != expected_lineage
            or committed_sha256 != pending.get("diff_sha256")):
        raise PilotError("Recovered checkpoint does not match the approved parent and diff; inspect manually")


def committed_records(state):
    history = state.get("history")
    if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
        raise PilotError("Saved run history is ambiguous; inspect before resume")
    return [record for record in history if record.get("commit")]


def stop_uncertain_saved_process(state, reason):
    state["uncertain_process"] = {"actor": "saved execution", "cleanup_confirmed": False, "reason": reason}
    mark_process_uncertain()
    raise PilotError(reason)


def expected_run_head(state, manifest):
    completed = state["completed"]
    if not completed:
        return manifest["initial_git_head"]
    records = committed_records(state)
    if not records or records[-1].get("task") != completed[-1]:
        raise PilotError("Completed tasks lack an approved Git checkpoint; inspect before resume")
    return records[-1]["commit"]


def verify_resume_git(repo, state, manifest, tasks):
    """Validate existing approvals without changing the checkout or inventing progress."""
    branch = require_run_branch(repo, state.get("branch"))
    if manifest.get("branch") not in (None, branch):
        raise PilotError("Run manifest Git branch differs from the selected feature branch")
    completed = state["completed"]
    if not isinstance(completed, list) or completed != [task["id"] for task in tasks[:len(completed)]]:
        raise PilotError("Completed tasks are not an ordered prefix of this plan")
    records = committed_records(state)
    if any(record.get("a_cleanup_confirmed") is False for record in state["history"]):
        stop_uncertain_saved_process(state, "Saved Codex A cleanup is unverified; inspect before resume")
    ownership = state.get("process_ownership")
    if ownership is not None and (type(ownership) is not int or ownership != 1):
        raise PilotError("Unsupported saved process-ownership state; inspect before resume")
    if state.get("process_ownership") is None:
        # Older unmanaged runners did not reliably save A cleanup or a B
        # failure observed before evidence I/O. Do not invent missing proof.
        for record in state["history"]:
            if type(record.get("a_exit")) is not int or type(record.get("a_timed_out")) is not bool:
                stop_uncertain_saved_process(state, "Legacy execution metadata is ambiguous; inspect before resume")
            if ((record.get("a_timed_out") or record.get("a_exit") in (125, 127))
                    and record.get("a_cleanup_confirmed") is not True
                    and record.get("a_process_started") is not False):
                stop_uncertain_saved_process(state, "Legacy Codex A cleanup lacks verified evidence; inspect before resume")
        review = state.get("pending_review")
        if review and review.get("review_invocations", 0):
            # A saved failure kind can belong to an earlier invocation; it is
            # not proof of the most recently reserved legacy process's cleanup.
            stop_uncertain_saved_process(state, "Legacy Codex B cleanup lacks verified evidence; inspect before resume")
    if [record.get("task") for record in records] != completed:
        raise PilotError("Completed tasks lack matching approved Git checkpoints")
    expected = manifest["initial_git_head"]
    if "initial_git_head" in state and state["initial_git_head"] != expected:
        raise PilotError("Saved initial Git HEAD differs from the run manifest")
    for record in records:
        review = record.get("review")
        if (not isinstance(review, dict) or review.get("decision") != "APPROVED"
                or review.get("issues") != [] or record.get("base_head") != expected
                or record.get("branch") not in (None, branch)):
            raise PilotError("Completed Git checkpoint does not match its approval and base")
        verify_approved_commit(repo, record, record["commit"], require_clean=False)
        expected = record["commit"]
    pending = state.get("pending_checkpoint") or state.get("pending_review")
    if pending:
        if len(completed) >= len(tasks) or pending.get("task") != tasks[len(completed)]["id"]:
            raise PilotError("Pending checkpoint is out of sequence in the prepared plan")
        if pending.get("base_head") != expected or pending.get("branch") not in (None, branch):
            raise PilotError("Pending checkpoint does not match the approved Git base and branch")
        if isinstance(pending.get("record"), dict) and pending["record"].get("a_cleanup_confirmed") is False:
            stop_uncertain_saved_process(state, "Pending Codex A cleanup is unverified; inspect before resume")
        # Approved commit recovery validates both permissible HEAD outcomes itself.
        if state.get("pending_checkpoint"):
            record = pending.get("record")
            review = record.get("review") if isinstance(record, dict) else None
            if (not isinstance(review, dict) or review.get("decision") != "APPROVED" or review.get("issues") != []
                    or record.get("task") != pending.get("task") or record.get("base_head") != expected
                    or record.get("diff_sha256") != pending.get("diff_sha256")
                    or record.get("branch") not in (None, branch)):
                raise PilotError("Pending checkpoint lacks matching independent approval")
            return branch
    if head(repo) != expected:
        raise PilotError("Git HEAD changed from the last approved checkpoint or initial run HEAD")
    return branch


def legacy_initial_head(repo, state):
    if "initial_git_head" in state:
        return state["initial_git_head"]
    records = committed_records(state)
    evidence = records[0] if records else state.get("pending_checkpoint") or state.get("pending_review")
    if evidence is not None and "base_head" in evidence:
        return evidence["base_head"]  # Verified before a replacement manifest is written.
    if not state["completed"] and head(repo) is None:
        return None
    raise PilotError("Legacy run lacks evidence of its initial Git HEAD; inspect before resume")


def recover_checkpoint(repo: Path, state: dict, state_path: Path, events: EventLog | None = None) -> None:
    """Reconcile the tiny window between successful commit and state save."""
    assert_test_process_safe(repo)
    assert_process_safe(repo)
    require_run_branch(repo, state.get("branch"))
    pending = state.get("pending_checkpoint")
    if not pending:
        return
    require_run_branch(repo, pending.get("branch", state.get("branch")))
    current = head(repo)
    task = pending["task"]
    if current != pending.get("base_head"):
        verify_approved_commit(repo, pending, current)
        if events:
            events.emit("Git", "checkpoint_recovered", "observed", task_id=task,
                        attempt=pending["record"].get("attempt"),
                        metadata={"commit": current, "outcome": "already committed"})
            events.emit("Git", "checkpoint_committed", "success", task_id=task,
                        attempt=pending["record"].get("attempt"), metadata={"commit": current, "recovered": True})
        state["completed"].append(task)
        state["history"].append(pending["record"] | {"commit": current})
        state["pending_checkpoint"] = None
        state["status"] = "stopped" if state.get("auto_push") else "running"
        save_json(state_path, state)
        return
    # Approval exists but a local commit hasn't completed. Only permit recovery
    # when the staged bytes still match exactly what Codex B approved.
    if git(repo, ["diff", "--name-only"]).strip() or git(repo, ["ls-files", "--others", "--exclude-standard"]).strip():
        raise PilotError("Unreviewed edits after approval: inspect before resume")
    diff = git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])
    if hashlib.sha256(diff.encode("utf-8", errors="surrogateescape")).hexdigest() != pending["diff_sha256"]:
        raise PilotError("Approved diff changed before checkpoint")
    if events:
        events.emit("Git", "checkpoint_started", "started", task_id=task,
                    attempt=pending["record"].get("attempt"), metadata={"recovered": True})
    try:
        require_run_branch(repo, state.get("branch"))
        committed = local_commit(repo, task)
    except PilotError:
        if events:
            events.emit("Git", "checkpoint_stopped", "failed", task_id=task,
                        attempt=pending["record"].get("attempt"), metadata={"reason": "checkpoint recovery failed"})
        raise
    verify_approved_commit(repo, pending, committed)
    require_run_branch(repo, state.get("branch"))
    if events:
        events.emit("Git", "checkpoint_committed", "success", task_id=task,
                    attempt=pending["record"].get("attempt"), metadata={"commit": committed, "recovered": True})
    state["completed"].append(task)
    state["history"].append(pending["record"] | {"commit": committed})
    state["pending_checkpoint"] = None
    state["status"] = "stopped" if state.get("auto_push") else "running"
    save_json(state_path, state)


def publish_approved(repo, state, state_path, events):
    """Reconcile with the remote even when a previous saved result says published."""
    assert_test_process_safe(repo)
    assert_process_safe(repo)
    require_run_branch(repo, state.get("branch"))
    config = state.get("auto_push")
    records = [r for r in state["history"] if r.get("commit")]
    if not config or not records:
        return True
    record = records[-1]
    sha = record["commit"]
    publication = {"commit": sha, "task": record["task"], "outcome": "pending",
                   "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    state["publication"] = publication
    state["status"] = "stopped"  # Durable retry point before any network operation.
    save_json(state_path, state)
    events.emit("Git", "push_started", "started", task_id=record["task"],
                metadata={"commit": sha, "branch": config["branch"], "local": "approved"})
    manager = GitHubWorkspaceManager(state_path.parent)
    try:
        manager.publish_checkpoint(repo, config["repository"], config["branch"], sha,
                                   {r["commit"]: r for r in records})
    except WorkspaceError as exc:
        publication.update(outcome="pending", error_code=exc.code,
                           finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        save_json(state_path, state)
        events.emit("Git", "push_finished", "failed", task_id=record["task"],
                    metadata={"commit": sha, "local": "approved", "remote": "unconfirmed", "reason": exc.code})
        print(f"APPROVED LOCALLY, PUSH PENDING: {exc} Retry Push by resuming this run; the identical SHA is retried without rerunning Codex.")
        return False
    publication.update(outcome="published", finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    # Remain resumable if interrupted before the next task or final completion save.
    state["status"] = "stopped"
    save_json(state_path, state)
    events.emit("Git", "push_finished", "success", task_id=record["task"],
                metadata={"commit": sha, "local": "approved", "remote": "confirmed"})
    return True


def correction_context(prior: list[dict]) -> str:
    """Keep the latest blockers complete and older review issues as a bounded checklist."""
    if not prior:
        return ""
    latest = prior[-1]
    review = latest.get("review")
    if isinstance(review, dict) and review.get("decision") == "REJECTED":
        issues = review["issues"]
        current = "\n".join(f"{index}. {issue}" for index, issue in enumerate(issues, 1))
        label = f"CURRENT CODEX B BLOCKERS (attempt {latest.get('attempt', '?')}):\n"
        seen = {issue.strip().casefold() for issue in issues}
    else:
        current = latest.get("feedback") or ""
        label = f"LATEST FAILURE (attempt {latest.get('attempt', '?')}):\n"
        seen = set()
    if not current:
        current = "No detailed feedback was recorded for the latest attempt; recheck earlier issues."
    guidance = ("Fix the reported problems in the current task. Preserve behavior that "
                "already works and keep the change focused. Recheck earlier review issues "
                "and fix any still present; do not rework an issue that is already fixed. "
                "Omission from a later review is not proof that an earlier issue is resolved.\n\n")
    base = guidance + label
    if len(base) + len(current) > MAX_CORRECTION_CONTEXT - 250:
        marker = "\n[Feedback shortened; full evidence remains in the run logs.]\n"
        room = MAX_CORRECTION_CONTEXT - 250 - len(base) - len(marker)
        current = current[:room // 2] + marker + current[-(room - room // 2):]
    result = base + current
    earlier = []
    for entry in reversed(prior[:-1]):
        verdict = entry.get("review")
        if not isinstance(verdict, dict) or verdict.get("decision") != "REJECTED":
            continue
        for issue in verdict.get("issues", []):
            key = issue.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                earlier.append(f"- attempt {entry.get('attempt', '?')}: {issue}")
    if earlier:
        heading = "\n\nEARLIER REVIEW ISSUES TO RECHECK:\n"
        room = MAX_CORRECTION_CONTEXT - len(result) - len(heading) - 100
        included = []
        for line in earlier:
            if len(line) + 1 <= room:
                included.append(line)
                room -= len(line) + 1
        omitted = len(earlier) - len(included)
        if included or omitted:
            result += heading + "\n".join(included)
            if omitted:
                result += f"\n[{omitted} older review issue(s) omitted from this prompt; see run history.]"
    return result


def review_prompt_for(task: dict, stat: str, diff: str, tests: list[dict]) -> str:
    return ("You are Codex B, an independent READ-ONLY reviewer. Inspect the current "
        "task's STAGED diff, all relevant project files, and tests. Previous tasks are locally "
        "committed. Do not blindly trust implementer claims or test results. Reject only for "
        "substantive, reproducible blockers in the CURRENT task/slice. List ALL such blockers "
        "together in one verdict. For EACH rejected issue, state the affected path or behavior, "
        "a minimal reproduction, the observed and expected results, and a concrete check "
        "that would show it is fixed. Keep each issue self-contained and concise. "
        f"Use at most {MAX_REVIEW_ISSUES_TOTAL} characters across all issues. "
        "Independently recheck corrected code; report blockers still present or newly introduced. "
        "Do not block approval for "
        "optional polish, hypothetical risks, unrelated pre-existing issues or future slices. "
        "If no current blockers remain, return APPROVED with an empty issues list; put any "
        "optional polish in summary as non-blocking suggestions. Put every blocker in issues, "
        "not only in summary; the decision and issues must agree. Inspect Git/credential safety "
        "and data-loss risks carefully. Merge to Main and per-task model/reasoning selection "
        "are deferred and are not approval blockers. Do not change files.\n\nTASK:\n" + task["prompt"] +
        "\n\nCURRENT TASK CHANGES:\n" + stat + "\n\nDIFF (truncated; inspect repository for full):\n" + diff[:35000] +
        "\n\nTEST RESULTS:\n" + json.dumps(tests))


def verify_review_ready(repo: Path, logs: Path, state: dict, task: dict) -> tuple[dict, str]:
    """Rebuild a review request only for the exact tested, staged implementation."""
    assert_test_process_safe(repo)
    assert_process_safe(repo)
    require_run_branch(repo, state.get("branch"))
    pending = state.get("pending_review")
    if not isinstance(pending, dict):
        raise PilotError("Missing review-ready checkpoint")
    if pending.get("process_uncertain") or pending.get("review_failure_kind") == "cleanup_unverified":
        raise PilotError("Reviewer process cleanup is unverified; inspect workspace lock")
    if pending.get("branch") not in (None, state.get("branch")):
        raise PilotError("Review-ready Git branch differs from the saved run")
    record = pending.get("record")
    tests = record.get("tests") if isinstance(record, dict) else None
    if (pending.get("task") != task["id"] or pending.get("repo") != str(repo)
            or pending.get("plan_sha256") != state["plan_sha256"]
            or not isinstance(pending.get("attempt"), int) or pending["attempt"] < 1
            or type(pending.get("review_invocations")) is not int
            or pending["review_invocations"] < 0
            or type(pending.get("review_invocation_limit", MAX_REVIEW_PROCESS_ATTEMPTS)) is not int
            or not MAX_REVIEW_PROCESS_ATTEMPTS <= pending.get("review_invocation_limit", MAX_REVIEW_PROCESS_ATTEMPTS)
                   <= max(MAX_REVIEW_PROCESS_ATTEMPTS, pending["review_invocations"] + 1)
            or ("review_retry_after" in pending and
                (type(pending["review_retry_after"]) not in (int, float)
                 or not math.isfinite(pending["review_retry_after"])))
            or not isinstance(record, dict) or record.get("task") != task["id"]
            or record.get("attempt") != pending["attempt"] or record.get("a_exit") != 0
            or record.get("a_timed_out") is not False or record.get("review") is not None
            or record.get("a_cleanup_confirmed") is False
            or not isinstance(tests, list) or len(tests) != len(task["test_commands"])):
        raise PilotError("Review-ready checkpoint does not match the task or verified tests")
    for index, result in enumerate(tests, 1):
        if (not isinstance(result, dict) or result.get("index") != index
                or result.get("exit_code") != 0 or result.get("timed_out") is not False
                or not isinstance(result.get("output_sha256"), str)):
            raise PilotError("Review-ready checkpoint has invalid test evidence")
        output_path = logs / f"{task['id']}-{pending['attempt']:02d}-test-{index}.txt"
        if (not output_path.is_file() or
                hashlib.sha256(output_path.read_bytes()).hexdigest() != result["output_sha256"]):
            raise PilotError("Review-ready test output changed; tests must be rerun")
    if head(repo) != pending.get("base_head"):
        raise PilotError("Review-ready Git HEAD changed; tests must be rerun")
    if not git_clean_staged(repo):
        raise PilotError("Review-ready worktree has unstaged or untracked changes")
    assert_staged_paths_safe(repo)
    diff = git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])
    if (not diff or hashlib.sha256(diff.encode("utf-8", errors="surrogateescape")).hexdigest()
            != pending.get("diff_sha256")):
        raise PilotError("Review-ready staged diff changed; tests must be rerun")
    stat = git(repo, ["diff", "--cached", "--stat"])
    return record, review_prompt_for(task, stat, diff, tests)


def schedule_review_retry(pending: dict) -> float:
    delay = REVIEW_RETRY_DELAYS[min(pending["review_invocations"] - 1, len(REVIEW_RETRY_DELAYS) - 1)]
    pending["review_retry_after"] = _review_clock() + delay
    return delay


def stop_review_retry(logs, state, state_path, events, reason):
    pending = state["pending_review"]
    used = pending["review_invocations"]
    limit = pending.get("review_invocation_limit", MAX_REVIEW_PROCESS_ATTEMPTS)
    state["status"] = "stopped"
    save_json(state_path, state)
    events.emit("controller", "stopped", "failed", task_id=pending["task"], attempt=pending["attempt"],
                metadata={"reason": reason, "failure_kind": pending.get("review_failure_kind", "unknown"),
                          "review_invocations": used, "review_invocation_limit": limit,
                          "review_retry_limit": used >= limit})
    print(f"STOPPED: {reason}; reviewer calls reserved: {used}/{limit}. "
          f"Review-ready checkpoint preserved in {logs}. "
          "After checking the failure, use --resume --retry-review-once for one extra call "
          "if the budget is exhausted. Unverified cleanup still requires inspection.")
    return "stopped"


def review_ready_task(repo: Path, logs: Path, state: dict, state_path: Path,
                      events: EventLog, task: dict, schema_path: Path,
                      execute: Callable, timeout: int, model_b: str | None, *,
                      retry_review_once=False) -> str:
    """Retry reviewer infrastructure independently of Codex A correction attempts."""
    pending = state["pending_review"]
    ident, attempt = pending["task"], pending["attempt"]
    base = f"{ident}-{attempt:02d}"
    if retry_review_once:
        verify_review_ready(repo, logs, state, task)
        if pending["review_invocations"] < pending.get("review_invocation_limit", MAX_REVIEW_PROCESS_ATTEMPTS):
            raise PilotError("One extra review call can only be authorized after the saved budget is exhausted")
        pending["review_invocation_limit"] = pending["review_invocations"] + 1
        save_json(state_path, state)
        events.emit("controller", "review_retry_authorized", "observed", task_id=ident, attempt=attempt,
                    metadata={"review_invocation_limit": pending["review_invocation_limit"]})
    while True:
        record, review_prompt = verify_review_ready(repo, logs, state, task)
        used = pending["review_invocations"]
        limit = pending.get("review_invocation_limit", MAX_REVIEW_PROCESS_ATTEMPTS)
        if used >= limit:
            return stop_review_retry(logs, state, state_path, events, "Codex B review retry budget exhausted")
        if used and pending.get("review_failure_kind") not in {"launch_failure", "output_limit"}:
            if "review_retry_after" not in pending:
                # Legacy checkpoints and interruptions after process exit have
                # no deadline. Conservatively schedule one before another call.
                schedule_review_retry(pending)
                save_json(state_path, state)
            wait = min(max(0.0, pending["review_retry_after"] - _review_clock()), max(REVIEW_RETRY_DELAYS))
            if wait:
                events.emit("controller", "review_retry_wait", "started", task_id=ident, attempt=attempt,
                            metadata={"delay_seconds": wait, "review_invocations": used})
                _review_sleep(wait)
                record, review_prompt = verify_review_ready(repo, logs, state, task)
        print(f"[{ident}] Codex B — independent review", flush=True)
        ordinal = pending["review_invocations"] + 1
        suffix = "" if ordinal == 1 else f"-retry-{ordinal}"
        prompt_name = f"{base}-b{suffix}-prompt.txt"
        stream_name = f"{base}-b{suffix}-events.jsonl"
        verdict_name = f"{base}-b{suffix}-verdict.json"
        verdict_path = logs / verdict_name
        # A failed reviewer may have left a verdict file. Only a fresh process
        # response from this invocation can authorize a commit.
        if verdict_path.exists():
            raise PilotError("Review verdict path already exists; inspect before retry")
        pending["review_invocations"] = ordinal
        pending.pop("review_retry_after", None)
        state["status"] = "running"
        save_json(state_path, state)
        (logs / prompt_name).write_text(review_prompt, encoding="utf-8")
        execution = events.emit("Codex B", "review_started", "started", task_id=ident, attempt=attempt,
                    evidence={"prompt": prompt_name, "diff": f"{base}-diff.txt",
                              "stream": stream_name, "verdict": verdict_name},
                    metadata={"review_try": ordinal, "review_invocation": ordinal})
        try:
            execution_started = _execution_clock()
            result = execute(codex_argv(readonly=True, last_message=verdict_path,
                                        schema=schema_path, model=model_b),
                             repo, timeout, prompt=review_prompt)
        except BaseException as exc:
            cleanup = getattr(exc, "cleanup_confirmed", None)
            if cleanup is not True and not (cleanup is None and getattr(exc, "process_started", None) is False):
                pending["process_uncertain"] = True
                pending["review_failure_kind"] = "cleanup_unverified"
                record_process_uncertainty(state, state_path, "Codex B", execution)
            else:
                pending["review_failure_kind"] = ("timeout" if isinstance(exc, subprocess.TimeoutExpired)
                                                  else "output_limit" if isinstance(exc, OutputLimitExceeded)
                                                  else "interrupted")
                if pending["review_failure_kind"] != "output_limit":
                    schedule_review_retry(pending)
            state["status"] = "stopped"
            save_json(state_path, state)
            raise
        code, output, timed = result
        execution_finished = _execution_clock()
        uncertain = cleanup_unverified(result)
        if uncertain:
            pending["process_uncertain"] = True
            pending["review_failure_kind"] = "cleanup_unverified"
            record_process_uncertainty(state, state_path, "Codex B", execution)
        measurement = execution_metadata(result, execution["seq"], execution_started, finished=execution_finished)
        (logs / stream_name).write_text(output, encoding="utf-8")
        events.emit("Codex B", "review_process_finished",
                    "cleanup_unverified" if uncertain else (
                        "timed_out" if timed else ("success" if code == 0 else "failed")),
                    task_id=ident, attempt=attempt, evidence={"stream": stream_name},
                    metadata={"exit_code": code, "timed_out": timed,
                              "review_try": ordinal, "review_invocation": ordinal}
                              | measurement)
        if not uncertain:
            # The reviewer has exited; a crash while parsing its verdict can
            # safely resume by making a fresh read-only review request.
            state["status"] = "stopped"
            save_json(state_path, state)
        if uncertain:
            reason = "Codex B process cleanup unverified"
            failure_kind = "cleanup_unverified"
            pending["process_uncertain"] = True
        elif timed:
            reason, failure_kind = "Codex B process timeout", "timeout"
        elif code == 125:
            reason, failure_kind = "Codex B output exceeded safe limit", "output_limit"
        elif code == 127:
            reason, failure_kind = "Codex B could not launch", "launch_failure"
        elif code != 0:
            reason, failure_kind = "Codex B process failure", "process_failure"
        else:
            try:
                verdict = checked_review(verdict_path)
            except PilotError:
                reason, failure_kind = "invalid review verdict", "invalid_verdict"
            else:
                # A read-only reviewer is still an external process; inspect the
                # worktree after it exits before accepting either verdict.
                verify_review_ready(repo, logs, state, task)
                record["review"] = verdict
                events.emit("Codex B", "review_finished", verdict["decision"].lower(),
                            task_id=ident, attempt=attempt, evidence={"verdict": verdict_name},
                            metadata={"execution_id": execution["seq"]})
                if verdict["decision"] == "APPROVED" and not verdict["issues"]:
                    record.update(base_head=head(repo), diff_sha256=pending["diff_sha256"], branch=state["branch"])
                    state["pending_checkpoint"] = {"task": ident, "base_head": pending["base_head"],
                                                    "diff_sha256": pending["diff_sha256"], "record": record,
                                                    "branch": state["branch"]}
                    state["pending_review"] = None
                    state["status"] = "checkpointing"
                    save_json(state_path, state)
                    events.emit("Git", "checkpoint_started", "started", task_id=ident,
                                attempt=attempt, evidence={"diff": f"{base}-diff.txt"})
                    try:
                        require_run_branch(repo, state["branch"])
                        commit = local_commit(repo, ident)
                        verify_approved_commit(repo, state["pending_checkpoint"], commit)
                        require_run_branch(repo, state["branch"])
                    except PilotError as exc:
                        events.emit("Git", "stopped", "failed", task_id=ident, attempt=attempt,
                                    evidence={"state": "state.json"},
                                    metadata={"reason": "checkpoint pending reconciliation", "error_type": type(exc).__name__})
                        print(f"STOPPED: local checkpoint failed: {exc}")
                        return "stopped"
                    events.emit("Git", "checkpoint_committed", "success", task_id=ident,
                                attempt=attempt, metadata={"commit": commit})
                    state["completed"].append(ident)
                    state["history"].append(record | {"commit": commit})
                    state["pending_checkpoint"] = None
                    state["status"] = "stopped" if state.get("auto_push") else "running"
                    save_json(state_path, state)
                    events.emit("controller", "task_completed", "success", task_id=ident,
                                attempt=attempt, metadata={"commit": commit})
                    print(f"[{ident}] APPROVED — saved local checkpoint {commit[:8]}", flush=True)
                    return "approved" if publish_approved(repo, state, state_path, events) else "stopped"
                feedback = "Codex B rejected:\n" + "\n".join(verdict["issues"])
                record["feedback"] = feedback
                state["history"].append(record)
                state["pending_review"] = None
                state["status"] = "running"
                save_json(state_path, state)
                events.emit("controller", "correction_requested", "retry", task_id=ident,
                            attempt=attempt, evidence={"verdict": verdict_name})
                print(f"[{ident}] Rejected; returning Codex B feedback to Codex A", flush=True)
                return "rejected"
        pending["review_failure_kind"] = failure_kind
        retryable = failure_kind in {"process_failure", "timeout", "invalid_verdict"}
        delay = schedule_review_retry(pending) if retryable else 0
        state["status"] = "stopped"
        save_json(state_path, state)
        events.emit("Codex B", "review_failed", failure_kind, task_id=ident, attempt=attempt,
                    metadata={"execution_id": execution["seq"], "review_invocation": ordinal})
        if not retryable or ordinal >= limit:
            return stop_review_retry(logs, state, state_path, events, reason)
        events.emit("controller", "review_retry", "retry", task_id=ident, attempt=attempt,
                    metadata={"reason": reason, "failure_kind": failure_kind,
                              "review_try": ordinal, "delay_seconds": delay})


def _run(plan: Path, repo: Path, logs: Path, *, create_repo=False, resume=False, attempts=3,
        auto_push=None, retry_review_once=False,
        model_a=None, model_b=None, timeout=1800,
        execute: Callable = call) -> int:
    if retry_review_once and not resume:
        raise PilotError("One extra review call requires --resume and an exhausted review-ready checkpoint")
    tasks, plan_sha = load_tasks(plan)
    repo = safe_repo(repo, create_repo)
    assert_test_process_safe(repo)
    assert_process_safe(repo)
    selected_branch = require_run_branch(repo)
    if auto_push:
        identity = normalize_repository(auto_push["repository"])[0]
        branch = auto_push["branch"]
        if (identity != auto_push["repository"] or branch.lower() in {"main", "master"}
                or selected_branch != branch):
            raise PilotError("Auto-push requires the selected origin feature branch; main/master are protected")
        git(repo, ["check-ref-format", "refs/heads/" + branch])
    logs = logs.resolve(strict=False)
    if logs == repo or logs.is_relative_to(repo):
        raise PilotError("Log directory must be outside the target project")
    logs.mkdir(parents=True, exist_ok=True)
    state_path = logs / "state.json"
    if state_path.exists():
        if not resume:
            raise PilotError("Existing run logs; use --resume (never overwrite a run)")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise PilotError("Saved run state must be an object; inspect before resume")
        if not isinstance(state.get("completed"), list):
            raise PilotError("Saved completed tasks are ambiguous; inspect before resume")
        committed_records(state)
        if any(state.get(key) is not None and not isinstance(state[key], dict)
               for key in ("pending_review", "pending_checkpoint")):
            raise PilotError("Saved pending checkpoint is ambiguous; inspect before resume")
        if "uncertain_test" in state:
            raise PilotError("Test-process cleanup remains unverified in saved state; inspect before resuming.")
        if "uncertain_process" in state:
            raise PilotError("Process cleanup is unverified in saved state; inspect before resuming.")
        if state.get("plan_sha256") != plan_sha or state.get("repo") != str(repo):
            raise PilotError("Resume plan or repository mismatch")
        if state.get("auto_push") != auto_push:
            raise PilotError("Auto-push settings are fixed for this run; use its original repository and branch options")
        if retry_review_once and not state.get("pending_review"):
            raise PilotError("One extra review call requires an exhausted review-ready checkpoint")
        if state.get("status") == "completed" and (state.get("pending_review") or state.get("pending_checkpoint")):
            raise PilotError("Completed state still has an unfinished checkpoint")
        if state.get("status") not in {"stopped", "checkpointing", "completed"}:
            raise PilotError("Previous run may still be active or interrupted; inspect before resuming")
        manifest_path = logs / "run-manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise PilotError("Run manifest must be an object")
            if (manifest.get("plan_sha256") != plan_sha or manifest.get("target_repo") != str(repo)
                    or manifest.get("task_ids") != [t["id"] for t in tasks]):
                raise PilotError("Run manifest does not match resume plan or repository")
            run_id = manifest["run_id"]
        else:
            # A legacy manifest may be absent while a timeline already exists.
            # Its recorded run ID is authoritative; validate/recover it before
            # creating the one-time manifest so a mismatch cannot rewrite it.
            timeline_path = logs / "events.jsonl"
            run_id = state.get("run_id") or str(uuid.uuid4())
            if timeline_path.exists():
                first_line = next((line for line in timeline_path.read_bytes().splitlines() if line.strip()), b"")
                if first_line:
                    try:
                        first_event = json.loads(first_line)
                        candidate = first_event.get("run_id") if isinstance(first_event, dict) else None
                        if isinstance(candidate, str) and candidate:
                            run_id = candidate
                    except (ValueError, TypeError):
                        pass
            manifest = {"run_id": run_id, "plan_sha256": plan_sha,
                        "task_ids": [t["id"] for t in tasks], "initial_git_head": legacy_initial_head(repo, state),
                        "start_time_utc": state.get("created_utc"), "target_repo": str(repo)}
        events = EventLog(logs / "events.jsonl", run_id)
        events.emit("controller", "resume_started", "started")
        try:
            if state.get("pending_review") and state.get("pending_checkpoint"):
                raise PilotError("Conflicting review and approved checkpoints")
            if state.get("status") == "completed" and state["completed"] != [task["id"] for task in tasks]:
                raise PilotError("Completed state does not contain every prepared task")
            selected_branch = verify_resume_git(repo, state, manifest, tasks)
            legacy_branch = state.get("branch") is None or manifest.get("branch") is None
            state["branch"] = manifest["branch"] = selected_branch
            state["initial_git_head"] = manifest["initial_git_head"]
            state["process_ownership"] = 1
            if not manifest_path.exists() or legacy_branch:
                # The timeline and recorded Git history were validated first.
                state["run_id"] = run_id
                save_json(manifest_path, manifest)
                save_json(state_path, state)
            recover_checkpoint(repo, state, state_path, events)
            if state.get("pending_review"):
                pending_task = next((t for t in tasks if t["id"] == state["pending_review"].get("task")), None)
                if pending_task is None or state["pending_review"]["task"] in state["completed"]:
                    raise PilotError("Review-ready task is not pending in this plan")
                verify_review_ready(repo, logs, state, pending_task)
            if state["completed"] and not git_clean(repo):
                # Dirty changes are legitimate only when the unfinished task was attempted.
                if (not state.get("pending_review") and
                        not any(h["task"] not in state["completed"] for h in state["history"])):
                    raise PilotError("Unrecognized changes after last approved checkpoint")
        except (PilotError, OSError, KeyError, TypeError, ValueError) as exc:
            state["status"] = "stopped"
            try:
                save_json(state_path, state)
            finally:
                events.emit("controller", "stopped", "failed", evidence={"state": "state.json"},
                            metadata={"reason": "resume reconciliation failed", "error_type": type(exc).__name__})
            raise
        if legacy_branch:
            events.emit("controller", "legacy_branch_bound", "observed",
                        metadata={"branch": selected_branch, "reason": "existing Git evidence verified before binding"})
        events.emit("controller", "resume_reconciled", "success", metadata={"completed_tasks": list(state["completed"])})
        if state.get("status") == "completed" and not auto_push:
            require_run_branch(repo, state["branch"])
            print("Already completed; approved Git checkpoints verified; no Codex invocation needed")
            return 0
    else:
        if resume:
            raise PilotError("No existing state to resume")
        manifest_path = logs / "run-manifest.json"
        timeline_path = logs / "events.jsonl"
        created = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run_id = str(uuid.uuid4())
        # A prior initialization may have persisted the manifest or run_started
        # event but failed before state.json. Adopt that identity after validating
        # its plan/repository instead of making the next attempt unrecoverable.
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise PilotError("Run manifest must be an object")
            if (manifest.get("plan_sha256") != plan_sha or manifest.get("target_repo") != str(repo)
                    or manifest.get("task_ids") != [t["id"] for t in tasks]):
                raise PilotError("Existing run manifest does not match this plan or repository")
            run_id = manifest["run_id"]
            created = manifest.get("start_time_utc") or created
            if (manifest.get("branch") not in (None, selected_branch)
                    or manifest.get("initial_git_head") != head(repo)):
                raise PilotError("Initialization Git HEAD or branch changed from the saved manifest")
        elif timeline_path.exists():
            # EventLog validates every complete record and preserves any torn tail.
            data = timeline_path.read_bytes()
            first_line = next((line for line in data.splitlines() if line.strip()), b"")
            if first_line:
                try:
                    first = json.loads(first_line)
                    if isinstance(first, dict) and isinstance(first.get("run_id"), str):
                        run_id = first["run_id"]
                except (ValueError, TypeError):
                    pass
        if not git_clean(repo):
            # Make this safety exit visible when the external log store works.
            events = EventLog(timeline_path, run_id)
            if not events.seq:
                events.emit("controller", "run_started", "started")
            events.emit("controller", "stopped", "failed",
                        metadata={"reason": "dirty worktree preflight"})
            raise PilotError("New run requires a clean working tree; use a disposable project")
        initial_head = head(repo)
        state = {"repo": str(repo), "plan_sha256": plan_sha, "status": "initializing",
                 "completed": [], "current_task": None, "history": [], "pending_checkpoint": None,
                 "pending_review": None,
                 "created_utc": created, "run_id": run_id, "auto_push": auto_push, "branch": selected_branch,
                 "initial_git_head": initial_head, "process_ownership": 1}
        manifest = {"run_id": run_id, "plan_sha256": plan_sha,
                    "task_ids": [t["id"] for t in tasks], "initial_git_head": initial_head,
                    "start_time_utc": created, "target_repo": str(repo), "branch": selected_branch}
        if not manifest_path.exists():
            save_json(manifest_path, manifest)
        events = EventLog(timeline_path, run_id)
        if not events.seq:
            events.emit("controller", "run_started", "started", evidence={"manifest": "run-manifest.json"})
        state["status"] = "running"
        save_json(state_path, state)
    if not state.get("pending_review") and not publish_approved(repo, state, state_path, events):
        return 1
    events.emit("controller", "plan_validated", "success", metadata={"task_count": len(tasks), "plan_sha256": plan_sha})
    schema_path = logs / "review-schema.json"
    schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")
    for task in tasks:
        ident = task["id"]
        if ident in state["completed"]:
            continue
        prior = [h for h in state["history"] if h.get("task") == ident]
        state["current_task"] = ident
        if state.get("pending_review"):
            if state["pending_review"].get("task") != ident:
                raise PilotError("Review-ready task is out of sequence")
            outcome = review_ready_task(repo, logs, state, state_path, events, task,
                                        schema_path, execute, timeout, model_b,
                                        retry_review_once=retry_review_once)
            if outcome == "stopped":
                return 1
            if outcome == "approved":
                continue
            prior = [h for h in state["history"] if h.get("task") == ident]
        start = len(prior) + 1
        for attempt in range(start, attempts + 1):
            assert_test_process_safe(repo)
            assert_process_safe(repo)
            require_run_branch(repo, state["branch"])
            task_head = expected_run_head(state, manifest)
            if head(repo) != task_head:
                raise PilotError("Git HEAD changed from the approved task base")
            events.emit("controller", "task_started", "started", task_id=ident, attempt=attempt)
            print(f"[{ident}] Codex A — attempt {attempt}", flush=True)
            state["status"] = "running"
            save_json(state_path, state)
            base = f"{ident}-{attempt:02d}"
            developer_msg = ("Implement this task in the current Git repository. You may create the normal "
                 "source files you need INSIDE this repository. Do not change Git history, push, "
                 "read secrets, or modify files outside it. Run appropriate tests and fix code issues "
                 "before finishing. State exactly what you changed and tested.\n\nTASK:\n" + task["prompt"])
            feedback = correction_context([h for h in state["history"] if h.get("task") == ident])
            if feedback:
                developer_msg += "\n\nPREVIOUS FAILURE OR REVIEW FEEDBACK:\n" + feedback
            (logs / f"{base}-a-prompt.txt").write_text(developer_msg, encoding="utf-8")
            execution = events.emit("Codex A", "process_started", "started", task_id=ident, attempt=attempt,
                        evidence={"prompt": f"{base}-a-prompt.txt", "stream": f"{base}-a-events.jsonl", "answer": f"{base}-a-answer.txt"})
            answer_path = logs / f"{base}-a-answer.txt"
            execution_started = _execution_clock()
            try:
                result = execute(codex_argv(readonly=False, last_message=answer_path,
                                          schema=None, model=model_a), repo, timeout, prompt=developer_msg)
            except BaseException as exc:
                cleanup = getattr(exc, "cleanup_confirmed", None)
                if cleanup is not True and not (cleanup is None and getattr(exc, "process_started", None) is False):
                    record_process_uncertainty(state, state_path, "Codex A", execution)
                raise
            code, output, timed = result
            execution_finished = _execution_clock()
            uncertain = cleanup_unverified(result)
            if uncertain:
                record_process_uncertainty(state, state_path, "Codex A", execution)
            measurement = execution_metadata(result, execution["seq"], execution_started, finished=execution_finished)
            (logs / f"{base}-a-events.jsonl").write_text(output, encoding="utf-8")
            events.emit("Codex A", "process_finished", "cleanup_unverified" if uncertain else (
                        "timed_out" if timed else ("success" if code == 0 else "failed")), task_id=ident, attempt=attempt,
                        evidence={"stream": f"{base}-a-events.jsonl", "answer": f"{base}-a-answer.txt"},
                        metadata={"exit_code": code, "timed_out": timed}
                                 | measurement)
            record = {"task": ident, "attempt": attempt, "a_exit": code,
                      "a_timed_out": timed, "a_cleanup_confirmed": getattr(result, "cleanup_confirmed", None),
                      "a_process_started": getattr(result, "process_started", None), "tests": [], "review": None}
            if uncertain:
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt,
                            metadata={"reason": "Codex A process cleanup unverified", "execution_id": execution["seq"]})
                return 1
            if code != 0 or timed:
                record["feedback"] = "Codex A process failed or timed out; see events log"
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, evidence={"state": "state.json"}, metadata={"reason": "Codex A process failure"})
                print(f"STOPPED: Codex A process failed; logs: {logs}")
                return 1
            if head(repo) != task_head or git(repo, ["branch", "--show-current"]) != state["branch"]:
                record["feedback"] = "Git HEAD or branch changed during Codex A implementation"
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt,
                            metadata={"reason": "Git HEAD changed during implementation"})
                return 1
            failed = []
            if not files_overview(repo).strip():
                failed.append(("No changed or created project files were found after Codex A ran.", ""))
            tested_diff_sha = None
            if not failed:
                try:
                    require_run_branch(repo, state["branch"])
                    _, _, tested_diff_sha = staging_summary(repo)
                except PilotError as exc:
                    record["feedback"] = str(exc)
                    state["history"].append(record)
                    state["status"] = "stopped"
                    save_json(state_path, state)
                    events.emit("controller", "stopped", "failed", task_id=ident,
                                attempt=attempt, metadata={"reason": "checkpoint safety"})
                    return 1
            for index, cmd in enumerate(task["test_commands"], 1):
                require_run_branch(repo, state["branch"])
                if head(repo) != task_head:
                    raise PilotError("Git HEAD changed before controller verification")
                execution = events.emit("tests", "test_started", "started", task_id=ident, attempt=attempt, metadata={"index": index})
                print(f"[{ident}] Running test {index}/{len(task['test_commands'])}", flush=True)
                context = {"repo": str(repo), "logs": str(logs), "run_id": state["run_id"],
                           "task": ident, "attempt": attempt, "index": index, "execution_id": execution["seq"]}
                gate = TestProcessGate(repo, context)
                test_started = _execution_clock()
                try:
                    result = execute(cmd, repo, min(timeout, 300))
                except BaseException as exc:
                    cleanup = getattr(exc, "cleanup_confirmed", None)
                    if (cleanup is True or (cleanup is None and getattr(exc, "process_started", None) is False)):
                        gate.finish_confirmed()
                    else:
                        record_test_uncertainty(state, state_path, gate)
                        events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt,
                                    metadata={"reason": "test process cleanup unverified", "index": index,
                                              "execution_id": execution["seq"], "cleanup_confirmed": False})
                    raise
                code, output, timed = result
                test_finished = _execution_clock()
                cleanup = getattr(result, "cleanup_confirmed", None)
                uncertain = (cleanup is not None and cleanup is not True) or (timed and cleanup is not True)
                if uncertain:
                    # Persist before evidence/metrics I/O: a logging failure must
                    # not turn this result into a resumable code-correction attempt.
                    record_test_uncertainty(state, state_path, gate)
                else:
                    # Non-timeout legacy tuples represent completed executions;
                    # an explicit verified cleanup also permits normal handling.
                    gate.finish_confirmed()
                measurement = execution_metadata(result, execution["seq"], test_started,
                                                 codex=False, finished=test_finished)
                output_path = logs / f"{base}-test-{index}.txt"
                output_path.write_text(output, encoding="utf-8")
                record["tests"].append({"index": index, "exit_code": code, "timed_out": timed,
                                        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest()})
                events.emit("tests", "test_finished", "cleanup_unverified" if uncertain else (
                            "timed_out" if timed else ("success" if code == 0 else "failed")), task_id=ident, attempt=attempt,
                            evidence={"output": f"{base}-test-{index}.txt"},
                            metadata={"index": index, "exit_code": code, "timed_out": timed,
                                      "cleanup_confirmed": cleanup} | measurement)
                if uncertain:
                    events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt,
                                metadata={"reason": "test process cleanup unverified", "index": index,
                                          "execution_id": execution["seq"], "cleanup_confirmed": False})
                    return 1
                if code != 0 or timed:
                    # Preserve the existing repair feedback while omitting argv;
                    # event metadata contains only safe index/status details.
                    failed.append((f"TEST {index} failed (exit {code}); see {base}-test-{index}.txt",
                                   output[-MAX_TEST_FEEDBACK:]))
                    if timed:
                        break
            if failed:
                # Share the budget across failures so one noisy test cannot
                # erase another test's identity or useful error tail.
                per_failure = MAX_TEST_FEEDBACK // len(failed) - 2
                parts = []
                for heading, detail in failed:
                    marker = "\n[output tail; full output is saved]\n"
                    room = max(0, per_failure - len(heading) - len(marker))
                    parts.append(heading + (marker + detail[-room:] if room and detail else ""))
                feedback = "\n\n".join(parts)
                record["feedback"] = feedback
                state["history"].append(record)
                save_json(state_path, state)
                print(f"[{ident}] Tests failed; returning evidence to Codex A", flush=True)
                if any(x["timed_out"] for x in record["tests"]):
                    state["status"] = "stopped"
                    save_json(state_path, state)
                    events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, metadata={"reason": "test timeout"})
                    print("STOPPED: test timeout (not a code-repair verdict)")
                    return 1
                events.emit("controller", "correction_requested", "retry", task_id=ident, attempt=attempt,
                            evidence={"test_output": [f"{base}-test-{i}.txt" for i in range(1, len(record["tests"])+1)]})
                continue
            try:
                require_run_branch(repo, state["branch"])
                stat, diff, diff_sha = staging_summary(repo)
                if (head(repo) != task_head or tested_diff_sha != diff_sha
                        or not git_clean_staged(repo)):
                    raise PilotError("Implementation changed during tests; rerun tests on the final staged diff")
            except PilotError as exc:
                record["feedback"] = str(exc)
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, metadata={"reason": "checkpoint safety"})
                print(f"STOPPED: change checkpoint safety: {exc}")
                return 1
            (logs / f"{base}-diff.txt").write_text(diff, encoding="utf-8")
            state["pending_review"] = {"task": ident, "attempt": attempt,
                                       "repo": str(repo), "plan_sha256": plan_sha,
                                        "branch": state["branch"],
                                       "base_head": task_head, "diff_sha256": diff_sha,
                                       "record": record, "review_invocations": 0,
                                       "review_invocation_limit": MAX_REVIEW_PROCESS_ATTEMPTS}
            state["status"] = "stopped"
            save_json(state_path, state)
            outcome = review_ready_task(repo, logs, state, state_path, events, task,
                                        schema_path, execute, timeout, model_b)
            if outcome == "stopped":
                return 1
            if outcome == "approved":
                break
        else:
            state["status"] = "stopped"
            save_json(state_path, state)
            events.emit("controller", "stopped", "failed", task_id=ident, metadata={"reason": "correction attempt limit"})
            print(f"STOPPED: correction attempt limit for {ident}. Logs: {logs}")
            return 1
    require_run_branch(repo, state["branch"])
    if head(repo) != expected_run_head(state, manifest):
        raise PilotError("Git HEAD changed before run completion")
    state["status"] = "completed"
    state["current_task"] = None
    save_json(state_path, state)
    events.emit("controller", "run_completed", "success", evidence={"state": "state.json"})
    print(f"COMPLETED: {len(tasks)} task(s). " + ("Approved checkpoints remotely confirmed. " if auto_push else "Local checkpoints saved; NO PUSH. ") + f"Logs: {logs}")
    return 0


@managed_job
def run(plan: Path, repo: Path, logs: Path, *, create_repo=False, resume=False, attempts=3,
        model_a=None, model_b=None, timeout=1800, execute: Callable = call, auto_push=None,
        retry_review_once=False) -> int:
    """Run while ensuring controlled exceptions after log creation are recorded."""
    try:
        return _run(plan, repo, logs, create_repo=create_repo, resume=resume,
                    attempts=attempts, model_a=model_a, model_b=model_b,
                    timeout=timeout, execute=execute, auto_push=auto_push,
                    retry_review_once=retry_review_once)
    except Exception as exc:
        log_dir = logs.resolve(strict=False)
        state_path = log_dir / "state.json"
        manifest_path = log_dir / "run-manifest.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
            run_id = manifest.get("run_id") or state.get("run_id")
            timeline_path = log_dir / "events.jsonl"
            # During first initialization there may be no manifest or state yet.
            # Adopt a validated existing timeline identity, or establish a fresh
            # one so controlled preflight and manifest failures can be recorded.
            if not run_id and timeline_path.exists():
                first_line = next((line for line in timeline_path.read_bytes().splitlines() if line.strip()), b"")
                if first_line:
                    first = json.loads(first_line)
                    if isinstance(first, dict) and isinstance(first.get("run_id"), str):
                        run_id = first["run_id"]
            if not run_id and not state_path.exists():
                run_id = str(uuid.uuid4())
            if run_id:
                timeline = EventLog(timeline_path, run_id)
                # Preserve checkpointing when a commit may have succeeded; resume
                # reconciliation needs that state to avoid replaying an approved task.
                if state and state.get("status") != "checkpointing":
                    state["status"] = "stopped"
                    save_json(state_path, state)
                last = None
                if timeline_path.exists():
                    lines = timeline_path.read_bytes().splitlines()
                    if lines:
                        try:
                            last = json.loads(lines[-1])
                        except (ValueError, TypeError):
                            pass
                if not last or last.get("event") != "stopped":
                    timeline.emit("controller", "stopped", "failed",
                                  task_id=state.get("current_task"),
                                  evidence={"state": "state.json"} if state else None,
                                  metadata={"reason": "controlled failure", "error_type": type(exc).__name__})
            else:
                print(f"FATAL: logging initialization failed before a timeline could be established: {type(exc).__name__}: {exc}", file=sys.stderr)
        except (OSError, ValueError, EventLogError):
            # Storage may be unavailable; make the failure explicit and never
            # claim a durable stopped event that could not be written.
            print(f"FATAL: could not persist failure evidence: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    finally:
        # A derived summary has no authority over execution or approval. Failure
        # to export it must not mask the original process/cleanup exception.
        try:
            log_dir, project = logs.resolve(strict=False), repo.resolve(strict=False)
            if log_dir != project and not log_dir.is_relative_to(project):
                write_metrics(log_dir)
        except Exception as exc:
            print(f"WARNING: metrics summary unavailable: {type(exc).__name__}", file=sys.stderr)


def git_clean_staged(repo: Path) -> bool:
    # No unreviewed changes outside the staged diff and no additional untracked files.
    return (not git(repo, ["diff", "--name-only"])
            and not git(repo, ["ls-files", "--others", "--exclude-standard"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simple Codex A/B coordinator (local checkpoints; explicit opt-in push)")
    parser.add_argument("plan", type=Path, help="TXT, MD, or simple JSON task file")
    parser.add_argument("--repo", required=True, type=Path, help="Git repository to modify")
    parser.add_argument("--logs", required=True, type=Path, help="Output logs, outside repository")
    parser.add_argument("--create-repo", action="store_true", help="Initialize an empty disposable repository")
    parser.add_argument("--resume", action="store_true", help="Resume a stopped run with same task file")
    parser.add_argument("--retry-review-once", action="store_true",
                        help="With --resume, authorize one additional Codex B call after its saved budget is exhausted")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--model-a")
    parser.add_argument("--model-b")
    parser.add_argument("--auto-push-repository", help="Explicitly enable auto-push to this GitHub OWNER/REPO")
    parser.add_argument("--auto-push-branch", help="Fixed origin feature branch for approved checkpoints")
    args = parser.parse_args(argv)
    if args.retry_review_once and not args.resume:
        cancel_reservation(args.repo, os.environ.get("AUTOCODING_JOB_RESERVATION"))
        parser.error("--retry-review-once requires --resume")
    if bool(args.auto_push_repository) != bool(args.auto_push_branch):
        cancel_reservation(args.repo, os.environ.get("AUTOCODING_JOB_RESERVATION"))
        parser.error("Auto-push requires both repository and feature branch")
    if not 1 <= args.attempts <= 10 or not 30 <= args.timeout <= 86400:
        cancel_reservation(args.repo, os.environ.get("AUTOCODING_JOB_RESERVATION"))
        parser.error("invalid attempts or timeout")
    if shutil.which("codex") is None:
        cancel_reservation(args.repo, os.environ.get("AUTOCODING_JOB_RESERVATION"))
        parser.error("codex command not found in PATH")
    try:
        return run(args.plan, args.repo, args.logs, create_repo=args.create_repo,
                   resume=args.resume, attempts=args.attempts, timeout=args.timeout,
                    retry_review_once=args.retry_review_once,
                   model_a=args.model_a, model_b=args.model_b,
                   auto_push=({"repository": normalize_repository(args.auto_push_repository)[0],
                               "branch": args.auto_push_branch} if args.auto_push_repository else None))
    except (PilotError, WorkspaceError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Only an unclaimed, matching reservation can be released here. A
        # claimed or uncertain runner's gate is deliberately left untouched.
        cancel_reservation(args.repo, os.environ.get("AUTOCODING_JOB_RESERVATION"))
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
