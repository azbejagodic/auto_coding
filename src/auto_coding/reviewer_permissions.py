"""Reviewer-only permissions, checked against the CLI's loaded configuration."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from auto_coding.process_runner import bounded_run
from auto_coding.python_environment import target_environment


_environment = ContextVar("reviewer_environment", default=None)


class ReviewerEnvironmentError(ValueError):
    pass


def command_environment(repo):
    return _environment.get() or target_environment(repo)


@contextmanager
def activated(environment):
    token = _environment.set(environment)
    try:
        yield
    finally:
        _environment.reset(token)


# This stdlib-only helper runs in the trusted engine directory, inside bounded_run's
# owned process tree. Never import target code while inspecting configuration.
_CONFIG_CLIENT = r'''
import json, queue, subprocess, sys, threading, time
command, cwd, profile = json.loads(sys.argv[1])
process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
responses = queue.Queue(maxsize=16)
def read():
    while True:
        line = process.stdout.readline(1048577)
        if not line: break
        if len(line) > 1048576: responses.put({"error": "Oversized config response"}); break
        try: value = json.loads(line)
        except ValueError: continue
        if "id" in value: responses.put(value)
threading.Thread(target=read, daemon=True).start()
def request(value):
    process.stdin.write(json.dumps(value) + "\n"); process.stdin.flush()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = responses.get(timeout=max(.01, deadline-time.monotonic()))
        if response.get("id") == value["id"]:
            if "error" in response: raise ValueError("Configuration RPC failed")
            return response["result"]
    raise TimeoutError("Configuration RPC did not respond")
request({"id":1,"method":"initialize","params":{"clientInfo":{"name":"auto_coding_review_preflight",
        "version":"1"},"capabilities":{"experimentalApi":True}}})
process.stdin.write('{"method":"initialized","params":{}}\n'); process.stdin.flush()
value = request({"id":2,"method":"config/read","params":{"includeLayers":True,"cwd":cwd}})
requirements = request({"id":3,"method":"configRequirements/read","params":{}})
config = value["config"]
# Do not export credentials, providers, prompts or unrelated configuration.
result = {"legacy": {key:config.get(key) for key in ("sandbox_mode","sandbox_workspace_write")},
          "default":config.get("default_permissions"),
          "profile":config.get("permissions",{}).get(profile),
          "windows_sandbox":(config.get("windows") or {}).get("sandbox"),
          "layers":[], "requirements":requirements.get("requirements")}
for layer in value["layers"]:
    result["layers"].append({"source":layer["name"], "legacy":{
        key:layer["config"].get(key) for key in ("sandbox_mode","sandbox_workspace_write")}})
process.stdin.close(); process.wait(timeout=5)
print(json.dumps(result))
'''


def validate_configuration(value, name, repo, scratch, *, filesystem=None, backend=None):
    try:
        if not isinstance(value["layers"], list) or not value["layers"]:
            raise ReviewerEnvironmentError("Loaded configuration layers were not reported")
        for layer in [value, *value["layers"]]:
            legacy = layer["legacy"]
            if not isinstance(legacy, dict) or set(legacy) != {"sandbox_mode", "sandbox_workspace_write"}:
                raise ReviewerEnvironmentError("Cannot verify loaded legacy configuration fields")
            if any(legacy.get(key) is not None for key in ("sandbox_mode", "sandbox_workspace_write")):
                raise ReviewerEnvironmentError("Legacy sandbox settings conflict with reviewer permissions in "
                                               + str(layer.get("source", "effective configuration")))
        if value["requirements"] is not None:
            raise ReviewerEnvironmentError("Managed reviewer permissions require separate validation; no reviewer launched")
        profile = value["profile"]
        loaded_filesystem = {key:item for key,item in profile["filesystem"].items() if item is not None}
        network = {key:item for key,item in profile["network"].items() if item is not None}
        if (value["default"] != name or profile["extends"] != ":read-only"
                or profile.get("workspace_roots") not in (None, {})
                or loaded_filesystem != (filesystem if filesystem is not None else {
                    repo.as_posix(): "read", scratch.as_posix(): "write"})
                or (backend is not None and value.get("windows_sandbox") != backend)
                or network != {"enabled": False}):
            raise ReviewerEnvironmentError("Effective reviewer profile differs from the required read-only target / writable scratch policy")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ReviewerEnvironmentError("Cannot verify effective reviewer configuration") from exc


def check_cli(settings, name, repo, scratch, *, environment=None, filesystem=None, backend=None):
    executable = shutil.which("codex")
    if not executable:
        raise ReviewerEnvironmentError("Codex CLI is unavailable")
    trusted = Path(__file__).resolve().parents[1]
    environment = dict(os.environ if environment is None else environment)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = str(trusted)
    version = bounded_run([executable, "--version"], cwd=str(trusted), env=environment,
                          capture_output=True, text=True, timeout=15)
    if version.returncode or version.stdout.strip() != "codex-cli 0.163.0":
        raise ReviewerEnvironmentError("Reviewer profiles are validated only for Codex CLI 0.163.0; validate this CLI before use")
    command = [executable, "app-server", "--strict-config"]
    for setting in settings:
        command.extend(["-c", setting])
    result = bounded_run([sys.executable, "-B", "-c", _CONFIG_CLIENT,
                          json.dumps([command, str(repo), name])], cwd=str(trusted), env=environment,
                         capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ReviewerEnvironmentError("Codex effective-configuration inspection failed; no reviewer launched")
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise ReviewerEnvironmentError("Invalid Codex effective-configuration response") from exc
    validate_configuration(value, name, repo, scratch, filesystem=filesystem, backend=backend)
    return executable


@dataclass
class ReviewPermissions:
    scratch: Path
    logs: Path
    argv: list[str]
    environment: dict[str, str]
    instructions: str
    identity: tuple[int, int]
    executable: str = "codex"
    profile: str = ""
    settings: tuple[str, ...] = ()

    def cleanup(self):
        current = self.scratch.lstat()
        if (not self.identity[1] or (current.st_dev, current.st_ino) != self.identity
                or self.scratch.is_symlink() or self.scratch.resolve().parent != self.logs):
            raise ReviewerEnvironmentError("Reviewer scratch ownership/path changed; retained for inspection")
        shutil.rmtree(self.scratch)


def prepare_review(argv, repo, logs, git, *, check=check_cli):
    repo, logs = Path(repo).resolve(), Path(logs).resolve()
    # Check both per-worktree and common metadata, never infer safety from .git's spelling.
    for option in ("--absolute-git-dir", "--git-common-dir"):
        raw = git(repo, ["rev-parse", option])
        if not raw or any(character in raw for character in "\n\r\0"):
            raise ReviewerEnvironmentError("Ambiguous Git metadata location")
        metadata = (repo / raw).resolve(strict=True)
        if not metadata.is_dir() or not metadata.is_relative_to(repo):
            raise ReviewerEnvironmentError("Linked/external Git metadata is not supported by reviewer permissions; use a regular checkout with contained .git metadata")
    if logs == repo or logs.is_relative_to(repo):
        raise ReviewerEnvironmentError("Reviewer scratch storage overlaps the target repository")
    scratch = Path(tempfile.mkdtemp(prefix="review-scratch-", dir=logs)).resolve()
    identity = scratch.lstat()
    spec = ReviewPermissions(scratch, logs, [], {}, "", (identity.st_dev, identity.st_ino))
    try:
        (scratch / "tmp").mkdir()
        environment = target_environment(repo)
        variables = {key: str(scratch / "tmp") for key in ("TEMP", "TMP", "TMPDIR")}
        variables.update(PYTHONPATH=environment["PYTHONPATH"], PYTHONDONTWRITEBYTECODE="1", GIT_OPTIONAL_LOCKS="0")
        environment.update(variables)
        name = "auto_coding_review_" + uuid.uuid4().hex
        policy = 'permissions.' + name + '={extends=":read-only",filesystem={' + json.dumps(repo.as_posix()) + '= "read",' + json.dumps(scratch.as_posix()) + '= "write"},network={enabled=false}}'
        settings = [f'default_permissions="{name}"', policy,
                    'shell_environment_policy.set={' + ','.join(key+'='+json.dumps(item) for key,item in variables.items()) + '}',
                    'shell_environment_policy.experimental_use_profile=false']
        executable = check(settings, name, repo, scratch) if check else (argv[0] if argv else "codex")
        spec.executable, spec.profile, spec.settings = executable, name, tuple(settings)
        spec.environment = environment
        if argv is None:
            return spec  # Workflow preflight reuses policy validation without building an agent request.
        command = list(argv[:-1])
        index = command.index("--sandbox")
        if command[index + 1] != "read-only":
            raise ReviewerEnvironmentError("Expected the reviewer-only command")
        del command[index:index + 2]
        if any(part in {"--sandbox", "-s", "--add-dir", "--config", "-c",
                        "--dangerously-bypass-approvals-and-sandbox"} for part in command):
            raise ReviewerEnvironmentError("Unexpected permission/configuration overrides in reviewer command")
        command[0] = executable
        command.extend(["--strict-config", "--cd", str(repo)])
        for setting in settings:
            command.extend(["-c", setting])
        spec.argv = [*command, argv[-1]]
        spec.instructions = (f"\n\nCODE REVIEW ENVIRONMENT: target cwd is {repo}; writable scratch is {scratch}; "
            f"TEMP/TMP/TMPDIR must resolve to {scratch / 'tmp'}. Keep the supplied target PYTHONPATH. "
            "Do not modify source or Git metadata. Do not execute tests, linting, builds or verification commands. "
            "Existing tests and Codex A's test report may be read as context; the report contains implementer claims. "
            "Report verification={status,details} with status=not_run: explain that testing is intentionally assigned "
            "to Codex A and the final controller suite. Do not require extra coverage or independent test execution "
            "for approval. Put concrete implementation defects or unmet task requirements in issues; otherwise "
            "use APPROVED with empty issues. Never claim unexecuted tests passed.")
        return spec
    except BaseException as exc:
        # A failed config probe may itself have unverified descendants. Retain its scratch.
        if (isinstance(exc, ReviewerEnvironmentError) or getattr(exc, "process_started", None) is False
                or getattr(exc, "cleanup_confirmed", False) is True):
            try:
                spec.cleanup()
            except OSError:
                pass
        raise
