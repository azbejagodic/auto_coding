"""Small process controller for the existing local-only simple_run CLI.

Default run storage is ``LocalAppData/AutoCoding/runs``. Closing the desktop window leaves
the child process running; it is never silently terminated. Reopen a run by
selecting its directory and use poll()/snapshot() to observe it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from subprocess import Popen
import sys
import threading
import uuid

from auto_coding.desktop_reader import read_run
from auto_coding.simple_run import PilotError, load_tasks
from auto_coding.github_workspace import (GitHubWorkspaceManager, storage_root, serialized_launch,
    managed_lock, reserve_job, cancel_reservation)
from auto_coding.process_runner import bounded_run
from auto_coding.repository_activity import repository_busy
from auto_coding.launch_preflight import launch_preflight, validate_repository_root
from auto_coding import run_lifecycle
from auto_coding.run_launcher import launch_runner
from auto_coding import plan_audit, simple_run
from auto_coding.reviewer_permissions import check_audit_model


class DesktopController:
    def __init__(self, runs_dir: str | Path | None = None):
        self.runs_dir = Path(runs_dir or (storage_root() / "runs")).expanduser().resolve()
        self.repo: Path | None = None
        self.repository = None
        self.plan: Path | None = None
        self.logs: Path | None = None
        self.process = None
        self._detached_pid: int | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self._launch_uncertain = False
        self._metadata_error = None
        self.process_history = []
        self._lock = threading.Lock()

    def checkout_busy(self, repo) -> bool:
        """Fail closed for a matching checkout's active or ambiguous run records."""
        return repository_busy(repo, self.runs_dir, selected_repo=self.repo,
                               selected_logs=self.logs, selected_activity=self._selected_checkout_busy)

    def _selected_checkout_busy(self, repo) -> bool:
        """Adapt this controller's live state to the headless activity checker."""
        if self._launch_uncertain or self._selected_run_unknown() or self._selected_run_active():
            return True
        if self.process is not None and self.process.poll() is None:
            return True
        if self.logs:
            state = read_run(self.logs).get("state")
            if state and state.get("repo") == str(repo) and state.get("status") not in {"stopped", "completed"}:
                return True
        return False

    def _workspace_manager(self):
        manager = GitHubWorkspaceManager(active_check=self.checkout_busy)
        manager.runs_dir = self.runs_dir
        return manager

    def github(self, action, repository=None, **kwargs):
        try:
            if action not in {"list_repositories", "connect", "branches", "fetch", "create_branch", "checkout_branch", "push_eligibility", "push_approved"}:
                raise ValueError("Unsupported workspace operation.")
            manager = self._workspace_manager()
            if action == "list_repositories":
                return {"ok": True, **manager.list_repositories()}
            if action not in {"branches", "push_eligibility"}:
                status = self.job_status()
                if status["active"] is not False or status["uncertain"]:
                    raise ValueError("Cannot change projects while a job is active or uncertain.")
            if action != "connect" and repository != self.repository:
                raise ValueError("Connect this repository before changing its branches or publishing.")
            if action.startswith("push_"):
                kwargs["approved"] = self._approved_checkpoints()
            result = getattr(manager, action)(repository, **kwargs)
            if action == "connect":
                self.repo = Path(result["path"])
                self.repository = result["repository"]
            return {"ok": True, **result}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "code": getattr(exc, "code", "workspace")}

    def _approved_checkpoints(self):
        approved = {}
        paths = list(self.runs_dir.glob("run-*/state.json"))
        if self.logs and self.logs / "state.json" not in paths:
            paths.append(self.logs / "state.json")
        for path in paths:
            try:
                if path.is_symlink():
                    continue
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("repo") == str(self.repo) and state.get("status") in {"completed", "stopped"} and not state.get("pending_checkpoint"):
                    for record in state.get("history", []):
                        if isinstance(record, dict) and isinstance(record.get("commit"), str):
                            approved[record["commit"]] = record
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return approved

    def select_repo(self, path: str | Path) -> dict:
        try:
            repo = validate_repository_root(path, run_command=bounded_run)
            self.repo = repo
            self.repository = None
            self.error = None
            return {"ok": True, "path": str(repo), "error": None}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.repo = None
            self.repository = None
            self.error = f"Repository validation failed: {exc}"
            return {"ok": False, "path": None, "error": self.error}

    def select_plan(self, path: str | Path) -> dict:
        try:
            plan = Path(path).expanduser().resolve(strict=True)
            if not plan.is_file() or plan.suffix.lower() not in {".json", ".txt", ".md"}:
                raise ValueError("Select an existing .json, .txt, or .md plan file.")
            load_tasks(plan)
            self.plan = plan
            self.error = None
            return {"ok": True, "path": str(plan), "error": None}
        except (OSError, UnicodeError, ValueError, PilotError, json.JSONDecodeError) as exc:
            self.plan = None
            self.error = f"Plan validation failed: {exc}"
            return {"ok": False, "path": None, "error": self.error}

    def select_logs(self, path: str | Path) -> dict:
        try:
            logs = Path(path).expanduser().resolve(strict=True)
            if not logs.is_dir(): raise ValueError("Select an existing run logs directory.")
            self.logs = logs
            return {"ok": True, "path": str(logs), "error": None}
        except (OSError, ValueError) as exc:
            self.logs = None
            self.error = f"Run logs selection failed: {exc}"
            return {"ok": False, "path": None, "error": self.error}

    def preflight(self) -> dict:
        """Read-only Git checks for display and launch confirmation."""
        metadata = self._read_metadata()
        result = launch_preflight(self.repo, self.runs_dir, run_command=bounded_run,
                                  other_active_run=self._other_active_run)
        if metadata and (metadata.get("repo") != str(self.repo) or metadata.get("plan") != str(self.plan)):
            result["resume_error"] = "Resume requires the original exact repository and plan paths saved in desktop-run.json."
        return result

    def _validate(self):
        if self.repo is None or self.plan is None:
            raise ValueError("Select and validate a repository and plan first.")
        # Revalidate plan on launch; never trust stale UI validation.
        load_tasks(self.plan)
        check = self.preflight()
        if check["errors"]: raise ValueError(" ".join(check["errors"]))
        if check["clean"] is not True: raise ValueError("Working tree has staged or unstaged changes. Commit or discard them, then use a feature branch.")

    @serialized_launch
    def start_audit(self, model: str, goal: str = "", timeout: int = 300) -> dict:
        """Launch the CLI audit only. Its ledger remains the sole verdict authority."""
        with self._lock:
            try:
                status = self.poll()
                if status["active"] is not False or status["uncertain"]:
                    raise ValueError("A job is active or uncertain; inspect its logs before launching an audit.")
                self._validate()
                tasks, _, identity, config = plan_audit.audit_inputs(
                    simple_run, self.plan, self.repo, model=model, goal=goal, timeout=timeout,
                    controller_tests=False)
                ledger = plan_audit.ledger_path(self.repo, identity)
                if ledger.exists():
                    prior = plan_audit.read_json(ledger)
                    if prior.get("status") != "completed":
                        raise ValueError("This one-shot audit is consumed or uncertain; inspect saved diagnostics. It cannot be repeated.")
                    directory = Path(prior["directory"])
                    evidence, _ = plan_audit.load_evidence(simple_run, directory, self.repo,
                        identity["plan_sha256"], [task["id"] for task in tasks])
                    if evidence["identity"] != identity:
                        raise ValueError("Existing audit identity differs from the selected inputs.")
                    self.logs = directory
                    return {"ok": True, "reused": True, "audit": self.audit_status()}
                if self.runs_dir == self.repo or self.runs_dir.is_relative_to(self.repo):
                    raise ValueError("Application run storage must be outside the target repository.")
                if self.repo == Path(simple_run.__file__).resolve().parents[2]:
                    raise ValueError("Select a separate target checkout; the live controller checkout cannot be audited.")
                check_audit_model(model, self.repo)
                self.runs_dir.mkdir(parents=True, exist_ok=True)
                # Stable launch intent prevents a second child even if the parent
                # crashes before the CLI reserves its authoritative audit ledger.
                logs = self.runs_dir / ("run-audit-" + plan_audit.digest(identity))
                try:
                    logs.mkdir(exist_ok=False)
                except FileExistsError:
                    raise ValueError(f"Audit launch already recorded; reopen {logs}. No automatic retry is allowed.") from None
                self.logs = logs
                simple_run.save_json(logs / "desktop-audit.json", {"identity": identity,
                    "configuration": config, "directory": str(logs / "audit")})
                self.process_history = []
                self.exit_code = None
                return launch_runner(self, repo=self.repo, plan=self.plan, logs=logs, attempts=1,
                    audit_options=dict(config, directory=logs / "audit"), popen=Popen,
                    persist_metadata=self._metadata, reserve=reserve_job,
                    cancel=cancel_reservation, fail=self._fail)
            except (OSError, ValueError, KeyError, TypeError, PilotError) as exc:
                if (getattr(exc, "cleanup_confirmed", None) is False
                        or (getattr(exc, "process_started", None) is True
                            and getattr(exc, "cleanup_confirmed", None) is not True)):
                    from auto_coding.repository_ownership import preserve_process_uncertainty
                    preserve_process_uncertainty(self.repo, self.logs or self.runs_dir)
                    self._launch_uncertain = True
                return self._fail(str(exc))

    def audit_status(self) -> dict:
        """Revalidate durable evidence independently of another run's availability.

        No verdict is cached: changed bytes/checkout/policy revoke permission on
        the next snapshot and again on launch. A busy run never deletes evidence.
        """
        result = {"status": "Idle", "identity": None, "report": None,
                  "verified": False, "start_approved": False, "audit_ready": False}
        try:
            if self.repo is None or self.plan is None:
                return result
            tasks, fingerprint = load_tasks(self.plan)
            if self.repo == Path(simple_run.__file__).resolve().parents[2]:
                raise ValueError("Select a separate target checkout; the live controller checkout cannot be audited.")
            directory = None
            intent = self.logs / "desktop-audit.json" if self.logs else None
            # An implementation run has its own saved approval binding. Never
            # turn that binding into permission to start a different new run.
            state = read_run(self.logs).get("state") if self.logs else None
            if state and "plan_audit" in state:
                manifest = plan_audit.read_json(self.logs / "run-manifest.json")
                plan_audit.verify_resume(simple_run, state, manifest, self.repo, fingerprint, tasks)
                simple_run.verify_resume_git(self.repo, state, manifest, tasks)
                if (state["repo"] != str(self.repo)
                        or simple_run.require_run_branch(self.repo) != manifest["branch"]
                        or simple_run.requested_verification(self.plan, fingerprint, None) != state["final_verification"]):
                    raise ValueError("Saved run repository, branch or verification policy changed")
                evidence, report = plan_audit.load_evidence(simple_run, state["plan_audit"]["directory"],
                    self.repo, fingerprint, [t["id"] for t in tasks])
                return dict(result, status="APPROVE", verified=True, identity=evidence["identity"],
                            configuration=evidence["configuration"], report=report,
                            directory=state["plan_audit"]["directory"], scope="resume")
            if self.logs and (self.logs / "audit-evidence.json").exists():
                directory = self.logs
                result["identity"] = plan_audit.read_json(directory / "audit-evidence.json")["identity"]
            elif intent and intent.exists():
                saved = plan_audit.read_json(intent)
                directory = Path(saved["directory"]).resolve()
                if directory != self.logs / "audit":
                    raise ValueError("Audit directory differs from saved launch location")
                result.update(identity=saved["identity"], configuration=saved["configuration"])
            elif self.logs:
                return result

            # Uses exactly the CLI's identity and input checks, without a model
            # capability probe or any call to Codex. Model/goal cannot buy retries.
            _, _, identity, _ = plan_audit.audit_inputs(simple_run, self.plan, self.repo,
                model="identity-check", controller_tests=False)
            ledger = plan_audit.ledger_path(self.repo, identity)
            launch_logs = self.runs_dir / ("run-audit-" + plan_audit.digest(identity))
            consumed = ledger.exists() or launch_logs.exists()
            result["audit_ready"] = not consumed
            if ledger.exists():
                prior = plan_audit.read_json(ledger)
                directory = Path(prior["directory"]).resolve()
            elif directory is None and launch_logs.exists():
                directory = launch_logs / "audit"
            if directory is None:
                return result
            result["directory"] = str(directory)
            if not (directory / "audit-evidence.json").exists():
                result["status"] = "Unknown"
                if intent and intent.exists() and (self.process is not None and self.process.poll() is None
                                                   or self._selected_run_active()):
                    result["status"] = "Running"
                elif intent and intent.exists() and self.exit_code is not None:
                    result["status"] = "Infrastructure error"
                result["error"] = "No completed controller-owned audit evidence. Saved one-shot calls cannot be repeated."
                return result
            evidence, report = plan_audit.load_evidence(simple_run, directory, self.repo,
                fingerprint, [task["id"] for task in tasks])
            result.update(identity=evidence["identity"], configuration=evidence["configuration"], report=report)
            if evidence["identity"] != identity or evidence["configuration"]["controller_tests"] is not False:
                raise ValueError("Audit target or verification configuration changed; audit the revised inputs")
            result["status"] = report["decision"]
            if report["decision"] == "APPROVE":
                plan_audit.authorize(simple_run, directory, self.repo, fingerprint, tasks,
                    identity["verification_policy"], controller_tests=False)
                result.update(verified=True, start_approved=True)
            else:
                result["error"] = "Revise the plan, then audit the exact revised bytes before starting."
            return result
        except (OSError, ValueError, KeyError, TypeError, PilotError) as exc:
            result.update(status="Unknown", verified=False, start_approved=False, error=str(exc))
            return result

    @staticmethod
    def _valid_pid(pid) -> bool:
        return run_lifecycle.valid_pid(pid)

    @staticmethod
    def _pid_active(pid) -> bool:
        if not DesktopController._valid_pid(pid): return False
        return run_lifecycle.pid_active(pid)

    def _other_active_run(self) -> bool:
        if not self.runs_dir.is_dir(): return False
        for metadata in self.runs_dir.glob("run-*/desktop-run.json"):
            try:
                item = json.loads(metadata.read_text(encoding="utf-8"))
                if not isinstance(item, dict):
                    continue
                # A stopped record's PID is historical and may have been reused.
                if item.get("status") not in {"launching", "running"}:
                    continue
                pid = item.get("pid")
                # A launch intent with no persisted PID is deliberately
                # unresolved: Popen may have succeeded immediately before the
                # metadata write failed. Treat it as active so another
                # controller cannot create duplicate work. A running record
                # with malformed/missing PID is equally ambiguous.
                if not self._valid_pid(pid):
                    return True
                if self._pid_active(pid) and pid != getattr(self.process, "pid", None): return True
            except (OSError, ValueError, TypeError):
                # A damaged launch record cannot establish that the child is idle.
                return True
        return False

    def _selected_run_active(self) -> bool:
        """Check the selected run's own persisted child PID, wherever its logs live."""
        if self.logs is None:
            return False
        metadata = self.logs / "desktop-run.json"
        try:
            item = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                return False
            if item.get("status") not in {"launching", "running"}:
                return False
            pid = item.get("pid")
        except (OSError, ValueError, TypeError):
            return False
        # A process handle is authoritative for jobs launched by this controller;
        # persisted PIDs cover runs reopened by another controller instance.
        own_pid = getattr(self.process, "pid", None)
        return pid != own_pid and self._pid_active(pid)

    def _selected_run_unknown(self) -> bool:
        """An active launch record without a valid PID must not be treated as idle."""
        item = self._read_metadata()
        return bool(self._metadata_error or (item and item.get("status") in {"launching", "running"}
                    and not self._valid_pid(item.get("pid"))))

    @serialized_launch
    def start(self, attempts: int = 3, auto_push: bool = False) -> dict:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self._fail("A desktop run is already active.")
            try:
                if self._launch_uncertain or self._selected_run_unknown():
                    raise ValueError("The selected run has unresolved launch status and may still be active. Inspect its process and logs before starting another run.")
                if self._selected_run_active():
                    raise ValueError("The selected run's child process is still active. Wait for it to stop before resuming.")
                if self._other_active_run(): raise ValueError("An earlier desktop run is still active. Reopen its logs and wait for it to stop.")
                self._validate()
                if type(auto_push) is not bool or (auto_push and not self.repository):
                    raise ValueError("Auto-push requires an explicitly selected managed GitHub repository.")
                if not 1 <= attempts <= 10: raise ValueError("Attempts must be between 1 and 10.")
                # Reject containment before creation so failed START cannot dirty
                # the target checkout.
                runs_dir = self.runs_dir.resolve(strict=False)
                if runs_dir == self.repo or runs_dir.is_relative_to(self.repo):
                    raise ValueError("Application run storage must be outside the target repository.")
                approval = self.audit_status()
                if not approval.get("start_approved"):
                    raise ValueError(approval.get("error") or "Start requires a verified APPROVE audit for these exact inputs.")
                approved_audit = approval["directory"]
                runs_dir.mkdir(parents=True, exist_ok=True)
                logs = runs_dir / ("run-" + uuid.uuid4().hex)
                logs.mkdir(exist_ok=False)
                self.logs = logs
                self.process_history = []
                self.exit_code = None
                self._metadata(logs)
                return self._launch(attempts, False, auto_push=auto_push, approved_audit=approved_audit)
            except (OSError, ValueError, PilotError) as exc:
                return self._fail(str(exc))

    @serialized_launch
    def resume(self, attempts: int = 3) -> dict:
        with self._lock:
            if self.logs and (self.logs / "desktop-audit.json").exists():
                return self._fail("One-shot audits cannot be resumed or repeated. Inspect the audit report and diagnostics.")
            if self.process is not None and self.process.poll() is None:
                return self._fail("A desktop run is already active.")
            try:
                if self._selected_run_active():
                    raise ValueError("The selected run's child process is still active. Wait for it to stop before resuming.")
                if self._selected_run_unknown() and not self._launch_uncertain:
                    raise ValueError("Run launch status is unknown or desktop-run.json is corrupt. Inspect the process and logs before resuming.")
                if self._other_active_run(): raise ValueError("An earlier desktop run is still active. Reopen its logs and wait for it to stop.")
                if self.repo is None or self.plan is None or self.logs is None:
                    raise ValueError("Select the original repository, plan, and existing run logs first.")
                if not 1 <= attempts <= 10:
                    raise ValueError("Attempts must be between 1 and 10.")
                _, plan_sha = load_tasks(self.plan)
                # Resume permits dirty/staged work for runner reconciliation, but keeps branch gate.
                check = self.preflight()
                if check["errors"]: raise ValueError(" ".join(check["errors"]))
                snapshot = read_run(self.logs)
                state = snapshot.get("state")
                if self._launch_uncertain:
                    raise ValueError("The previous launch could not persist its child PID. Check whether the runner is still active before retrying; automatic resume is blocked.")
                metadata = self._read_metadata()
                if check["resume_error"]: raise ValueError(check["resume_error"])
                if metadata and metadata.get("status") in {"launching", "running"} and not self._pid_active(metadata.get("pid")):
                    raise ValueError("Run metadata indicates a launch whose child status is unknown. Inspect the run before resuming.")
                if not state or (state.get("status") != "stopped" and not (
                        state.get("auto_push") and state.get("status") == "checkpointing")):
                    raise ValueError("Resume requires a stopped run or an interrupted auto-push checkpoint.")
                if state.get("plan_sha256") != plan_sha or state.get("repo") != str(self.repo):
                    raise ValueError("Resume plan or repository mismatch; select the original plan and repository.")
                return self._launch(attempts, True)
            except (OSError, ValueError, PilotError) as exc:
                return self._fail(str(exc))

    def _launch(self, attempts: int, resume: bool, auto_push: bool = False, approved_audit=None) -> dict:
        config = None
        if resume:
            config = read_run(self.logs).get("state", {}).get("auto_push")
        elif auto_push:
            config = {"repository": self.repository, "branch": self.preflight()["branch"]}
        return launch_runner(self, repo=self.repo, plan=self.plan, logs=self.logs, attempts=attempts,
                             resume=resume, publication=config, approved_audit=approved_audit, popen=Popen, persist_metadata=self._metadata,
                             reserve=reserve_job, cancel=cancel_reservation, fail=self._fail)

    def _read_metadata(self):
        value, self._metadata_error = run_lifecycle.read_launch_metadata(self.logs)
        return value

    def _metadata(self, logs: Path, pid: int | None = None, status: str = "stopped"):
        run_lifecycle.write_launch_metadata(logs, repo=self.repo, plan=self.plan, pid=pid,
                                           status=status, process_history=self.process_history)

    def _fail(self, message: str) -> dict:
        self.error = message
        return {"ok": False, "error": message}

    def poll(self) -> dict:
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                self.exit_code = code
                self.process = None
                self.process_history.append({"exit_code": code, "launch_warning": self.error,
                    "message": (f"Runner stopped with exit code {code}; inspect state.json and events.jsonl."
                                if code != 0 else "Runner process exited with code 0; durable state determines the run outcome.")})
                if self.logs:
                    try:
                        self._metadata(self.logs, status="stopped")
                        self._launch_uncertain = False
                    except OSError as exc:
                        self.error = f"Runner exited with code {code}, but desktop metadata could not be updated: {exc}. Inspect state.json and events.jsonl."
                if code != 0:
                    self.error = f"Runner stopped with exit code {code}; inspect state.json and events.jsonl."
        elif self._detached_pid is not None and not self._pid_active(self._detached_pid):
            self.error = "The previously launched child is no longer running; durable run state was left unchanged. Inspect state.json and events.jsonl."
            self._detached_pid = None
            if self.logs:
                try:
                    self._metadata(self.logs, status="stopped")
                except OSError as exc:
                    self.error += f" Desktop metadata could not be updated: {exc}."
        # A reopened launch with no valid persisted PID is neither confirmed
        # running nor confirmed stopped. Do not tell the dashboard it is idle:
        # use JSON null for active and surface an explicit actionable warning.
        recorded = read_run(self.logs) if self.logs else {}
        state = recorded.get("state") or {}
        metadata = self._read_metadata()
        cli_unknown = (self.process is None and self._detached_pid is None and metadata is None
                       and bool(state) and (state.get("status") not in ("stopped", "completed", "failed")
                       or recorded.get("summary", {}).get("execution_open")
                       or (isinstance(state.get("final_verification_result"), dict)
                           and state["final_verification_result"].get("status") in ("prepared", "running"))))
        uncertain = self._launch_uncertain or self._selected_run_unknown() or cli_unknown
        if uncertain and self.process is None and self._detached_pid is None:
            active = None
            if not self.error:
                self.error = self._metadata_error or ("Run process status is unknown: no confirmed child activity is available. "
                              "The runner may still be active. "
                              "Inspect the process and logs before starting or resuming.")
        else:
            active = self.process is not None or (self._detached_pid is not None and self._pid_active(self._detached_pid))
        # A durable result cannot prove that an active/unknown child is idle.
        outcome = ("unknown" if uncertain or active is None else "active" if active else
                   (state or {}).get("status", "unavailable"))
        return {"active": active, "uncertain": uncertain, "outcome": outcome,
                "process_history": list(self.process_history),
                "exit_code": self.exit_code, "error": self.error}

    def snapshot(self) -> dict:
        self.poll()
        data = read_run(self.logs) if self.logs else read_run(Path("." ) / "__missing_run_logs__")
        data["controller"] = {"repo": str(self.repo) if self.repo else None, "plan": str(self.plan) if self.plan else None,
                               "logs": str(self.logs) if self.logs else None, **self.poll()}
        return data

    def selected_paths(self) -> dict:
        """Return the paths currently selected by the desktop UI."""
        return {"repo": str(self.repo) if self.repo else None,
                "plan": str(self.plan) if self.plan else None,
                "logs": str(self.logs) if self.logs else None}

    def job_status(self) -> dict:
        """Return current process activity, exit code, and readable controller error."""
        return self.poll()

    def reopen(self, logs: str | Path) -> dict:
        result = self.select_logs(logs)
        if not result["ok"]: return result
        self.repository = None
        self.error = None
        self._launch_uncertain = False
        self._detached_pid = None
        self.process_history = []
        self.exit_code = None
        # CLI logs use the same state and manifest, without desktop metadata.
        # Recover the target for display; selecting the original plan remains
        # necessary for resume authorization when its path was never persisted.
        recorded = read_run(self.logs).get("state") or {}
        self.repo = Path(recorded["repo"]) if isinstance(recorded.get("repo"), str) and recorded["repo"] else None
        self.plan = None
        # CLI-produced audit directories have no desktop launch metadata. Their
        # identity supplies only the target; select the exact plan for validation.
        if (self.logs / "audit-evidence.json").is_file():
            try:
                evidence = plan_audit.read_json(self.logs / "audit-evidence.json")
                self.repo = Path(evidence["identity"]["target"]["repo"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return self._fail(f"Cannot load audit evidence: {exc}")
        try:
            value = self._read_metadata()
            if self._metadata_error:
                self._launch_uncertain = True
                self.error = self._metadata_error + ". Child status is unknown; inspect before retrying."
            if value is None: raise ValueError("No usable desktop metadata")
            self.process_history = [item for item in value.get("process_history", [])
                                    if isinstance(item, dict)] if isinstance(value.get("process_history", []), list) else []
            self.exit_code = next((item["exit_code"] for item in reversed(self.process_history)
                                   if type(item.get("exit_code")) is int), None)
            self.repo, self.plan = Path(value["repo"]), Path(value["plan"])
            if managed_lock(self.repo):
                try:
                    identity = json.loads(self.repo.with_suffix(".json").read_text(encoding="utf-8"))["repository"]
                    manager = self._workspace_manager()
                    _, _, managed_repo, metadata, _ = manager._paths(identity)
                    if managed_repo == self.repo:
                        manager._verify(identity, self.repo, metadata)
                        self.repository = identity
                except (OSError, ValueError, KeyError, TypeError):
                    self.repository = None
            in_flight = value.get("status") in {"launching", "running"}
            pid = value.get("pid")
            # Never monitor a historical PID from a stopped run: it may have
            # been recycled for an unrelated application.
            self._detached_pid = pid if in_flight and self._valid_pid(pid) else None
            self._launch_uncertain = in_flight and self._detached_pid is None
            self.error = None
            if self._launch_uncertain:
                self.error = ("Run launch status is unknown: no valid child PID was saved. "
                              "The runner may still be active; inspect it before retrying.")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {"ok": True, "path": str(self.logs), "error": None,
                "warning": self.error if self._launch_uncertain else None}
