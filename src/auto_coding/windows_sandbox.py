"""Experimental final-only adapter for the Phase 3 Windows Sandbox runner.

MXC remains the default. No target code runs on the host. Committed Git objects
qualify packaging only; protected before/after source attestation is not provided.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile

from auto_coding.controller_test_sandbox import ControllerTestEnvironmentError, _TestResult
from auto_coding.final_verification import verification_policy, policy_sha256, unittest_evidence
from auto_coding.process_runner import bounded_run, MAX_CAPTURE_BYTES
from auto_coding.sensitive_paths import is_sensitive_path
from auto_coding.python_environment import test_environment


BACKEND_ENV = "AUTOCODING_FINAL_TEST_BACKEND"
SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "windows_sandbox_poc"
GUEST = r"C:\AutoCodingTest"
WINDOWS = sys.platform == "win32"


def _regular(path):
    for parent in path.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked evidence directories are unsupported")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Evidence/source must be a regular file, not a link")


def _json(path):
    _regular(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Oversized Sandbox JSON evidence")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Sandbox evidence must be an object")
    return value


def _text(path):
    _regular(path)
    if path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("Oversized Sandbox output")
    return path.read_text(encoding="utf-8-sig")


def _identity(repo, git, head, branch):
    if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
            or git(repo, ["rev-parse", "HEAD"]) != head
            or git(repo, ["branch", "--show-current"]) != branch
            or git(repo, ["status", "--porcelain=v1", "--untracked-files=all"])):
        raise ValueError("Approved session HEAD/branch/clean worktree no longer match")
    for path in (repo, *repo.parents):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked/reparse source paths are unsupported")
    if not (repo / ".git").is_dir() or (repo / ".git").is_symlink():
        raise ValueError("A regular Git checkout is required")
    if getattr((repo / '.git').lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('Linked Git metadata is unsupported')
    index = git(repo, ['ls-files', '--stage', '-v', '-z'])
    if index and (not index.endswith('\0') or any(not re.fullmatch(
            r'H (100644|100755) [0-9a-f]{40,64} 0\t[^\0]+', entry)
            for entry in index[:-1].split('\0'))):
        raise ValueError('Unresolved index or hidden working-tree flags cannot authorize final testing')


def package_source(repo, destination, head, branch, git):
    """Package committed blobs, bypassing checkout filters and export attributes."""
    _identity(repo, git, head, branch)
    entries = git(repo, ["--no-replace-objects", "ls-tree", "-rz", "--full-tree", head])
    files, seen = {}, set()
    for entry in entries.rstrip("\0").split("\0"):
        match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40,64})\t(.+)", entry)
        if not match:
            raise ValueError("Snapshot requires regular committed files; links/submodules unsupported")
        name = match[3]
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith("/") or "\\" in name or ":" in name
                or any(ord(c) < 32 for c in name) or is_sensitive_path(name)
                or any(p in {".", ".."} or p.endswith((".", " ")) or "~" in p
                       or p.casefold() == ".codex" or p.casefold().split('.')[0] in {
                           'con', 'prn', 'aux', 'nul', *[f'com{i}' for i in range(10)],
                           *[f'lpt{i}' for i in range(10)]} for p in parts)
                or name.casefold() in seen):
            raise ValueError("Unsafe, sensitive or ambiguous committed source path")
        files[name] = match[2]
        seen.add(name.casefold())
        if len(files) > 50000:
            raise ValueError("Source contains too many files")
    # Latin-1 round trips binary output through the existing bounded runner's
    # text API. cat-file never runs smudge filters or honors export-ignore/subst.
    executable = shutil.which('git')
    if not executable or Path(executable).resolve().is_relative_to(repo):
        raise ValueError('A trusted host Git executable outside the target is required')
    batch = bounded_run([str(Path(executable).resolve()), '--no-replace-objects', 'cat-file', '--batch'], cwd=str(repo),
        input=''.join(oid + '\n' for oid in files.values()), capture_output=True,
        encoding='latin-1', timeout=120, check=False, shell=False)
    if batch.returncode != 0:
        raise ValueError('Git could not read the approved committed source objects')
    data, offset = batch.stdout.encode('latin-1'), 0
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = head.encode('ascii')
        for name, oid in files.items():
            end = data.index(b'\n', offset)
            fields = data[offset:end].decode('ascii').split(' ')
            if len(fields) != 3 or fields[:2] != [oid, 'blob'] or not fields[2].isdigit():
                raise ValueError('Git returned incomplete committed source objects')
            size = int(fields[2])
            offset = end + 1
            contents = data[offset:offset + size]
            offset += size
            if len(contents) != size or data[offset:offset + 1] != b'\n':
                raise ValueError('Incomplete committed source bytes')
            offset += 1
            archive.writestr(name, contents)
        if offset != len(data):
            raise ValueError('Unexpected Git object output')
    _identity(repo, git, head, branch)


def guest_command(command, repo):
    result = []
    for index, argument in enumerate(command):
        if index == 0 and (argument.casefold() in {"python", "python3", "python.exe", "python3.exe"}
                           or argument == sys.executable):
            result.append(r"C:\Python313Embed\python.exe")
            continue
        path = Path(argument)
        if path.is_absolute():
            try:
                relative = path.relative_to(repo)
            except ValueError:
                raise ValueError("Approved command refers to an unmapped host path") from None
            argument = str(PureWindowsPath(GUEST) / PureWindowsPath(*relative.parts))
        elif PureWindowsPath(argument).is_absolute() or PureWindowsPath(argument).drive:
            raise ValueError("Unmapped Windows command path")
        result.append(argument)
    return result


def guest_environment(repo, environment):
    # Preserve the existing non-secret runtime settings and in-project imports.
    # Host PATH, home, auth, Git configuration and credentials never cross over.
    portable = {'CI', 'NO_COLOR', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUTF8', 'PYTHONIOENCODING',
                'PYTHONHASHSEED', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM'}
    result = {key:value for key,value in environment.items() if key in portable}
    imports = [GUEST, GUEST + r'\src']
    for entry in environment.get('PYTHONPATH', '').split(os.pathsep):
        if not entry:
            continue
        relative = Path(entry).resolve().relative_to(repo)
        translated = str(PureWindowsPath(GUEST) / PureWindowsPath(*relative.parts))
        if translated not in imports:
            imports.append(translated)
    result.update(PYTHONPATH=';'.join(imports), AUTOCODING_CONFIG_DIR=r'C:\AutoCodingConfig')
    return result


def _timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Sandbox timestamps must be timezone aware")
    return parsed


def _instances(value):
    if not isinstance(value, list):
        raise ValueError('Malformed Sandbox instance list')
    ids = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('Id'), str):
            raise ValueError('Malformed Sandbox instance identity')
        ids.append(str(uuid.UUID(item['Id'])))
    return ids


def validate_evidence(root, request, *, require_pass=True):
    """Reparse controller-selected files; never follow a guest-selected host path."""
    summary = _json(root / "summary.json")
    result = _json(root / "results" / "baseline.json")
    if (summary.get("mode") != "Session" or summary.get("runId") != request["runId"]
            or summary.get("sandboxId") != request["sandboxId"]
            or summary.get("requestedSandboxId") != request["sandboxId"]
            or summary.get("baseline") != result or summary.get("cleanupVerified") is not True):
        raise ValueError("Sandbox summary identity, cleanup or guest result is invalid")
    after = _instances(_json(root / "after.output.txt").get("WindowsSandboxEnvironments"))
    if request["sandboxId"].casefold() in after:
        raise ValueError("Owned Sandbox absence was not established")
    if _json(root / "start.output.txt").get("Id") != request["sandboxId"]:
        raise ValueError("Launch did not return the owned Sandbox ID")
    if type(result.get('suiteIndex')) is not int:
        raise ValueError('Malformed suite index')
    for key in ("runId", "sourceCommit", "sourceBranch", "policySha256", "suiteIndex", "sandboxId"):
        if result.get(key) != request[key]:
            raise ValueError("Stale or incorrect Sandbox source/policy identity")
    if (not isinstance(result.get("account"), str) or not result["account"]
            or not isinstance(result.get("computerName"), str) or not result["computerName"]
            or not re.fullmatch(r"[0-9a-f]{40}", result.get("disposableCommit") or "")
            or result.get("versions") != {"git": "git version 2.53.0.windows.4", "python": "Python 3.13.17", "node": "v26.13.1"}
            or _timestamp(result["finishedUtc"]) < _timestamp(result["startedUtc"])):
        raise ValueError("Missing or malformed guest environment evidence")
    cli = _json(root / "exec-baseline.output.txt")
    if (type(cli.get("ExitCode")) is not int or type(result.get("operationExitCode")) is not int
            or cli["ExitCode"] != result["operationExitCode"]):
        raise ValueError("CLI and guest exit codes disagree")
    for label in ("start", "exec-baseline", "stop", "after"):
        process = _json(root / (label + ".process.json"))
        if type(process.get("exitCode")) is not int or process["exitCode"] != 0 or process.get("timedOut") is not False:
            raise ValueError("Host CLI process failed or timed out")
        if label == 'exec-baseline' and not (
                _timestamp(process['startedUtc']) - dt.timedelta(seconds=5) <= _timestamp(result['startedUtc'])
                <= _timestamp(result['finishedUtc']) <= _timestamp(process['finishedUtc']) + dt.timedelta(seconds=5)):
            raise ValueError('Stale or impossible guest execution timestamps')
    suites = result.get("suites")
    if not isinstance(suites, list) or len(suites) != 1:
        raise ValueError("Missing or unexpected suite results")
    suite = suites[0]
    name = f"session-suite-{request['suiteIndex']}"
    if not isinstance(suite, dict) or suite.get("file") != name:
        raise ValueError("Incorrect suite identity")
    process = _json(root / "results" / (name + ".process.json"))
    if (process != suite.get("process") or _json(root / "results" / (name + ".suite.json")) != suite
            or [process.get("executable"), *process.get("arguments", [])] != request["command"]
            or process.get("workingDirectory") != GUEST
            or process.get("stdout") != rf"C:\ACResults\{name}.stdout.log"
            or process.get("stderr") != rf"C:\ACResults\{name}.stderr.log"
            or type(process.get("exitCode")) is not int or type(process.get("timedOut")) is not bool
            or not 1 <= process.get("timeoutSeconds", 0) <= request["policy"]["timeout_seconds"]
            or not (_timestamp(result["startedUtc"]) <= _timestamp(process["startedUtc"])
                    <= _timestamp(process["finishedUtc"]) <= _timestamp(result["finishedUtc"]))):
        raise ValueError("Malformed or contradictory suite process evidence")
    stdout = _text(root / "results" / (name + ".stdout.log"))
    stderr = _text(root / "results" / (name + ".stderr.log"))
    output = stdout + "\n" + stderr
    report = unittest_evidence(output)
    matches = list(re.finditer(r"(?m)^Ran ([0-9]{1,7}) tests? in [0-9.]+s\r?$", output))
    verdict = output[matches[-1].end():].lstrip("\r\n").split("\n", 1)[0].rstrip("\r") if matches else ""
    counts = {}
    for field, label in (("failures", "failures"), ("errors", "errors"), ("skips", "skipped")):
        match = re.search(r"\b" + label + r"=([0-9]+)", verdict)
        counts[field] = int(match[1]) if match else 0
    clean_pass = bool(report and report["successful"] and not report["skipped"]
                      and report["tests"] > 0 and verdict == "OK")
    expected_status = "TIMEOUT" if process["timedOut"] else "PASS" if process["exitCode"] == 0 and clean_pass else "FAIL"
    success = expected_status == "PASS"
    if (not report or type(result.get("totalTests")) is not int
            or any(type(suite.get(key)) is not int for key in ("tests", "failures", "errors", "skips"))
            or suite["tests"] != report["tests"] or result.get("totalTests") != report["tests"]
            or any(suite[key] != counts[key] for key in counts)
            or suite.get("status") != expected_status or result.get("success") is not success
            or cli["ExitCode"] != (0 if success else 1)
            or summary.get("outcome") != ("PASS" if success else "FAIL")
            or (require_pass and (not success or summary.get("failure") is not None or result.get("error") is not None))):
        raise ValueError("Failed, skipped, incomplete or inconsistent unittest evidence")
    return output if require_pass else (process["exitCode"], output, process["timedOut"])


def _rescue_cleanup(root, request, launch):
    """A host timeout can kill the CLI client without killing its guest."""
    try:
        marker_path = root / 'launch-attempt.json'
        if not marker_path.exists():
            return True  # Trusted runner cannot launch before its atomic marker.
        marker = _json(marker_path)
        if marker != {"runId": request["runId"], "sandboxId": request["sandboxId"]}:
            return False
        executable = shutil.which("wsb")
        if not executable:
            return False
        host_clean = True
        for label, args, timeout in (("rescue-stop", ["stop", "--id", request["sandboxId"], "--raw"], 120),
                                      ("rescue-list", ["list", "--raw"], 30)):
            try:
                process = launch([executable, *args], cwd=str(SCRIPTS), timeout=timeout, shell=False,
                                 capture_output=True, text=True, encoding="utf-8")
            except (OSError, subprocess.TimeoutExpired) as exc:
                host_clean = host_clean and (getattr(exc, 'cleanup_confirmed', None) is True
                                             or getattr(exc, 'process_started', None) is False)
                (root / (label + '.error.txt')).write_text(str(exc), encoding='utf-8')
                if label == 'rescue-stop':
                    continue  # Still verify absence after a failed/timed-out stop.
                return False
            (root / (label + ".json")).write_text(json.dumps({"argv": [executable, *args],
                "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}), encoding="utf-8")
            if label == "rescue-list":
                items = json.loads(process.stdout)["WindowsSandboxEnvironments"]
                return host_clean and process.returncode == 0 and request['sandboxId'].casefold() not in _instances(items)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return False


def _confirmed_cleanup(root, request):
    try:
        process = _json(root / 'after.process.json')
        items = _json(root / 'after.output.txt')['WindowsSandboxEnvironments']
        return (type(process.get('exitCode')) is int and process['exitCode'] == 0 and process.get('timedOut') is False
                and request['sandboxId'].casefold() not in _instances(items))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def saved_evidence(logs, receipt, *, head, branch, policy, index, run_id, invocation):
    if not isinstance(receipt, str) or not re.fullmatch(r"windows-sandbox-[0-9a-f]{32}", receipt):
        raise ValueError("Missing Sandbox evidence receipt")
    root = logs / receipt
    if root.is_symlink() or root.resolve().parent != logs.resolve():
        raise ValueError("Invalid Sandbox evidence directory")
    request = _json(root / "request.json")
    if (request.get("sourceCommit") != head or request.get("sourceBranch") != branch
            or request.get("policy") != policy or request.get("policySha256") != policy_sha256(policy)
            or request.get("suiteIndex") != index or request.get("sessionRunId") != run_id
            or request.get("invocation") != invocation
            or request.get("approvedCommand") != policy["test_commands"][index - 1]):
        raise ValueError("Sandbox receipt does not belong to this approved final verification")
    return validate_evidence(root, request)


def run(command, repo, logs, timeout, *, git, expected_head, expected_branch, policy,
        index, session_run_id, invocation, environment=None, launch=bounded_run):
    started = False
    host_finished = False
    root = None
    try:
        policy = verification_policy(policy)
        if command != policy["test_commands"][index - 1] or timeout != policy["timeout_seconds"]:
            raise ValueError("Final suite differs from the approved policy")
        if not WINDOWS:
            raise ValueError("Windows Sandbox final verification requires Windows")
        repo, logs = Path(repo).resolve(), Path(logs).resolve()
        if logs == repo or logs.is_relative_to(repo):
            raise ValueError("Evidence must be outside the source checkout")
        powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file() or not shutil.which("wsb") or not (SCRIPTS / "run.ps1").is_file():
            raise ValueError("Installed Windows Sandbox CLI/PowerShell/Phase 3 runner unavailable")
        root = logs / ("windows-sandbox-" + uuid.uuid4().hex)
        root.mkdir()
        (root / "input").mkdir()
        package_source(repo, root / "input/baseline.zip", expected_head, expected_branch, git)
        request = {"runId": str(uuid.uuid4()), "sandboxId": str(uuid.uuid4()), "sourceCommit": expected_head,
                   "sourceBranch": expected_branch, "policy": policy, "policySha256": policy_sha256(policy),
                   "suiteIndex": index, "command": guest_command(command, repo), "approvedCommand": command,
                   "sessionRunId": session_run_id, "invocation": invocation,
                   "environment": guest_environment(repo, environment if environment is not None else test_environment(repo))}
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
        # Paths are literal single-quoted PowerShell strings; no shell interpolation.
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        code = (f"& ([scriptblock]::Create([IO.File]::ReadAllText({quote(SCRIPTS / 'run.ps1')}))) "
                f"-SourceDirectory {quote(SCRIPTS)} -Mode Session -RequestPath {quote(request_path)}")
        argv = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(code.encode("utf-16-le")).decode("ascii")]
        started = True
        process = launch(argv, cwd=str(SCRIPTS), timeout=timeout + 1500, shell=False,
                         capture_output=True, text=True, encoding="utf-8")
        host_finished = True
        (root / "adapter.stdout.log").write_text(process.stdout or "", encoding="utf-8")
        (root / "adapter.stderr.log").write_text(process.stderr or "", encoding="utf-8")
        (root / "adapter.process.json").write_text(json.dumps({"argv": argv, "exit_code": process.returncode}), encoding="utf-8")
        summary = _json(root / "summary.json")
        clean = _confirmed_cleanup(root, request)
        if not clean:
            clean = _rescue_cleanup(root, request, launch)
        try:
            code, output, timed = validate_evidence(root, request, require_pass=False)
            if process.returncode != (0 if summary.get('outcome') == 'PASS' else 1):
                raise ValueError("PowerShell runner failed despite passing guest evidence")
            _identity(repo, git, expected_head, expected_branch)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            # Failed evidence cannot pass even if both native host exits are zero.
            diagnostic = f"Windows Sandbox verification rejected: {exc}; evidence: {root}"
            suites = (summary.get('baseline') or {}).get('suites') or []
            timed = any(isinstance(item, dict) and isinstance(item.get('process'), dict)
                        and item['process'].get('timedOut') is True for item in suites)
            result = _TestResult(124 if timed else 127, diagnostic, timed, clean, True)
        else:
            result = _TestResult(code, output, timed, True, True)
        result.windows_sandbox = root.name
        return result
    except BaseException as exc:
        if started:
            guest_clean = _rescue_cleanup(root, request, launch)
            clean = guest_clean and (host_finished or getattr(exc, 'cleanup_confirmed', None) is True)
        else:
            clean = getattr(exc, 'cleanup_confirmed', None) is not False
        if isinstance(exc, subprocess.TimeoutExpired):
            result = _TestResult(124, f"Windows Sandbox host execution TIMEOUT; evidence: {root}", True, clean, True)
            result.windows_sandbox = root.name if root is not None else None
            return result
        # Never infer Sandbox termination from the host process exit alone.
        raise ControllerTestEnvironmentError(
            f"Windows Sandbox final verification could not complete: {exc}; evidence: {root}",
            cleanup_confirmed=clean, process_started=started,
            details={"stage": "windows_sandbox_final", "evidence": str(root)}) from exc
