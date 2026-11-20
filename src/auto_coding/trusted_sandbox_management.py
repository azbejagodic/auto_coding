"""Sealed host-management commands, separate from untrusted-process containment.

No API accepts an arbitrary host script, executable, working directory or command.
Guest policy argv remains data consumed by the existing runner inside its VM.
"""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from auto_coding import source_integrity
from auto_coding.process_runner import bounded_run, MAX_CAPTURE_BYTES, OutputLimitExceeded


CONTROLLER = Path(__file__).resolve().parents[2]
# Reviewed controller-code hashes, loaded before any task executes. Newline/BOM
# normalization makes the same reviewed script portable across Git checkouts.
PINS = {'run.ps1': '24ccf9ad5176f637155c996c8c08402e041ab3767830ce308024436b4205c3d7', 'common.ps1': 'beffb69b2241afc288cd064d120110605007586925ac13f537ae31391a7b1833', 'baseline.ps1': 'cf5b794b9bbddc8a144256bfcfe705f451fc278def81379236d5f3823d4939e3', 'guest.ps1': '881077a14e5c64bcf2283d839a1826b4814351905d0a649486f5eb60497667ed', 'broker.ps1': '01a100afa30d0252f9fa75983a72cd4cb09544d6d0e51305622a3b2ebec49fa7', 'source_integrity.py': '1aa66cb11fa471829bbc9706ba41fc80e5e02dff870dc3ff2d9a8096d32dd8ae'}


def script_bytes(path):
    return source_integrity.read(path).decode('utf-8-sig').replace('\r\n', '\n').encode('utf-8')


def system_root():
    if os.name != 'nt': return Path(os.environ['SYSTEMROOT'])  # portable transport fixtures
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetWindowsDirectoryW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]; query.restype = ctypes.c_uint
    path = ctypes.create_unicode_buffer(32768)
    size = query(path, len(path))
    if not 0 < size < len(path): raise OSError('Cannot identify the protected Windows directory')
    return Path(path.value)


def _inventory(output):
    value = json.loads(output, object_pairs_hook=source_integrity._pairs)
    entries = value.get('WindowsSandboxEnvironments')
    if not isinstance(entries, list): raise ValueError('Incomplete management inventory')
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get('Id'), str): raise ValueError('Malformed Sandbox identity')
        ident = str(uuid.UUID(item['Id']))
        if ident != item['Id'].lower() or ident in seen: raise ValueError('Ambiguous Sandbox identity')
        seen.add(ident)
    return value


def authenticated_broker(value, package):
    if (not isinstance(value, dict) or value.get('jobMembershipVerified') is not True
            or type(value.get('pid')) is not int or value['pid'] < 1
            or type(value.get('createdFileTime')) is not int or value['createdFileTime'] < 1
            or value.get('packageFullName') != package['packageFullName']
            or str(Path(value.get('image', '')).resolve()).casefold() != str(Path(package['image']).resolve()).casefold()):
        return False
    return hashlib.sha256(source_integrity.read(Path(value['image']))).hexdigest() == package['brokerSha256']


def management_creation_flags(in_job, ancestor_flags):
    flags = 0x00000004 | 0x08000000  # suspended, hidden
    if in_job:
        if not ancestor_flags & 0x00000800:
            raise ValueError('Caller Job does not authorize sealed management separation')
        flags |= 0x01000000  # sealed root only; immediately assigned before resume
    return flags


