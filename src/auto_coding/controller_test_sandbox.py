"""Model-free controller tests in the validated Windows MXC sandbox."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from auto_coding.python_environment import test_environment
from auto_coding.reviewer_permissions import (
    ReviewerEnvironmentError, activated, check_cli, prepare_review,
)


class ControllerTestEnvironmentError(ValueError):
    def __init__(self, message, *, cleanup_confirmed=True, process_started=False):
        super().__init__(message)
        self.cleanup_confirmed = cleanup_confirmed
        self.process_started = process_started


# Trusted stdlib bootstrap ignores target imports (-I). The child receives the
# sanitized target PYTHONPATH. No guest supervisor: bounded_run owns the MXC tree.
_BOOTSTRAP = r'''
import json, os, subprocess, sys
command, marker, environment = json.loads(sys.argv[1])
os.environ.clear()
os.environ.update(environment)
try:
    result = {"exit_code": subprocess.call(command), "kind": "test"}
except OSError as error:
    result = {"kind": "environment", "error_type": type(error).__name__,
              "errno": error.errno, "winerror": getattr(error, "winerror", None)}
print("\n" + marker + json.dumps(result), flush=True)
'''


class _TestResult(tuple):
    def __new__(cls, code, output, timed=False, cleanup_confirmed=True, process_started=True):
        result = super().__new__(cls, (code, output, timed))
        result.cleanup_confirmed = cleanup_confirmed
        result.process_started = process_started
        return result


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
        filesystem = {":root": "deny", windows.as_posix(): "read", runtime.as_posix(): "read",
                      repo.as_posix(): "read", spec.scratch.as_posix(): "write"}
        name = "auto_coding_tests_" + uuid.uuid4().hex
        policy = 'permissions.' + name + '={extends=":read-only",filesystem={' + ','.join(
            json.dumps(key) + '=' + json.dumps(value) for key, value in filesystem.items()
        ) + '},network={enabled=false}}'
        settings = [f'default_permissions="{name}"', policy, 'windows.sandbox=mxc']
        environment = test_environment(repo)
        environment.update({key: str(spec.scratch / "tmp") for key in ("TEMP", "TMP", "TMPDIR")})
        home = spec.scratch / "codex-home"
        home.mkdir()
        environment.update(CODEX_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        executable = check_cli(settings, name, repo, spec.scratch, environment=environment,
                               filesystem=filesystem, backend="mxc")
        marker = "AUTO_CODING_TEST_RESULT_" + uuid.uuid4().hex + ":"
        argv = [executable]
        for setting in settings:
            argv.extend(["-c", setting])
        child_environment = {key: value for key, value in environment.items() if key != "CODEX_HOME"}
        argv.extend(["sandbox", "-P", name, "-C", str(repo), "--", sys.executable, "-I", "-B",
                     "-c", _BOOTSTRAP, json.dumps([command, marker, child_environment])])
        spec.argv, spec.environment = argv, environment
        return spec, marker
    except BaseException as exc:
        cleanup = getattr(exc, "cleanup_confirmed", None)
        clean = cleanup is True or (cleanup is None and (
            isinstance(exc, (ReviewerEnvironmentError, ControllerTestEnvironmentError, KeyError))
            or getattr(exc, "process_started", None) is False))
        if clean:
            spec.cleanup()
        raise ControllerTestEnvironmentError(
            f"Controller test sandbox preflight failed ({type(exc).__name__}); scratch: {spec.scratch}",
            cleanup_confirmed=clean, process_started=not clean) from exc


def run(command, repo, logs, timeout, *, git, launch):
    spec, marker = prepare(command, repo, logs, git)
    clean = False
    try:
        with activated(spec.environment):
            result = launch(spec.argv, repo, timeout)
        code, output, timed = result
        cleanup = getattr(result, "cleanup_confirmed", None)
        clean = cleanup is True or (cleanup is None and not timed and code not in (125, 127))
        if not clean or timed:
            # An unknown launch result must never look like a completed legacy
            # tuple to the controller's gate/recovery handling.
            return _TestResult(code, output, timed, clean, getattr(result, "process_started", None))
        prefix, separator, footer = output.rpartition("\n" + marker)
        try:
            evidence = json.loads(footer) if separator and code == 0 else None
        except ValueError:
            evidence = None
        if (not isinstance(evidence, dict) or evidence.get("kind") != "test"
                or type(evidence.get("exit_code")) is not int):
            diagnostic = Path(logs) / ("controller-test-sandbox-" + uuid.uuid4().hex + ".txt")
            diagnostic.write_text(output, encoding="utf-8")
            raise ControllerTestEnvironmentError(
                f"Sandbox startup/command environment failed; see {diagnostic}", process_started=True)
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
            spec.cleanup()
