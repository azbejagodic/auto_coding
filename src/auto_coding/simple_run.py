"""Opt-in Codex A -> tests -> Codex B coordinator (local-only pilot v2.1).

Uses task-specific local Git checkpoints and durable logs. No pushes, no
file-by-file implementation planning, no mutation of the existing controller.
"""
from __future__ import annotations

import argparse
import hashlib
import datetime
import json
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


MAX_PLAN = 512 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVED", "REJECTED"]},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "summary": {"type": "string"},
    },
    "required": ["decision", "issues", "summary"],
    "additionalProperties": False,
}


class PilotError(Exception):
    pass


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
    """Capture all tool output for a diagnostic bundle; no shell execution."""
    try:
        result = subprocess.run(argv, cwd=str(cwd), input=prompt, text=True,
                                encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False, shell=False)
        return result.returncode, (result.stdout or "")[:MAX_OUTPUT], False
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, str(output)[-MAX_OUTPUT:] + "\nPROCESS TIMED OUT", True
    except OSError as exc:
        return 127, f"Process could not start: {type(exc).__name__}", False


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
            created = call(["git", "init"], repo, 30)
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


def head(repo: Path) -> str | None:
    rc, output, timed = call(["git", "rev-parse", "--verify", "HEAD"], repo, 15)
    return output.strip() if rc == 0 and not timed else None


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


def staging_summary(repo: Path) -> tuple[str, str, str]:
    """Stage project changes only; omit build caches, never omit secret files."""
    ignore_generated_locally(repo)
    git(repo, ["add", "-A"])
    paths = [p for p in git(repo, ["diff", "--cached", "--name-only", "-z"]).split("\0") if p]
    for item in paths:
        if is_generated_path(item):
            # Handle the previous V2 failure, where a cache file is already staged.
            # Never silently unstage a file already present in a previous commit.
            if head(repo) is not None:
                exists, _, _ = call(["git", "cat-file", "-e", f"HEAD:{item}"], repo, 15)
                if exists == 0:
                    raise PilotError(f"Previously tracked generated file changed: {item}; inspect manually")
            git(repo, ["rm", "-q", "-f", "--cached", "--", item])
    paths = [p for p in git(repo, ["diff", "--cached", "--name-only", "-z"]).split("\0") if p]
    if not paths:
        raise PilotError("No changed or created project files in this task")
    for item in paths:
        components = item.replace("\\", "/").lower().split("/")
        base = components[-1]
        if (any(c in {".git", ".ssh"} for c in components)
                or base in {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json"}
                or base.endswith((".pem", ".p12", ".pfx", ".key"))):
            raise PilotError(f"Sensitive file staged: {item}; inspect manually")
    # Diff is used for both the reviewer and an approval-to-commit integrity check.
    diff = git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])
    diff_hash = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()
    stat = git(repo, ["diff", "--cached", "--stat"])
    return stat, diff, diff_hash


def checked_review(verdict_path: Path) -> dict:
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        if (not isinstance(verdict, dict) or set(verdict) != {"decision", "issues", "summary"}
                or verdict["decision"] not in ("APPROVED", "REJECTED")
                or not isinstance(verdict["issues"], list) or len(verdict["issues"]) > 20
                or any(not isinstance(x, str) for x in verdict["issues"])
                or not isinstance(verdict["summary"], str)):
            raise ValueError("Invalid verdict")
        return verdict
    except (ValueError, OSError) as exc:
        raise PilotError("Invalid or missing Codex B verdict; see event logs") from exc


def local_commit(repo: Path, ident: str) -> str:
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


def verify_approved_commit(repo: Path, pending: dict, current: str | None) -> None:
    """Accept only the exact approved patch on the exact recorded parent.

    A matching commit subject is not proof of approval. Check both normal
    commits and crash recovery before reporting a checkpoint as committed.
    """
    if not current or not git_clean(repo):
        raise PilotError("Checkpoint state ambiguous: inspect repository and logs")
    message = git(repo, ["log", "-1", "--format=%s"])
    lineage = git(repo, ["rev-list", "--parents", "-n", "1", "HEAD"]).split()
    base = pending.get("base_head")
    expected_lineage = [current, base] if base else [current]
    committed_diff = git_patch(repo, ["show", "--format=", "--binary", "--no-ext-diff", "HEAD"])
    committed_sha256 = hashlib.sha256(committed_diff.encode("utf-8", errors="replace")).hexdigest()
    if (message != f"auto-coding: {pending['task']}" or lineage != expected_lineage
            or committed_sha256 != pending.get("diff_sha256")):
        raise PilotError("Recovered checkpoint does not match the approved parent and diff; inspect manually")


