"""Small process controller for the existing local-only simple_run CLI.

Default run storage is ``~/AutoCoding-Runs``. Closing the desktop window leaves
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


class DesktopController:
    def __init__(self, runs_dir: str | Path | None = None):
        self.runs_dir = Path(runs_dir or (Path.home() / "AutoCoding-Runs")).expanduser().resolve()
        self.repo: Path | None = None
        self.plan: Path | None = None
        self.logs: Path | None = None
        self.process = None
        self._detached_pid: int | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self._launch_uncertain = False
        self._lock = threading.Lock()

    def select_repo(self, path: str | Path) -> dict:
        try:
            repo = Path(path).expanduser().resolve(strict=True)
            if not repo.is_dir(): raise ValueError("Target must be a directory.")
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True, shell=False)
            if top.returncode or Path(top.stdout.strip()).resolve() != repo:
                raise ValueError("Select the Git repository root, not a subdirectory.")
            self.repo = repo
            return {"ok": True, "path": str(repo), "error": None}
        except (OSError, ValueError) as exc:
            self.repo = None
            self.error = f"Repository validation failed: {exc}"
            return {"ok": False, "path": None, "error": self.error}

    def select_plan(self, path: str | Path) -> dict:
        try:
            plan = Path(path).expanduser().resolve(strict=True)
            if not plan.is_file() or plan.suffix.lower() not in {".json", ".txt", ".md"}:
                raise ValueError("Select an existing .json, .txt, or .md plan file.")
            load_tasks(plan)
            self.plan = plan
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

    def _validate(self):
        if self.repo is None or self.plan is None:
            raise ValueError("Select and validate a repository and plan first.")
        # Revalidate plan on launch; never trust stale UI validation.
        load_tasks(self.plan)
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.repo, capture_output=True, text=True, shell=False)
        if branch.returncode: raise ValueError("Could not determine the current Git branch.")
        if branch.stdout.strip().lower() in {"main", "master"}:
            raise ValueError("Runs are blocked on main/master. Switch to a feature branch first.")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.repo, capture_output=True, text=True, shell=False)
        if status.returncode: raise ValueError("Could not check Git working tree.")
        if status.stdout.strip(): raise ValueError("Working tree has staged or unstaged changes. Commit or discard them, then use a feature branch.")

    @staticmethod
    def _valid_pid(pid) -> bool:
        # bool is an int in Python, but never a real process identifier.
        return type(pid) is int and pid > 0

    @staticmethod
    def _pid_active(pid) -> bool:
        if not DesktopController._valid_pid(pid): return False
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
                continue
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
        return bool(item and item.get("status") in {"launching", "running"}
                    and not self._valid_pid(item.get("pid")))

    def start(self, attempts: int = 3) -> dict:
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
                self._metadata(logs)
                return self._launch(attempts, False)
            except (OSError, ValueError, PilotError) as exc:
                return self._fail(str(exc))

    def resume(self, attempts: int = 3) -> dict:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self._fail("A desktop run is already active.")
            try:
                if self._selected_run_active():
                    raise ValueError("The selected run's child process is still active. Wait for it to stop before resuming.")
                if self._other_active_run(): raise ValueError("An earlier desktop run is still active. Reopen its logs and wait for it to stop.")
                if self.repo is None or self.plan is None or self.logs is None:
                    raise ValueError("Select the original repository, plan, and existing run logs first.")
                if not 1 <= attempts <= 10:
                    raise ValueError("Attempts must be between 1 and 10.")
                _, plan_sha = load_tasks(self.plan)
                # Resume permits dirty/staged work for runner reconciliation, but keeps branch gate.
                branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.repo, capture_output=True, text=True, shell=False)
                if branch.returncode or branch.stdout.strip().lower() in {"main", "master"}:
                    raise ValueError("Resume is blocked on main/master. Switch to the original feature branch first.")
                snapshot = read_run(self.logs)
                state = snapshot.get("state")
                if self._launch_uncertain:
                    raise ValueError("The previous launch could not persist its child PID. Check whether the runner is still active before retrying; automatic resume is blocked.")
                metadata = self._read_metadata()
                if metadata and metadata.get("status") in {"launching", "running"} and not self._pid_active(metadata.get("pid")):
                    raise ValueError("Run metadata indicates a launch whose child status is unknown. Inspect the run before resuming.")
                if not state or state.get("status") != "stopped":
                    raise ValueError("Resume is available only when state.json truthfully records status 'stopped'.")
                if state.get("plan_sha256") != plan_sha or state.get("repo") != str(self.repo):
                    raise ValueError("Resume plan or repository mismatch; select the original plan and repository.")
                return self._launch(attempts, True)
            except (OSError, ValueError, PilotError) as exc:
                return self._fail(str(exc))

    def _launch(self, attempts: int, resume: bool) -> dict:
        src = str(Path(__file__).resolve().parents[1])
        env = os.environ.copy()
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = [sys.executable, "-B", "-m", "auto_coding.simple_run", "--repo", str(self.repo),
                "--logs", str(self.logs), "--attempts", str(attempts)]
        if resume: argv.append("--resume")
        argv.append(str(self.plan))
        try:
            # Persist intent before creating a child. If PID persistence then fails,
            # reopen can still refuse an unsafe duplicate resume.
            self._metadata(self.logs, status="launching")
            self.process = Popen(argv, cwd=src, env=env, shell=False)
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
            if self.process is None and self.logs is not None:
                try:
                    self._metadata(self.logs, status="stopped")
                except OSError:
                    # Leaving launch intent is conservative: a reopened UI will
                    # require inspection rather than risk a duplicate resume.
                    self._launch_uncertain = True
            return self._fail(f"Could not start the local runner: {type(exc).__name__}: {exc}")

    def _read_metadata(self):
        if self.logs is None:
            return None
        try:
            value = json.loads((self.logs / "desktop-run.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _metadata(self, logs: Path, pid: int | None = None, status: str = "stopped"):
        value = {"repo": str(self.repo), "plan": str(self.plan), "logs": str(logs)}
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
                self.error = ("Run launch status is unknown: metadata says launching/running "
                              "without a valid child PID. The runner may still be active. "
                              "Inspect the process and logs before starting or resuming.")
        else:
            active = self.process is not None or (self._detached_pid is not None and self._pid_active(self._detached_pid))
        return {"active": active, "uncertain": uncertain,
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
        meta = self.logs / "desktop-run.json"
        try:
            value = json.loads(meta.read_text(encoding="utf-8"))
            self.repo, self.plan = Path(value["repo"]), Path(value["plan"])
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