class ManagementSession:
    def __init__(self, root, request, repo):
        if sys.platform != 'win32': raise ValueError('Trusted management requires Windows')
        self.root, self.repo = Path(root).resolve(), Path(repo).resolve()
        if (not re.fullmatch(r'windows-sandbox-[0-9a-f]{32}', self.root.name)
                or self.root == self.repo or self.root.is_relative_to(self.repo)):
            raise ValueError('Management evidence must be controller-owned outside the target')
        self.request = request
        for key in ('runId', 'sandboxId'):
            if str(uuid.UUID(request[key])) != request[key]: raise ValueError('Invalid management identity')
        count = len(request['policy']['test_commands']) if request.get('executionScope') == 'approved-session-v2' else 1
        self.timeout = request['policy']['timeout_seconds'] * count + 1500
        self.scripts = self.root / 'host'
        self.scripts.mkdir()
        self.snapshot = {}
        for name, expected in PINS.items():
            path = CONTROLLER / ('src/auto_coding/source_integrity.py' if name == 'source_integrity.py'
                                 else 'tools/windows_sandbox_poc/' + name)
            data = script_bytes(path)
            if hashlib.sha256(data).hexdigest() != expected: raise ValueError('Unreviewed management script: ' + name)
            (self.scripts / name).write_bytes(data)
            self.snapshot[name] = expected
        system = system_root()
        self.powershell = system / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.environment = {key: value for key, value in os.environ.items() if key.upper() in {
            'SYSTEMROOT','WINDIR','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA','USERPROFILE',
            'LOCALAPPDATA','APPDATA','TEMP','TMP'}}
        self.environment.update(PATH=os.pathsep.join(str(system / path) for path in ('System32','.','System32/WindowsPowerShell/v1.0')),
                                PSModulePath=str(system / 'System32/WindowsPowerShell/v1.0/Modules'),
                                SYSTEMROOT=str(system), WINDIR=str(system))
        command = self._powershell('broker.ps1', ' -ValidateOnly')
        package = bounded_run(command, cwd=str(self.scripts), env=self.environment,
                              capture_output=True, shell=False, timeout=45)
        (self.root / 'management-package.stdout.log').write_text(package.stdout, encoding='utf-8')
        (self.root / 'management-package.stderr.log').write_text(package.stderr, encoding='utf-8')
        if package.returncode != 0: raise ValueError('Trusted package validation failed: ' + package.stderr[-2000:])
        self.package = json.loads(package.stdout, object_pairs_hook=source_integrity._pairs)
        if (self.package.get('signatureVerified') is not True or self.package.get('packagePayloadVerified') is not True
                or self.package.get('activationAttempted') is not False or self.package.get('managementHostVerified') is not True):
            raise ValueError('Missing signed management-package evidence')
        self.cli = self.package['cli']
        self.environment['PATH'] = str(Path(self.cli).parent) + os.pathsep + self.environment['PATH']
        self.request_digest = None
        self.runner_started = False
        self.sequence = 0

    def _powershell(self, name, suffix):
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        code = f"& ([scriptblock]::Create([IO.File]::ReadAllText({quote(self.scripts / name)})))" + suffix
        return [str(self.powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                base64.b64encode(code.encode('utf-16-le')).decode('ascii')]

    def runner_argv(self):
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        return self._powershell('run.ps1', f' -SourceDirectory {quote(self.scripts)} -Mode Session -RequestPath {quote(self.root / "request.json")}')

    def bind(self):
        self.request_digest = hashlib.sha256(source_integrity.read(self.root / 'request.json')).hexdigest()
        source = source_integrity.read(self.root / 'input/source_integrity.py').decode('utf-8-sig').replace('\r\n','\n').encode()
        if hashlib.sha256(source).hexdigest() != PINS['source_integrity.py']:
            raise ValueError('Unreviewed guest integrity worker')

    def metadata(self):
        return {'schemaVersion': 1, 'cli': self.cli, 'package': self.package,
                'scripts': self.snapshot, 'lifetimePolicy': 'authenticated-package-broker-only'}

    def _check(self):
        for name, expected in self.snapshot.items():
            if hashlib.sha256(script_bytes(self.scripts / name)).hexdigest() != expected:
                raise ValueError('Management snapshot changed')
        if self.request_digest is not None and hashlib.sha256(source_integrity.read(self.root / 'request.json')).hexdigest() != self.request_digest:
            raise ValueError('Management request changed')

    def preflight(self):
        self._check()
        result = self._execute('preflight', [self.cli, 'list', '--raw'], 30)
        if result.returncode != 0: raise ValueError('Management preflight CLI failed')
        inventory = _inventory(result.stdout)
        if any(item['Id'].lower() == self.request['sandboxId'] for item in inventory['WindowsSandboxEnvironments']):
            raise ValueError('Requested Sandbox ID already exists')
        return inventory

    def run(self, argv, *, cwd, timeout, shell=False, capture_output=True, text=True, encoding='utf-8'):
        self._check()
        if (shell or not capture_output or not text or encoding != 'utf-8' or Path(cwd) != self.scripts
                or self.request_digest is None): raise ValueError('Unapproved management execution options')
        if argv == self.runner_argv() and timeout == self.timeout and not self.runner_started:
            return self._execute('runner', argv, timeout)
        marker = source_integrity.load(self.root / 'launch-attempt.json')
        if marker != {'runId':self.request['runId'],'sandboxId':self.request['sandboxId']}:
            raise ValueError('Rescue command has no owned launch marker')
        if argv == [self.cli,'stop','--id',self.request['sandboxId'],'--raw'] and timeout == 120:
            return self._execute('rescue-stop', argv, timeout)
        if argv == [self.cli,'list','--raw'] and timeout == 30:
            result = self._execute('rescue-list', argv, timeout)
            _inventory(result.stdout)
            return result
        raise ValueError('Command is outside the sealed Sandbox-management protocol')

    def _authorize_execution(self, label, argv, timeout):
        self._check()
        if type(label) is not str or type(argv) is not list or any(type(argument) is not str for argument in argv) or type(timeout) is not int:
            raise ValueError('Unapproved management command or deadline')
        if label == 'preflight' and argv == [self.cli, 'list', '--raw'] and timeout == 30:
            return
        if self.request_digest is None:
            raise ValueError('Management execution requires a bound request')
        if label == 'runner':
            if argv == self.runner_argv() and timeout == self.timeout and not self.runner_started:
                return
            raise ValueError('Unapproved or repeated management runner')
        if label not in {'rescue-stop', 'rescue-list'}:
            raise ValueError('Unknown management operation')
        marker = source_integrity.load(self.root / 'launch-attempt.json')
        if marker != {'runId': self.request['runId'], 'sandboxId': self.request['sandboxId']}:
            raise ValueError('Rescue command has no owned launch marker')
        if label == 'rescue-stop' and argv == [self.cli, 'stop', '--id', self.request['sandboxId'], '--raw'] and timeout == 120:
            return
        if label == 'rescue-list' and argv == [self.cli, 'list', '--raw'] and timeout == 30:
            return
        raise ValueError('Command is outside the sealed Sandbox-management protocol')

    def _execute(self, label, argv, timeout):
        self._authorize_execution(label, argv, timeout)
        argv = list(argv)  # Own the validated arguments; callers cannot mutate them during launch.
        from auto_coding.windows_management_job import ManagementJob
        self.sequence += 1
        prefix = self.root / f'management-{self.sequence:02d}-{label}'
        job = ManagementJob(self.package)
        process = None; launch_attempted = False; readers = []; buffers = [bytearray(), bytearray()]
        excessive = threading.Event(); errors = []; lock = threading.Lock(); started = time.monotonic()
        record = {'argv':argv, 'timeoutSeconds':timeout, 'descendantTracking':True,
                  'breakawayAllowed':False, 'persistentPolicy':'authenticated-package-broker-only'}
        def drain(stream, buffer):
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk: break
                    with lock:
                        if sum(map(len,buffers)) + len(chunk) > MAX_CAPTURE_BYTES:
                            excessive.set(); break
                        buffer.extend(chunk)
            except BaseException as exc: errors.append(str(exc))
            finally: stream.close()
        try:
            flags = job.creation_flags()
            self._authorize_execution(label, argv, timeout)
            if label == 'runner':
                self.runner_started = True
            launch_attempted = True
            process = subprocess.Popen(argv, cwd=str(self.scripts), env=self.environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                creationflags=flags)
            job.assign_and_resume(process)
            for stream, buffer in zip((process.stdout,process.stderr),buffers):
                reader = threading.Thread(target=drain,args=(stream,buffer),daemon=True);reader.start();readers.append(reader)
            while True:
                if excessive.is_set(): raise OutputLimitExceeded('Management output exceeded its complete-capture limit')
                if errors: raise OSError('Management output could not be captured')
                if process.poll() is not None and all(not reader.is_alive() for reader in readers):
                    survivors = job.survivors(process.pid)
                    if survivors is not None:
                        record.update(completed=True,rootExitCode=process.returncode,completeOutput=True,
                                      authenticatedBrokerSurvivors=survivors,cleanupConfirmed=True)
                        return subprocess.CompletedProcess(argv,process.returncode,
                            buffers[0].decode('utf-8',errors='strict'),buffers[1].decode('utf-8',errors='strict'))
                    raise OSError('Unfinished or unauthenticated management descendant')
                if time.monotonic()-started >= timeout: raise subprocess.TimeoutExpired(argv,timeout)
                time.sleep(.01)
        except BaseException as exc:
            clean = not launch_attempted or isinstance(exc, FileNotFoundError)
            if process is not None:
                try: clean = job.cancel_transients(process)
                except (OSError,ValueError,subprocess.TimeoutExpired): clean = False
                for reader in readers: reader.join(timeout=1)
                clean = clean and all(not reader.is_alive() for reader in readers)
            record.update(completed=False,completeOutput=False,cleanupConfirmed=clean,error=str(exc))
            exc.cleanup_confirmed=clean
            exc.process_started=True if process is not None else False if clean else None
            exc.output_incomplete=True
            raise
        finally:
            record['seconds']=time.monotonic()-started
            record['ancestorJobFlags']=getattr(job,'ancestor_flags',None)
            record['sealedRootSeparatedFromAncestor']=bool(getattr(job,'ancestor_flags',0) & 0x00000800)
            record['observedProcesses']=list(job.processes.values())
            for suffix,buffer in zip(('stdout','stderr'),buffers):
                Path(str(prefix)+'.'+suffix+'.log').write_bytes(bytes(buffer))
            Path(str(prefix)+'.process.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
            job.close()  # Does not kill shared brokers or alter foreign VMs.