def recover_checkpoint(repo: Path, state: dict, state_path: Path, events: EventLog | None = None) -> None:
    """Reconcile the tiny window between successful commit and state save."""
    pending = state.get("pending_checkpoint")
    if not pending:
        return
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
        state["status"] = "running"
        save_json(state_path, state)
        return
    # Approval exists but a local commit hasn't completed. Only permit recovery
    # when the staged bytes still match exactly what Codex B approved.
    if git(repo, ["diff", "--name-only"]).strip() or git(repo, ["ls-files", "--others", "--exclude-standard"]).strip():
        raise PilotError("Unreviewed edits after approval: inspect before resume")
    diff = git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])
    if hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest() != pending["diff_sha256"]:
        raise PilotError("Approved diff changed before checkpoint")
    if events:
        events.emit("Git", "checkpoint_started", "started", task_id=task,
                    attempt=pending["record"].get("attempt"), metadata={"recovered": True})
    try:
        committed = local_commit(repo, task)
    except PilotError:
        if events:
            events.emit("Git", "checkpoint_stopped", "failed", task_id=task,
                        attempt=pending["record"].get("attempt"), metadata={"reason": "checkpoint recovery failed"})
        raise
    verify_approved_commit(repo, pending, committed)
    if events:
        events.emit("Git", "checkpoint_committed", "success", task_id=task,
                    attempt=pending["record"].get("attempt"), metadata={"commit": committed, "recovered": True})
    state["completed"].append(task)
    state["history"].append(pending["record"] | {"commit": committed})
    state["pending_checkpoint"] = None
    state["status"] = "running"
    save_json(state_path, state)


