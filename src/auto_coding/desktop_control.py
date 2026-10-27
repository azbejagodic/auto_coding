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

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.run_history import RunHistory
from auto_coding.simple_run import PilotError, load_tasks
from auto_coding.github_workspace import (GitHubWorkspaceManager, storage_root, serialized_launch,
    managed_lock, reserve_job, cancel_reservation)


class DesktopController:
    def __init__(self, runs_dir: str | Path | None = None):
        self.runs_dir = Path(runs_dir or (storage_root() / "runs")).expanduser().resolve()
        self.run_history = RunHistory(self.runs_dir)
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
        repo = Path(repo).resolve()
        if self.repo and self.repo.resolve() == repo:
            if self._launch_uncertain or self._selected_run_unknown() or self._selected_run_active():
                return True
            if self.process is not None and self.process.poll() is None:
                return True
            if self.logs:
                state = read_run(self.logs).get("state")
                if state and state.get("repo") == str(repo) and state.get("status") not in {"stopped", "completed"}:
                    return True
        paths = list(self.runs_dir.glob("run-*/desktop-run.json"))
        if self.logs and (self.logs / "desktop-run.json") not in paths:
            paths.append(self.logs / "desktop-run.json")
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(item, dict): return True
                if item.get("repo") and Path(item["repo"]).resolve() != repo: continue
                if item.get("status") in {"launching", "running"}:
                    # Even an absent PID may have left active descendants.
                    return True
            except FileNotFoundError:
                if path.parent == self.logs: continue
                return True
            except (OSError, ValueError, TypeError):
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
            repo = Path(path).expanduser().resolve(strict=True)
            if not repo.is_dir(): raise ValueError("Target must be a directory.")
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", shell=False)
            if top.returncode or Path(top.stdout.strip()).resolve() != repo:
                raise ValueError("Select the Git repository root, not a subdirectory.")
            self.repo = repo
            self.repository = None
            self.error = None
            return {"ok": True, "path": str(repo), "error": None}
        except (OSError, ValueError) as exc:
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
        result = {"branch": None, "clean": None, "errors": [], "resume_error": None, "start_error": None}
        metadata = self._read_metadata()
        if metadata and (metadata.get("repo") != str(self.repo) or metadata.get("plan") != str(self.plan)):
            result["resume_error"] = "Resume requires the original exact repository and plan paths saved in desktop-run.json."
        if self.repo is None:
            result["errors"].append("Select a Git repository root.")
            return result
        job_lock = managed_lock(self.repo, ".job.lock")
        if job_lock and job_lock.exists():
            result["errors"].append("An Auto-Coding runner is active or uncertain for this checkout.")
        if self.runs_dir == self.repo or self.runs_dir.is_relative_to(self.repo):
            result["start_error"] = "Run storage must be outside the project. Select a different project or configure external run storage."
        if self._other_active_run():
            result["errors"].append("An earlier desktop run may still be active. Open its logs and wait for it to stop.")
        try:
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.repo,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=10)
            result["branch"] = branch.stdout.strip() if not branch.returncode else None
            if not result["branch"]:
                result["errors"].append("Select a feature branch; detached HEAD or unavailable Git branch.")
            elif result["branch"].lower() in {"main", "master"}:
                result["errors"].append("Runs are blocked on main/master. Switch to a feature branch first.")
            status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                    cwd=self.repo, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=10)
            result["clean"] = not status.stdout.strip() if not status.returncode else None
            if status.returncode:
                result["errors"].append("Could not check Git working tree.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["errors"].append(f"Git preflight failed: {exc}")
        return result

    def _validate(self):
        if self.repo is None or self.plan is None:
            raise ValueError("Select and validate a repository and plan first.")
        # Revalidate plan on launch; never trust stale UI validation.
        load_tasks(self.plan)
        check = self.preflight()
        if check["errors"]: raise ValueError(" ".join(check["errors"]))
        if check["clean"] is not True: raise ValueError("Working tree has staged or unstaged changes. Commit or discard them, then use a feature branch.")

    @staticmethod
    def _valid_pid(pid) -> bool:
        # bool is an int in Python, but never a real process identifier.
        return type(pid) is int and pid > 0

    @staticmethod
    def _pid_active(pid) -> bool:
        if not DesktopController._valid_pid(pid): return False
        if os.name == "nt":
            # os.kill(pid, 0) uses TerminateProcess on Windows. Query only.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                # Access denied is uncertain; conservatively block duplicates.
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID absent
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

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
                runs_dir.mkdir(parents=True, exist_ok=True)
                logs = runs_dir / ("run-" + uuid.uuid4().hex)
                logs.mkdir(exist_ok=False)
                self.logs = logs
                self.process_history = []
                self.exit_code = None
                self._metadata(logs)
                self.run_history.register(logs)
                return self._launch(attempts, False, auto_push=auto_push)
            except (OSError, ValueError, PilotError) as exc:
                return self._fail(str(exc))

    @serialized_launch
    def resume(self, attempts: int = 3) -> dict:
        with self._lock:
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

    def _launch(self, attempts: int, resume: bool, auto_push: bool = False) -> dict:
        src = str(Path(__file__).resolve().parents[1])
        env = os.environ.copy()
        # A reservation belongs only to this launch, never to a parent runner.
        env.pop("AUTOCODING_JOB_RESERVATION", None)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = [sys.executable, "-B", "-m", "auto_coding.simple_run", "--repo", str(self.repo),
                "--logs", str(self.logs), "--attempts", str(attempts)]
        if resume: argv.append("--resume")
        config = None
        if resume:
            config = read_run(self.logs).get("state", {}).get("auto_push")
        elif auto_push:
            config = {"repository": self.repository, "branch": self.preflight()["branch"]}
        if config:
            argv.extend(["--auto-push-repository", config["repository"], "--auto-push-branch", config["branch"]])
        argv.append(str(self.plan))
        reservation = None
        spawned = False
        try:
            reservation = reserve_job(self.repo)
            if reservation is not None:
                env["AUTOCODING_JOB_RESERVATION"] = reservation
            # Persist intent before creating a child. If PID persistence then fails,
            # reopen can still refuse an unsafe duplicate resume.
            self._metadata(self.logs, status="launching")
            options = {}
            if os.name == "nt":
                options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            # Do not inherit the dashboard console or pipes. Diagnostic output
            # remains available even if initialization fails before state.json.
            output_path = self.logs / "runner-output.txt"
            if not output_path.resolve().is_relative_to(self.logs.resolve()):
                raise OSError("Runner output path escapes the selected logs directory.")
            with output_path.open("ab") as output:
                self.process = Popen(argv, cwd=src, env=env, shell=False,
                                     stdin=subprocess.DEVNULL, stdout=output,
                                     stderr=subprocess.STDOUT, **options)
                spawned = True
            self.exit_code = None
            self.error = None
            try:
                self._metadata(self.logs, pid=getattr(self.process, "pid", None), status="running")
                self._launch_uncertain = False
            except OSError as exc:
                self._launch_uncertain = True
                self.error = f"Runner is active (PID {getattr(self.process, 'pid', 'unknown')}), but its PID could not be saved: {exc}. Keep this window open and do not resume this run."
            return {"ok": True, "argv": argv, "error": self.error}
        except OSError as exc:
            if not spawned:
                cancel_reservation(self.repo, reservation)
            if self.process is None and self.logs is not None:
                try:
                    self._metadata(self.logs, status="stopped")
                except OSError:
                    # Leaving launch intent is conservative: a reopened UI will
                    # require inspection rather than risk a duplicate resume.
                    self._launch_uncertain = True
            return self._fail(f"Could not start the local runner: {type(exc).__name__}: {exc}")

    def _read_metadata(self):
        self._metadata_error = None
        if self.logs is None:
            return None
        try:
            path = self.logs / "desktop-run.json"
            if not path.exists() and not path.is_symlink():
                return None  # Legacy runs legitimately have no desktop metadata.
            raw = read_evidence(self.logs, "desktop-run.json")
            if not raw["ok"]: raise ValueError(raw["text"])
            value = json.loads(raw["text"])
            if not isinstance(value, dict): raise ValueError("metadata must be an object")
            return value
        except (OSError, ValueError) as exc:
            self._metadata_error = f"desktop-run.json is corrupt or unreadable: {exc}"
            return None

    def _metadata(self, logs: Path, pid: int | None = None, status: str = "stopped"):
        value = {"repo": str(self.repo), "plan": str(self.plan), "logs": str(logs)}
        if self.process_history:
            value["process_history"] = self.process_history
        if pid is not None: value["pid"] = pid
        value["status"] = status
        temp = logs / "desktop-run.json.tmp"
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        temp.replace(logs / "desktop-run.json")

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
        uncertain = self._launch_uncertain or self._selected_run_unknown()
        if uncertain and self.process is None and self._detached_pid is None:
            active = None
            if not self.error:
                self.error = self._metadata_error or ("Run launch status is unknown: metadata says launching/running "
                              "without a valid child PID. The runner may still be active. "
                              "Inspect the process and logs before starting or resuming.")
        else:
            active = self.process is not None or (self._detached_pid is not None and self._pid_active(self._detached_pid))
        state = read_run(self.logs).get("state") if self.logs else None
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
        self.run_history.register(self.logs)
        self.repository = None
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
