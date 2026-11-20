"""Experimental final-only adapter for the Phase 3 Windows Sandbox runner.

MXC remains the default. No target code runs on the host. Host SHA-256 baselines
qualify packaging and cooperative guest inventories, not malicious-guest attestation.
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
from auto_coding import source_integrity, trusted_sandbox_management


BACKEND_ENV = "AUTOCODING_FINAL_TEST_BACKEND"
SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "windows_sandbox_poc"
GUEST = r"C:\AutoCodingTest"
WINDOWS = sys.platform == "win32"
SESSION_SCOPE = 'approved-session-v2'


class _SessionEvidence(tuple):
    def __new__(cls, code, output, timed, integrity, suites):
        value = super().__new__(cls, (code, output, timed, integrity))
        value.suites = suites
        return value


def _session_request(request, policy):
    if 'session_coverage' not in policy:
        raise ValueError('Missing approved session coverage policy')
    selection = request.get('sessionSelection')
    commands = request.get('commands')
    if (request.get('executionScope') != SESSION_SCOPE or type(request.get('suiteIndex')) is not int
            or request['suiteIndex'] != 1
            or request.get('policySha256') != policy_sha256(policy)
            or request.get('approvedCommands') != policy['test_commands']
            or not isinstance(commands, list) or len(commands) != len(policy['test_commands'])
            or any(not isinstance(command, list) or not command
                   or any(not isinstance(arg, str) or not arg for arg in command) for command in commands)
            or not isinstance(selection, dict)
            or not isinstance(request.get('sourceStartCommit'), str)
            or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', request['sourceStartCommit'])
            or request.get('selectionSha256') != source_integrity.digest(selection)
            or selection.get('finalCommit') != request.get('sourceCommit')
            or selection.get('initialCommit') != request.get('sourceStartCommit')
            or selection.get('branch') != request.get('sourceBranch')
            or selection.get('sessionRunId') != request.get('sessionRunId')
            or selection.get('policySha256') != request.get('policySha256')
            or selection.get('selectedSuiteIndices') != list(range(1, len(commands) + 1))
            or selection.get('minimumTestsPerSuite') != policy.get('session_coverage', {}).get('minimum_tests_per_suite')
            or selection.get('mandatorySecuritySuites') != policy.get('session_coverage', {}).get('mandatory_security_suites')):
        raise ValueError('Incomplete or unbound approved session selection')


def _session_suites(root, request, summary, result, integrity, require_pass):
    if (result.get('executionScope') != SESSION_SCOPE or result.get('selectionSha256') != request['selectionSha256']
            or result.get('sourceStartCommit') != request['sourceStartCommit']):
        raise ValueError('Guest session selection identity differs from the approved request')
    suites = result.get('suites')
    if not isinstance(suites, list) or len(suites) != len(request['commands']):
        raise ValueError('Missing or duplicated mandatory session suites')
    payloads, total, passed = [], 0, True
    previous = _timestamp(result['startedUtc'])
    minimum = request['sessionSelection']['minimumTestsPerSuite']
    for index, (suite, command) in enumerate(zip(suites, request['commands']), 1):
        name = f'session-suite-{index}'
        if not isinstance(suite, dict) or suite.get('file') != name:
            raise ValueError('Incorrect session suite order or identity')
        process = _json(root / 'results' / (name + '.process.json'))
        if (process != suite.get('process') or _json(root / 'results' / (name + '.suite.json')) != suite
                or [process.get('executable'), *process.get('arguments', [])] != command
                or process.get('workingDirectory') != GUEST
                or process.get('stdout') != rf'C:\ACResults\{name}.stdout.log'
                or process.get('stderr') != rf'C:\ACResults\{name}.stderr.log'
                or type(process.get('exitCode')) is not int or type(process.get('timedOut')) is not bool
                or type(process.get('timeoutSeconds')) is not int
                or not 1 <= process['timeoutSeconds'] <= request['policy']['timeout_seconds']
                or not (previous <= _timestamp(process['startedUtc']) <= _timestamp(process['finishedUtc'])
                        <= _timestamp(result['finishedUtc']))):
            raise ValueError('Malformed or contradictory session suite process')
        previous = _timestamp(process['finishedUtc'])
        output = _text(root / 'results' / (name + '.stdout.log')) + '\n' + _text(root / 'results' / (name + '.stderr.log'))
        report = unittest_evidence(output)
        matches = list(re.finditer(r'(?m)^Ran ([0-9]{1,7}) tests? in [0-9.]+s\r?$', output))
        verdict = output[matches[-1].end():].lstrip('\r\n').split('\n', 1)[0].rstrip('\r') if matches else ''
        counts = {}
        for field, label in (('failures', 'failures'), ('errors', 'errors'), ('skips', 'skipped')):
            match = re.search(r'\b' + label + r'=([0-9]+)', verdict)
            counts[field] = int(match[1]) if match else 0
        clean = bool(report and report['successful'] and report['tests'] > 0 and verdict == 'OK')
        status = 'TIMEOUT' if process['timedOut'] else 'PASS' if process['exitCode'] == 0 and clean else 'FAIL'
        if (not report or any(type(suite.get(key)) is not int for key in ('tests', 'failures', 'errors', 'skips'))
                or suite['tests'] != report['tests'] or any(suite[key] != counts[key] for key in counts)
                or suite.get('status') != status):
            raise ValueError('Incomplete or inconsistent session unittest evidence')
        total += report['tests']
        passed = passed and status == 'PASS' and report['tests'] >= minimum[index - 1]
        payloads.append((process['exitCode'], output, process['timedOut']))
    passed = passed and total >= request['policy']['minimum_tests'] and integrity['verified']
    if (type(result.get('totalTests')) is not int or result['totalTests'] != total
            or result.get('success') is not passed or result['operationExitCode'] != (0 if passed else 1)
            or summary.get('outcome') != ('PASS' if passed else 'FAIL')
            or 'failure' not in summary or 'error' not in result
            or (passed and (summary['failure'] is not None or result['error'] is not None))
            or (require_pass and not passed)):
        raise ValueError('Failed, skipped or contradictory complete session evidence')
    output = '\n'.join(payload[1] for payload in payloads)
    if len(output.encode('utf-8')) > MAX_CAPTURE_BYTES:
        raise ValueError('Combined session output exceeds the evidence limit')
    return output if require_pass else _SessionEvidence(result['operationExitCode'], output,
        any(payload[2] for payload in payloads), integrity, payloads)


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
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=source_integrity._pairs)
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
    files, seen, protected = {}, set(), []
    for entry in entries.rstrip("\0").split("\0"):
        match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40,64})\t(.+)", entry)
        if not match:
            raise ValueError("Snapshot requires regular committed files; links/submodules unsupported")
        name = source_integrity.safe_path(match[3])
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
            protected.append(source_integrity.file_record(name, contents))
        if offset != len(data):
            raise ValueError('Unexpected Git object output')
    _identity(repo, git, head, branch)
    return source_integrity.records(protected)


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
    policy = verification_policy(request['policy'])
    index = request.get('suiteIndex')
    whole_session = request.get('executionScope') == SESSION_SCOPE
    if whole_session:
        _session_request(request, policy)
    elif ('executionScope' in request or type(index) is not int or not 1 <= index <= len(policy['test_commands'])
            or request.get('policySha256') != policy_sha256(policy)
            or request.get('approvedCommand') != policy['test_commands'][index - 1]):
        raise ValueError('Receipt does not identify the approved policy command')
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
    integrity = source_integrity.verify_receipt(root, request, result, _json)
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
        if label in {'stop', 'after'}:
            expected = ['stop','--id',request['sandboxId'],'--raw'] if label == 'stop' else ['list','--raw']
            if process.get('arguments') != expected:
                raise ValueError('Cleanup evidence does not target only the owned Sandbox')
        if label == 'exec-baseline' and not (
                _timestamp(process['startedUtc']) - dt.timedelta(seconds=5) <= _timestamp(result['startedUtc'])
                <= _timestamp(result['finishedUtc']) <= _timestamp(process['finishedUtc']) + dt.timedelta(seconds=5)):
            raise ValueError('Stale or impossible guest execution timestamps')
    if whole_session:
        return _session_suites(root, request, summary, result, integrity, require_pass)
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
            or type(process.get('timeoutSeconds')) is not int
            or not 1 <= process['timeoutSeconds'] <= request["policy"]["timeout_seconds"]
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
    success = expected_status == "PASS" and integrity['verified']
    if (not report or type(result.get("totalTests")) is not int
            or any(type(suite.get(key)) is not int for key in ("tests", "failures", "errors", "skips"))
            or suite["tests"] != report["tests"] or result.get("totalTests") != report["tests"]
            or any(suite[key] != counts[key] for key in counts)
            or suite.get("status") != expected_status or result.get("success") is not success
            or cli["ExitCode"] != (0 if success else 1)
            or summary.get("outcome") != ("PASS" if success else "FAIL")
            or 'failure' not in summary or 'error' not in result
            or (success and (summary['failure'] is not None or result['error'] is not None))
            or (require_pass and not success)):
        raise ValueError("Failed, skipped, incomplete or inconsistent unittest evidence")
    return output if require_pass else (process["exitCode"], output, process["timedOut"], integrity)


def _rescue_cleanup(root, request, launch):
    """A host timeout can kill the CLI client without killing its guest."""
    try:
        marker_path = root / 'launch-attempt.json'
        if not marker_path.exists():
            return True  # Trusted runner cannot launch before its atomic marker.
        marker = _json(marker_path)
        if marker != {"runId": request["runId"], "sandboxId": request["sandboxId"]}:
            return False
        manager = getattr(launch, '__self__', None)
        managed = isinstance(manager, trusted_sandbox_management.ManagementSession)
        executable = manager.cli if managed else shutil.which("wsb")
        if not executable:
            return False
        host_clean = True
        for label, args, timeout in (("rescue-stop", ["stop", "--id", request["sandboxId"], "--raw"], 120),
                                      ("rescue-list", ["list", "--raw"], 30)):
            try:
                process = launch([executable, *args], cwd=str(manager.scripts if managed else SCRIPTS), timeout=timeout, shell=False,
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


def saved_evidence(logs, receipt, *, head, branch, policy, index, run_id, invocation,
                   integrity_verdict=None, source_repo=None):
    if not isinstance(receipt, str) or not re.fullmatch(r"windows-sandbox-[0-9a-f]{32}", receipt):
        raise ValueError("Missing Sandbox evidence receipt")
    root = logs / receipt
    if root.is_symlink() or root.resolve().parent != logs.resolve():
        raise ValueError("Invalid Sandbox evidence directory")
    request = _json(root / "request.json")
    source = Path(source_repo).resolve() if source_repo is not None else Path.cwd().resolve()
    if (request.get("sourceCommit") != head or request.get("sourceBranch") != branch
            or request.get("policy") != policy or request.get("policySha256") != policy_sha256(policy)
            or request.get("suiteIndex") != index or request.get("sessionRunId") != run_id
            or request.get("invocation") != invocation
            or request.get("approvedCommand") != policy["test_commands"][index - 1]
            or request.get('command') != guest_command(policy['test_commands'][index - 1], source)):
        raise ValueError("Sandbox receipt does not belong to this approved final verification")
    output = validate_evidence(root, request)
    actual = source_integrity.verify_receipt(root, request, _json(root / 'results/baseline.json'), _json)
    if (_json(root / 'integrity-verification.json') != actual
            or (integrity_verdict is not None and integrity_verdict != actual)):
        raise source_integrity.IntegrityError('Saved host integrity verdict is missing or contradictory')
    return output


def saved_session_evidence(logs, receipt, *, head, branch, policy, run_id, invocation,
                           selection, source_repo, integrity_verdict):
    if not isinstance(receipt, str) or not re.fullmatch(r'windows-sandbox-[0-9a-f]{32}', receipt):
        raise ValueError('Missing complete-session Sandbox receipt')
    root = logs / receipt
    if root.is_symlink() or root.resolve().parent != logs.resolve():
        raise ValueError('Invalid Sandbox evidence directory')
    request = _json(root / 'request.json')
    expected_commands = [guest_command(command, Path(source_repo).resolve()) for command in policy['test_commands']]
    if (request.get('executionScope') != SESSION_SCOPE or request.get('sourceCommit') != head
            or request.get('sourceBranch') != branch or request.get('policy') != policy
            or request.get('sessionRunId') != run_id or request.get('invocation') != invocation
            or request.get('sessionSelection') != selection or request.get('commands') != expected_commands):
        raise ValueError('Receipt does not belong to this complete approved session')
    evidence = validate_evidence(root, request, require_pass=False)
    if evidence[0] != 0 or evidence[2] or not source_integrity.valid_verdict(evidence[3]):
        raise ValueError('Saved complete session did not pass')
    if _json(root / 'integrity-verification.json') != evidence[3] or integrity_verdict != evidence[3]:
        raise source_integrity.IntegrityError('Saved complete-session integrity verdict differs')
    return [payload[1] for payload in evidence.suites]


def run_session(commands, repo, logs, timeout, *, selection, **options):
    """One owned instance, one package, all bound commands and one pre/post scan."""
    return run(commands, repo, logs, timeout, index=1, session_selection=selection, **options)


def run(command, repo, logs, timeout, *, git, expected_head, expected_branch, policy,
        index, session_run_id, invocation, environment=None, launch=None, session_selection=None):
    started = False
    host_finished = False
    root = None
    manager = None
    try:
        policy = verification_policy(policy)
        whole_session = session_selection is not None
        expected = policy['test_commands'] if whole_session else policy['test_commands'][index - 1]
        if command != expected or timeout != policy["timeout_seconds"]:
            raise ValueError("Final suite differs from the approved policy")
        budget = timeout * (len(command) if whole_session else 1)
        if budget > 86400:
            raise ValueError('Combined session command deadlines exceed the existing bounded runner capacity')
        if not WINDOWS:
            raise ValueError("Windows Sandbox final verification requires Windows")
        repo, logs = Path(repo).resolve(), Path(logs).resolve()
        if logs == repo or logs.is_relative_to(repo):
            raise ValueError("Evidence must be outside the source checkout")
        if launch is not None:
            powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            if not powershell.is_file() or not shutil.which("wsb") or not (SCRIPTS / "run.ps1").is_file():
                raise ValueError("Installed Windows Sandbox CLI/PowerShell/Phase 3 runner unavailable")
        root = logs / ("windows-sandbox-" + uuid.uuid4().hex)
        root.mkdir()
        (root / "input").mkdir()
        request = {"runId": str(uuid.uuid4()), "sandboxId": str(uuid.uuid4()), "sourceCommit": expected_head,
                   "sourceBranch": expected_branch, "policy": policy, "policySha256": policy_sha256(policy),
                   "suiteIndex": index,
                   "sessionRunId": session_run_id, "invocation": invocation,
                   "environment": guest_environment(repo, environment if environment is not None else test_environment(repo))}
        if whole_session:
            request.update(executionScope=SESSION_SCOPE, commands=[guest_command(cmd, repo) for cmd in command],
                           approvedCommands=command, sessionSelection=session_selection,
                           sourceStartCommit=session_selection['initialCommit'],
                           selectionSha256=source_integrity.digest(session_selection))
            _session_request(request, policy)
        else:
            request.update(command=guest_command(command, repo), approvedCommand=command)
        protected = package_source(repo, root / "input/baseline.zip", expected_head, expected_branch, git)
        manifest = source_integrity.baseline(request, protected)
        manifest_bytes = source_integrity.canonical(manifest)
        if len(manifest_bytes) > source_integrity.LIMIT:
            raise source_integrity.IntegrityError('Host source manifest exceeds evidence size bound')
        # Neither this authority nor request.json is exposed through writable mappings.
        with (root / 'source-baseline.json').open('xb') as stream:
            stream.write(manifest_bytes)
        with (root / 'input/source-baseline.json').open('xb') as stream:
            stream.write(manifest_bytes)
        shutil.copyfile(source_integrity.__file__, root / 'input/source_integrity.py')
        request['sourceIntegrity'] = {'schemaVersion': 1, 'baselineSha256': source_integrity.digest(manifest)}
        source_integrity.check_archive(root / 'input/baseline.zip', manifest)
        request_path = root / "request.json"
        request_bytes = json.dumps(request, indent=2).encode('utf-8')
        if len(request_bytes) > 1024 * 1024:
            raise ValueError('Session request exceeds the bounded JSON evidence size')
        request_path.write_bytes(request_bytes)
        script_directory = SCRIPTS
        if launch is None:
            manager = trusted_sandbox_management.ManagementSession(root, request, repo)
            request['trustedManagement'] = manager.metadata()
            request_bytes = json.dumps(request, indent=2).encode('utf-8')
            if len(request_bytes) > 1024 * 1024:
                raise ValueError('Trusted-management request exceeds the JSON evidence limit')
            request_path.write_bytes(request_bytes)
            manager.bind()
            manager.preflight()
            launch = manager.run
            script_directory = manager.scripts
        if manager is not None:
            argv = manager.runner_argv()
        else:
            # Preserve the existing explicitly injected launcher contract.
            quote = lambda path: "'" + str(path).replace("'", "''") + "'"
            code = (f"& ([scriptblock]::Create([IO.File]::ReadAllText({quote(script_directory / 'run.ps1')}))) "
                    f"-SourceDirectory {quote(script_directory)} -Mode Session -RequestPath {quote(request_path)}")
            argv = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(code.encode("utf-16-le")).decode("ascii")]
        started = True
        process = launch(argv, cwd=str(script_directory), timeout=budget + 1500, shell=False,
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
            evidence = validate_evidence(root, request, require_pass=False)
            code, output, timed, integrity = evidence
            if process.returncode != (0 if summary.get('outcome') == 'PASS' else 1):
                raise ValueError("PowerShell runner failed despite passing guest evidence")
            _identity(repo, git, expected_head, expected_branch)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            # Failed evidence cannot pass even if both native host exits are zero.
            diagnostic = f"Windows Sandbox verification rejected: {exc}; evidence: {root}"
            suites = (summary.get('baseline') or {}).get('suites') or []
            timed = any(isinstance(item, dict) and isinstance(item.get('process'), dict)
                        and item['process'].get('timedOut') is True for item in suites)
            code = 124 if timed else 126 if isinstance(exc, source_integrity.IntegrityError) else 127
            result = _TestResult(code, diagnostic, timed, clean, True)
            integrity = {'schemaVersion': 1, 'verified': False, 'error': str(exc)}
        else:
            result = _TestResult(code, output, timed, clean, True)
            if whole_session:
                result.suite_results = [_TestResult(code, text, timed, clean, True)
                                        for code, text, timed in evidence.suites]
        result.windows_sandbox = root.name
        result.source_integrity = integrity
        for suite in getattr(result, 'suite_results', []):
            suite.windows_sandbox = root.name
            suite.source_integrity = integrity
        with (root / 'integrity-verification.json').open('x', encoding='utf-8') as stream:
            json.dump(integrity, stream, indent=2)
        return result
    except BaseException as exc:
        if started:
            guest_clean = _rescue_cleanup(root, request, launch)
            clean = guest_clean and (host_finished or getattr(exc, 'cleanup_confirmed', None) is True)
        else:
            clean = getattr(exc, 'cleanup_confirmed', None) is not False
        if isinstance(exc, subprocess.TimeoutExpired):
            stage = 'host execution' if started else 'management preflight before VM launch'
            result = _TestResult(124, f"Windows Sandbox {stage} TIMEOUT; evidence: {root}", True, clean, started)
            result.windows_sandbox = root.name if root is not None else None
            return result
        # Never infer Sandbox termination from the host process exit alone.
        raise ControllerTestEnvironmentError(
            f"Windows Sandbox final verification could not complete: {exc}; evidence: {root}",
            cleanup_confirmed=clean, process_started=started,
            details={"stage": "windows_sandbox_final", "evidence": str(root)}) from exc
