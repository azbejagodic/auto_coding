"""Model-free controller tests in the validated Windows MXC sandbox."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from auto_coding.python_environment import test_environment
from auto_coding import test_workspace
from auto_coding.sensitive_paths import is_sensitive_path
from auto_coding.reviewer_permissions import (
    ReviewerEnvironmentError, activated, check_cli, prepare_review,
)


# Operator-controlled only: JSON array of trusted, preinstalled installation
# directories. Never loaded from a plan or target file, or passed to test code.
TOOLCHAIN_DIRS_ENV = "AUTOCODING_TEST_TOOLCHAIN_DIRS_JSON"


def trusted_toolchain_filesystem(repo, logs, runtime, windows):
    raw = os.environ.get(TOOLCHAIN_DIRS_ENV)
    if raw is None:
        # Discover only fixed machine installation locations. PATH, project
        # manifests and missing-executable messages never authorize grants.
        program_files = os.environ.get("ProgramFiles") or os.environ.get("PROGRAMFILES")
        roots = [Path(program_files) / name for name in ("Git", "nodejs")] if program_files else []
        raw = json.dumps([str(path) for path in roots if path.is_dir()])
    try:
        if len(raw) > 32768:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError
        protected = [repo, logs, runtime, windows, Path(__file__).resolve().parents[2],
                     Path.home(), Path.home().parent]
        protected += [Path(os.environ[key]) for key in ("USERPROFILE", "HOME", "PROGRAMDATA")
                      if os.environ.get(key)]
        protected = [path.resolve(strict=False) for path in protected]
        result, denied = {}, []
        for value in values:
            if (not isinstance(value, str) or not value or len(value) > 4096
                    or any(ord(char) < 32 for char in value)):
                raise ValueError
            path = Path(value)
            # No relative, UNC/device, drive-relative, ADS, dot or short-name
            # spellings. Canonical local installation paths only.
            if (not path.is_absolute() or value.startswith(("\\\\", "//"))
                    or len(path.parts) < 3 or ":" in value[2:]
                    or is_sensitive_path(value) or ".codex" in {part.casefold() for part in path.parts}
                    or any(part in {".", ".."} or part.endswith((".", " "))
                           or "~" in part for part in value.replace("\\", "/").split("/"))):
                raise ValueError
            for component in (path, *path.parents):
                info = component.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError
            resolved = path.resolve(strict=True)
            if resolved != path or not resolved.is_dir():
                raise ValueError
            if any(resolved.is_relative_to(item) or item.is_relative_to(resolved) for item in protected):
                raise ValueError
            result[resolved.as_posix()] = "read"
            # Nested links must not turn an approved installation into a route
            # to unrelated host files. Fail closed on unreadable/oversized trees.
            entries = 0
            def scan_error(error):
                raise error
            for parent, directories, files in os.walk(resolved, followlinks=False, onerror=scan_error):
                for name in directories + files:
                    entries += 1
                    candidate = Path(parent) / name
                    if entries > 50000:
                        raise ValueError
                    info = candidate.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError
                    if (is_sensitive_path(str(candidate)) or name.casefold() == ".codex"):
                        if not any(candidate.is_relative_to(parent) for parent in denied):
                            denied.append(candidate)
                            result[candidate.as_posix()] = "deny"
                            if len(denied) > 128:
                                raise ValueError
        if len(json.dumps(result)) > 16384:
            raise ValueError
        return result
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise ReviewerEnvironmentError("Invalid or unsafe trusted controller-test toolchain directories") from exc


class ControllerTestEnvironmentError(ValueError):
    def __init__(self, message, *, cleanup_confirmed=True, process_started=False,
                 classification="unclassified_execution_problem", details=None, output="",
                 output_artifact=None):
        super().__init__(message)
        self.cleanup_confirmed = cleanup_confirmed
        self.process_started = process_started
        self.classification = classification
        self.details = details if isinstance(details, dict) else {}
        self.output = output if isinstance(output, str) else str(output)
        self.output_artifact = output_artifact


# Trusted stdlib bootstrap ignores target imports (-I). The child receives the
# sanitized target PYTHONPATH. No guest supervisor: bounded_run owns the MXC tree.
_BOOTSTRAP = r'''
import errno, json, os, shutil, stat, subprocess, sys, tempfile
command, marker, environment = json.loads(sys.argv[1])
os.environ.clear()
os.environ.update(environment)
context = {"cwd": os.getcwd(), "tools": {name: shutil.which(name) for name in
           ("python", "git", "node")}, "executable": shutil.which(command[0]),
           "environment": {key: environment.get(key) for key in
           ("PATH", "PYTHONPATH", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "XDG_CACHE_HOME", "PIP_CACHE_DIR", "npm_config_cache")}}
stage = "temporary_storage"
context["recovery"] = []
try:
    for directory in sorted({environment[key] for key in ("TEMP", "TMP", "TMPDIR") if key in environment}):
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            # One controller-designated leaf only; never rerun test code,
            # recreate a parent, expand grants or fall back to host storage.
            os.mkdir(directory)
            context["recovery"].append("recreated_temporary_directory")
            info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError(errno.EACCES, "Temporary storage is a link or reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Temporary storage is not a directory")
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"controller storage probe")
            probe.flush()
    stage = "command_launch"
    result = {"exit_code": subprocess.call(command), "kind": "test"}
except OSError as error:
    result = {"kind": "environment", "error_type": type(error).__name__,
              "error_text": str(error), "errno": error.errno,
              "winerror": getattr(error, "winerror", None), "executable": command[0], "stage": stage}
result["context"] = context
print("\n" + marker + json.dumps(result), flush=True)
'''


class _TestResult(tuple):
    def __new__(cls, code, output, timed=False, cleanup_confirmed=True, process_started=True):
        result = super().__new__(cls, (code, output, timed))
        result.cleanup_confirmed = cleanup_confirmed
        result.process_started = process_started
        return result


def _category(evidence, output):
    if evidence.get("kind") == "environment":
        if evidence.get("errno") in (1, 13) or evidence.get("winerror") == 5:
            return "filesystem_permission"
        if evidence.get("stage") == "temporary_storage":
            return "temporary_storage_failure"
        if evidence.get("errno") == 2 or evidence.get("winerror") in (2, 3):
            return "missing_executable"
        return "command_launch"
    if not evidence.get("exit_code"):
        return "passed"
    # Output is diagnostic evidence, never authorization for retries or grants.
    for pattern, category in ((r"ModuleNotFoundError:", "possible_missing_dependency"),
                              (r"PermissionError:|WinError 5\b", "possible_filesystem_permission"),
                              (r"FileNotFoundError:|WinError 2\b", "possible_missing_executable_or_file"),
                              (r"CalledProcessError:", "nested_subprocess_failure")):
        if re.search(pattern, output):
            return category
    return "test_failure"


def prepare(command, repo, logs, git):
    """Validate policy before target code runs; never use global Codex config."""
    if sys.platform != "win32":
        raise ControllerTestEnvironmentError("Controller test isolation is validated only on Windows MXC")
    repo, logs = Path(repo).resolve(), Path(logs).resolve()
    executable = shutil.which("codex")
    if not executable or Path(executable).resolve().is_relative_to(repo):
        raise ControllerTestEnvironmentError("A trusted installed Codex CLI is required")
    # Reuse regular-checkout checks, external scratch ownership and cleanup.
    spec = prepare_review(None, repo, logs, git, check=None)
    try:
        runtime = Path(sys.executable).resolve().parent
        windows = Path(os.environ["SYSTEMROOT"]).resolve(strict=True)
        if any(path == repo or repo.is_relative_to(path) or path.is_relative_to(repo)
               or path == path.parent for path in (runtime, windows)):
            raise ControllerTestEnvironmentError("Unsupported overlapping runtime/checkout paths")
        workspace = spec.scratch / "workspace"
        workspace.mkdir()
        snapshot = test_workspace.capture(repo, git, workspace)
        filesystem = {":root": "deny", windows.as_posix(): "read", runtime.as_posix(): "read",
                      repo.as_posix(): "deny", spec.scratch.as_posix(): "write"}
        tools = trusted_toolchain_filesystem(repo, logs, runtime, windows)
        filesystem.update(tools)
        name = "auto_coding_tests_" + uuid.uuid4().hex
        policy = 'permissions.' + name + '={extends=":read-only",filesystem={' + ','.join(
            json.dumps(key) + '=' + json.dumps(value) for key, value in filesystem.items()
        ) + '},network={enabled=false}}'
        settings = [f'default_permissions="{name}"', policy, 'windows.sandbox=mxc']
        base = test_environment(repo)
        search = [runtime, windows / "System32", windows]
        for root, access in tools.items():
            if access == "read":
                search.extend(Path(root) / child for child in ("", "cmd", "bin", "Scripts"))
        base["PATH"] = os.pathsep.join([*map(str, search), base.get("PATH", "")])
        base["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        environment = test_workspace.environment(workspace, spec.scratch, base, filesystem, source=repo)
        home = spec.scratch / "codex-home"
        home.mkdir()
        environment.update(CODEX_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        executable = check_cli(settings, name, repo, spec.scratch, environment=environment,
                               filesystem=filesystem, backend="mxc")
        if test_workspace.capture(repo, git) != snapshot:
            raise ControllerTestEnvironmentError("Source snapshot changed while preparing tests")
        relocated = []
        for argument in command:
            path = Path(argument)
            if path.is_absolute() and path.is_relative_to(repo):
                argument = str(workspace / path.relative_to(repo))
            relocated.append(argument)
        resolved = test_workspace.resolve_tool(relocated[0], environment)
        if resolved:
            relocated[0] = resolved
        marker = "AUTO_CODING_TEST_RESULT_" + uuid.uuid4().hex + ":"
        argv = [executable]
        for setting in settings:
            argv.extend(["-c", setting])
        child_environment = {key: value for key, value in environment.items() if key != "CODEX_HOME"}
        argv.extend(["sandbox", "-P", name, "-C", str(workspace), "--", sys.executable, "-I", "-B",
                     "-c", _BOOTSTRAP, json.dumps([relocated, marker, child_environment])])
        spec.argv, spec.environment = argv, environment
        spec.workspace, spec.snapshot = workspace, snapshot
        spec.tools = {tool: test_workspace.resolve_tool(tool, environment) for tool in ("python", "git", "node")}
        return spec, marker
    except BaseException as exc:
        cleanup = getattr(exc, "cleanup_confirmed", None)
        clean = cleanup is True or (cleanup is None and (
            isinstance(exc, (ReviewerEnvironmentError, ControllerTestEnvironmentError, KeyError, ValueError))
            or getattr(exc, "process_started", None) is False))
        diagnostic = logs / ("controller-test-preflight-" + uuid.uuid4().hex + ".json")
        diagnostic.write_text(json.dumps({"stage": "sandbox_preflight", "error_type": type(exc).__name__,
            "error_text": str(exc)[:2048], "cleanup_confirmed": clean,
            "workspace_retained": not clean, "scratch": str(spec.scratch)}, indent=2), encoding="utf-8")
        if clean:
            spec.cleanup()
        raise ControllerTestEnvironmentError(
            f"Controller test sandbox preflight failed ({type(exc).__name__}); scratch: {spec.scratch}",
            cleanup_confirmed=clean, process_started=not clean,
            classification=("confirmed_environment_blocker" if isinstance(
                exc, (ReviewerEnvironmentError, OSError, KeyError, ValueError))
                else "unclassified_execution_problem"),
            details={"stage": "sandbox_preflight", "error_type": type(exc).__name__,
                     "error_text": str(exc)[:2048]}) from exc


def run(command, repo, logs, timeout, *, git, launch):
    spec, marker = prepare(command, repo, logs, git)
    clean = False
    diagnostic, context = None, None
    try:
        with activated(spec.environment):
            result = launch(spec.argv, spec.workspace, timeout)
        code, output, timed = result
        cleanup = getattr(result, "cleanup_confirmed", None)
        clean = cleanup is True
        diagnostic = Path(logs) / ("controller-test-sandbox-" + uuid.uuid4().hex)
        # Controller-owned exports only. Never read a guest-selected output path.
        diagnostic.with_suffix(".txt").write_text(output, encoding="utf-8")
        context = {"snapshot": spec.snapshot, "cwd": str(spec.workspace),
                   "resolved_tools": spec.tools,
                   "exit_code": code, "timed_out": timed, "cleanup_confirmed": clean,
                   "workspace_cleanup": "pending" if clean else "retained",
                   "category": ("cleanup_uncertain" if not clean else "timeout" if timed else
                                "sandbox_initialization" if output.startswith("Error: failed to prepare MXC sandbox:")
                                else "incomplete_execution_evidence"),
                   "environment": {key: spec.environment.get(key) for key in
                       ("PATH", "PYTHONPATH", "TEMP", "TMP", "TMPDIR", "HOME", "XDG_CACHE_HOME")}}
        diagnostic.with_suffix(".json").write_text(json.dumps(context, indent=2), encoding="utf-8")
        if not clean or timed:
            # An unknown launch result must never look like a completed legacy
            # tuple to the controller's gate/recovery handling.
            return _TestResult(code, output, timed, clean, getattr(result, "process_started", None))
        prefix, separator, footer = output.rpartition("\n" + marker)
        try:
            evidence = json.loads(footer) if separator and code == 0 else None
        except ValueError:
            evidence = None
        if isinstance(evidence, dict):
            context["guest"] = evidence
            context["category"] = _category(evidence, prefix)
            diagnostic.with_suffix(".json").write_text(json.dumps(context, indent=2), encoding="utf-8")
        if isinstance(evidence, dict) and evidence.get("kind") == "environment":
            artifact = diagnostic.with_suffix(".txt")
            details = {key: evidence.get(key) for key in
                       ("executable", "error_type", "error_text", "errno", "winerror")
                       if evidence.get(key) is not None}
            details["stage"] = evidence.get("stage", "command_launch")
            raise ControllerTestEnvironmentError(
                "Configured test command could not be launched; "
                f"diagnostics: {artifact.name}", cleanup_confirmed=True, process_started=False,
                classification="confirmed_environment_blocker", details=details,
                output=prefix, output_artifact=artifact.name)
        if (not isinstance(evidence, dict) or evidence.get("kind") != "test"
                or type(evidence.get("exit_code")) is not int):
            artifact = diagnostic.with_suffix(".txt")
            raise ControllerTestEnvironmentError(
                f"Sandbox execution result was incomplete; see {artifact}", process_started=True,
                classification="unclassified_execution_problem", output=output,
                output_artifact=artifact.name)
        return _TestResult(evidence["exit_code"], prefix)
    except BaseException as exc:
        if not isinstance(exc, ControllerTestEnvironmentError):
            cleanup = getattr(exc, "cleanup_confirmed", None)
            if cleanup is not None:
                clean = cleanup is True
            elif getattr(exc, "process_started", None) is False:
                clean = True
            exc.cleanup_confirmed = clean
        raise
    finally:
        if clean:
            try:
                spec.cleanup()
            except (OSError, ReviewerEnvironmentError) as exc:
                if context is not None:
                    context.update(workspace_cleanup="failed", cleanup_error_type=type(exc).__name__)
                    diagnostic.with_suffix(".json").write_text(json.dumps(context, indent=2), encoding="utf-8")
                raise ControllerTestEnvironmentError(
                    "Test processes stopped, but disposable workspace cleanup failed; inspect retained scratch",
                    cleanup_confirmed=True, process_started=True,
                    classification="confirmed_environment_blocker",
                    details={"stage": "workspace_cleanup", "scratch": str(spec.scratch)}) from exc
            if context is not None:
                context["workspace_cleanup"] = "removed"
                diagnostic.with_suffix(".json").write_text(json.dumps(context, indent=2), encoding="utf-8")