def _run(plan: Path, repo: Path, logs: Path, *, create_repo=False, resume=False, attempts=3,
        model_a=None, model_b=None, timeout=1800,
        execute: Callable = call) -> int:
    tasks, plan_sha = load_tasks(plan)
    repo = safe_repo(repo, create_repo)
    logs = logs.resolve(strict=False)
    if logs == repo or logs.is_relative_to(repo):
        raise PilotError("Log directory must be outside the target project")
    logs.mkdir(parents=True, exist_ok=True)
    state_path = logs / "state.json"
    if state_path.exists():
        if not resume:
            raise PilotError("Existing run logs; use --resume (never overwrite a run)")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("plan_sha256") != plan_sha or state.get("repo") != str(repo):
            raise PilotError("Resume plan or repository mismatch")
        if state.get("status") == "completed":
            print("Already completed; no Codex invocation needed")
            return 0
        if state.get("status") not in {"stopped", "checkpointing"}:
            raise PilotError("Previous run may still be active or interrupted; inspect before resuming")
        manifest_path = logs / "run-manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
                        "task_ids": [t["id"] for t in tasks], "initial_git_head": head(repo),
                        "start_time_utc": state.get("created_utc"), "target_repo": str(repo)}
        events = EventLog(logs / "events.jsonl", run_id)
        if not manifest_path.exists():
            # Write only after the existing timeline has been validated.
            state["run_id"] = run_id
            save_json(manifest_path, manifest)
            save_json(state_path, state)
        events.emit("controller", "resume_started", "started")
        try:
            recover_checkpoint(repo, state, state_path, events)
            if state["completed"] and not git_clean(repo):
                # Dirty changes are legitimate only when the unfinished task was attempted.
                if not any(h["task"] not in state["completed"] for h in state["history"]):
                    raise PilotError("Unrecognized changes after last approved checkpoint")
        except (PilotError, OSError, KeyError, TypeError, ValueError) as exc:
            state["status"] = "stopped"
            try:
                save_json(state_path, state)
            finally:
                events.emit("controller", "stopped", "failed", evidence={"state": "state.json"},
                            metadata={"reason": "resume reconciliation failed", "error_type": type(exc).__name__})
            raise
        events.emit("controller", "resume_reconciled", "success", metadata={"completed_tasks": list(state["completed"])})
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
            if (manifest.get("plan_sha256") != plan_sha or manifest.get("target_repo") != str(repo)
                    or manifest.get("task_ids") != [t["id"] for t in tasks]):
                raise PilotError("Existing run manifest does not match this plan or repository")
            run_id = manifest["run_id"]
            created = manifest.get("start_time_utc") or created
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
        state = {"repo": str(repo), "plan_sha256": plan_sha, "status": "initializing",
                 "completed": [], "current_task": None, "history": [], "pending_checkpoint": None,
                 "created_utc": created, "run_id": run_id}
        manifest = {"run_id": run_id, "plan_sha256": plan_sha,
                    "task_ids": [t["id"] for t in tasks], "initial_git_head": head(repo),
                    "start_time_utc": created, "target_repo": str(repo)}
        if not manifest_path.exists():
            save_json(manifest_path, manifest)
        events = EventLog(timeline_path, run_id)
        if not events.seq:
            events.emit("controller", "run_started", "started", evidence={"manifest": "run-manifest.json"})
        state["status"] = "running"
        save_json(state_path, state)
    events.emit("controller", "plan_validated", "success", metadata={"task_count": len(tasks), "plan_sha256": plan_sha})
    schema_path = logs / "review-schema.json"
    if not schema_path.exists():
        schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")
    for task in tasks:
        ident = task["id"]
        if ident in state["completed"]:
            continue
        prior = [h for h in state["history"] if h.get("task") == ident]
        feedback = (prior[-1].get("feedback") if prior else "") or ""
        state["current_task"] = ident
        start = len(prior) + 1
        for attempt in range(start, attempts + 1):
            events.emit("controller", "task_started", "started", task_id=ident, attempt=attempt)
            print(f"[{ident}] Codex A — attempt {attempt}", flush=True)
            state["status"] = "running"
            save_json(state_path, state)
            base = f"{ident}-{attempt:02d}"
            developer_msg = ("Implement this task in the current Git repository. You may create the normal "
                 "source files you need INSIDE this repository. Do not change Git history, push, "
                 "read secrets, or modify files outside it. Run appropriate tests and fix code issues "
                 "before finishing. State exactly what you changed and tested.\n\nTASK:\n" + task["prompt"])
            if feedback:
                developer_msg += "\n\nPREVIOUS FAILURE OR REVIEW FEEDBACK:\n" + feedback[-14000:]
            (logs / f"{base}-a-prompt.txt").write_text(developer_msg, encoding="utf-8")
            events.emit("Codex A", "process_started", "started", task_id=ident, attempt=attempt,
                        evidence={"prompt": f"{base}-a-prompt.txt", "stream": f"{base}-a-events.jsonl", "answer": f"{base}-a-answer.txt"})
            answer_path = logs / f"{base}-a-answer.txt"
            code, output, timed = execute(codex_argv(readonly=False, last_message=answer_path,
                                                      schema=None, model=model_a), repo, timeout, prompt=developer_msg)
            (logs / f"{base}-a-events.jsonl").write_text(output, encoding="utf-8")
            events.emit("Codex A", "process_finished", "timed_out" if timed else ("success" if code == 0 else "failed"), task_id=ident, attempt=attempt,
                        evidence={"stream": f"{base}-a-events.jsonl", "answer": f"{base}-a-answer.txt"}, metadata={"exit_code": code, "timed_out": timed})
            record = {"task": ident, "attempt": attempt, "a_exit": code,
                      "a_timed_out": timed, "tests": [], "review": None}
            if code != 0 or timed:
                record["feedback"] = "Codex A process failed or timed out; see events log"
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, evidence={"state": "state.json"}, metadata={"reason": "Codex A process failure"})
                print(f"STOPPED: Codex A process failed; logs: {logs}")
                return 1
            failed = []
            if not files_overview(repo).strip():
                failed.append("No changed or created project files were found after Codex A ran.")
            for index, cmd in enumerate(task["test_commands"], 1):
                events.emit("tests", "test_started", "started", task_id=ident, attempt=attempt, metadata={"index": index})
                print(f"[{ident}] Running test {index}/{len(task['test_commands'])}", flush=True)
                test_started = time.monotonic()
                code, output, timed = execute(cmd, repo, min(timeout, 300))
                test_duration = round(time.monotonic() - test_started, 3)
                (logs / f"{base}-test-{index}.txt").write_text(output, encoding="utf-8")
                record["tests"].append({"index": index, "exit_code": code, "timed_out": timed})
                events.emit("tests", "test_finished", "timed_out" if timed else ("success" if code == 0 else "failed"), task_id=ident, attempt=attempt,
                            evidence={"output": f"{base}-test-{index}.txt"}, metadata={"index": index, "exit_code": code, "timed_out": timed, "duration_seconds": test_duration})
                if code != 0 or timed:
                    # Preserve the existing repair feedback while omitting argv;
                    # event metadata contains only safe index/status details.
                    failed.append(f"TEST {index} failed (exit {code}); see {base}-test-{index}.txt\n{output[-12000:]}")
                    if timed:
                        break
            if failed:
                feedback = "\n\n".join(failed)
                record["feedback"] = feedback[-14000:]
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
                stat, diff, diff_sha = staging_summary(repo)
            except PilotError as exc:
                record["feedback"] = str(exc)
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, metadata={"reason": "checkpoint safety"})
                print(f"STOPPED: change checkpoint safety: {exc}")
                return 1
            (logs / f"{base}-diff.txt").write_text(diff, encoding="utf-8")
            print(f"[{ident}] Codex B — independent review", flush=True)
            review_prompt = ("You are Codex B, an independent READ-ONLY reviewer. Inspect the current "
                "task's STAGED diff, all relevant project files, and tests. Previous tasks are locally "
                "committed. Do not blindly trust implementer claims or test results; reject with "
                "specific reproducible issues. Do not change files.\n\nTASK:\n" + task["prompt"] +
                "\n\nCURRENT TASK CHANGES:\n" + stat + "\n\nDIFF (truncated; inspect repository for full):\n" + diff[:35000] +
                "\n\nTEST RESULTS:\n" + json.dumps(record["tests"]))
            (logs / f"{base}-b-prompt.txt").write_text(review_prompt, encoding="utf-8")
            events.emit("Codex B", "review_started", "started", task_id=ident, attempt=attempt,
                        evidence={"prompt": f"{base}-b-prompt.txt", "diff": f"{base}-diff.txt", "stream": f"{base}-b-events.jsonl", "verdict": f"{base}-b-verdict.json"})
            verdict_path = logs / f"{base}-b-verdict.json"
            code, output, timed = execute(codex_argv(readonly=True, last_message=verdict_path,
                                                      schema=schema_path, model=model_b),
                                          repo, timeout, prompt=review_prompt)
            (logs / f"{base}-b-events.jsonl").write_text(output, encoding="utf-8")
            events.emit("Codex B", "review_process_finished", "timed_out" if timed else ("success" if code == 0 else "failed"), task_id=ident, attempt=attempt,
                        evidence={"stream": f"{base}-b-events.jsonl"}, metadata={"exit_code": code, "timed_out": timed})
            if code != 0 or timed:
                record["feedback"] = "Codex B process failure or timeout; see logs"
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, metadata={"reason": "Codex B process failure"})
                print("STOPPED: reviewer process failure; see logs")
                return 1
            try:
                verdict = checked_review(verdict_path)
            except PilotError as exc:
                record["feedback"] = str(exc)
                state["history"].append(record)
                state["status"] = "stopped"
                save_json(state_path, state)
                events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, evidence={"stream": f"{base}-b-events.jsonl"}, metadata={"reason": "invalid review verdict"})
                print(f"STOPPED: {exc}")
                return 1
            record["review"] = verdict
            events.emit("Codex B", "review_finished", verdict["decision"].lower(), task_id=ident, attempt=attempt,
                        evidence={"verdict": f"{base}-b-verdict.json"})
            if verdict["decision"] == "APPROVED" and not verdict["issues"]:
                # Prevent changed or unstaged bytes from slipping in after review.
                if (not git_clean_staged(repo) or
                        hashlib.sha256(git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"]).encode("utf-8", errors="replace")).hexdigest() != diff_sha):
                    record["feedback"] = "Project changed after review; inspect before checkpoint"
                    state["history"].append(record)
                    state["status"] = "stopped"
                    save_json(state_path, state)
                    events.emit("controller", "stopped", "failed", task_id=ident, attempt=attempt, metadata={"reason": "worktree changed after review"})
                    print("STOPPED: worktree changed after Codex B review")
                    return 1
                state["pending_checkpoint"] = {"task": ident, "base_head": head(repo),
                                                "diff_sha256": diff_sha, "record": record}
                state["status"] = "checkpointing"
                save_json(state_path, state)
                events.emit("Git", "checkpoint_started", "started", task_id=ident, attempt=attempt, evidence={"diff": f"{base}-diff.txt"})
                try:
                    commit = local_commit(repo, ident)
                    verify_approved_commit(repo, state["pending_checkpoint"], commit)
                except PilotError as exc:
                    events.emit("Git", "stopped", "failed", task_id=ident, attempt=attempt,
                                evidence={"state": "state.json"}, metadata={"reason": "checkpoint pending reconciliation", "error_type": type(exc).__name__})
                    print(f"STOPPED: local checkpoint failed: {exc}")
                    return 1
                events.emit("Git", "checkpoint_committed", "success", task_id=ident, attempt=attempt, metadata={"commit": commit})
                state["completed"].append(ident)
                state["history"].append(record | {"commit": commit})
                state["pending_checkpoint"] = None
                state["status"] = "running"
                save_json(state_path, state)
                events.emit("controller", "task_completed", "success", task_id=ident, attempt=attempt, metadata={"commit": commit})
                print(f"[{ident}] APPROVED — saved local checkpoint {commit[:8]}", flush=True)
                break
            feedback = "Codex B rejected:\n" + "\n".join(verdict["issues"])
            record["feedback"] = feedback[-14000:]
            state["history"].append(record)
            save_json(state_path, state)
            events.emit("controller", "correction_requested", "retry", task_id=ident, attempt=attempt, evidence={"verdict": f"{base}-b-verdict.json"})
            print(f"[{ident}] Rejected; returning Codex B feedback to Codex A", flush=True)
        else:
            state["status"] = "stopped"
            save_json(state_path, state)
            events.emit("controller", "stopped", "failed", task_id=ident, metadata={"reason": "correction attempt limit"})
            print(f"STOPPED: correction attempt limit for {ident}. Logs: {logs}")
            return 1
    state["status"] = "completed"
    state["current_task"] = None
    save_json(state_path, state)
    events.emit("controller", "run_completed", "success", evidence={"state": "state.json"})
    print(f"COMPLETED: {len(tasks)} task(s). Local checkpoints saved; NO PUSH. Logs: {logs}")
    return 0


def run(plan: Path, repo: Path, logs: Path, *, create_repo=False, resume=False, attempts=3,
        model_a=None, model_b=None, timeout=1800, execute: Callable = call) -> int:
    """Run while ensuring controlled exceptions after log creation are recorded."""
    try:
        return _run(plan, repo, logs, create_repo=create_repo, resume=resume,
                    attempts=attempts, model_a=model_a, model_b=model_b,
                    timeout=timeout, execute=execute)
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


def git_clean_staged(repo: Path) -> bool:
    # No unreviewed changes outside the staged diff and no additional untracked files.
    return (not git(repo, ["diff", "--name-only"])
            and not git(repo, ["ls-files", "--others", "--exclude-standard"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simple Codex A/B coordinator v2.1 (local checkpoints, no push)")
    parser.add_argument("plan", type=Path, help="TXT, MD, or simple JSON task file")
    parser.add_argument("--repo", required=True, type=Path, help="Git repository to modify")
    parser.add_argument("--logs", required=True, type=Path, help="Output logs, outside repository")
    parser.add_argument("--create-repo", action="store_true", help="Initialize an empty disposable repository")
    parser.add_argument("--resume", action="store_true", help="Resume a stopped run with same task file")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--model-a")
    parser.add_argument("--model-b")
    args = parser.parse_args(argv)
    if not 1 <= args.attempts <= 10 or not 30 <= args.timeout <= 86400:
        parser.error("invalid attempts or timeout")
    if shutil.which("codex") is None:
        parser.error("codex command not found in PATH")
    try:
        return run(args.plan, args.repo, args.logs, create_repo=args.create_repo,
                   resume=args.resume, attempts=args.attempts, timeout=args.timeout,
                   model_a=args.model_a, model_b=args.model_b)
    except (PilotError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
