"""Batch 3 deterministic qualification; these doubles are not real Sandbox proof."""
import copy
import json
import subprocess
import unittest
import os
import sys
import base64
import tempfile
import time
import datetime as dt
from pathlib import Path
from unittest.mock import patch

from auto_coding import windows_sandbox as sandbox, source_integrity as integrity
from auto_coding.process_runner import bounded_run
import test_windows_sandbox as fixtures


class EvidenceAcceptanceTests(unittest.TestCase):
    setUp = fixtures.AdapterTests.setUp
    git = fixtures.AdapterTests.git
    commit = fixtures.AdapterTests.commit
    fixture = fixtures.AdapterTests.fixture
    write = fixtures.AdapterTests.write
    invoke_adapter = fixtures.AdapterTests.invoke_adapter

    def test_success_cannot_contain_guest_or_host_error(self):
        for target in ('guest','host'):
            summary = self.fixture()
            if target == 'guest': summary['baseline']['error'] = 'Unexpected process failure'
            else: summary['failure'] = 'Shutdown reported a failure'
            self.write(self.logs / 'summary.json', summary)
            self.write(self.logs / 'results/baseline.json', summary['baseline'])
            with self.subTest(target=target), self.assertRaises(ValueError):
                sandbox.validate_evidence(self.logs, self.request, require_pass=False)

    def test_cache_command_must_implement_the_approved_policy(self):
        root = self.logs / ('windows-sandbox-' + '1' * 32)
        root.mkdir()
        request = copy.deepcopy(self.request)
        request['command'] = [r'C:\Python313Embed\python.exe','-c','print("different command")']
        summary = self.fixture(request, root)
        verdict = integrity.verify_receipt(root, request, summary['baseline'], sandbox._json)
        self.write(root / 'integrity-verification.json', verdict)
        with self.assertRaises(ValueError):
            sandbox.saved_evidence(self.logs, root.name, head=self.head, branch='feature/session',
                policy=self.policy, index=1, run_id='session', invocation=1)

    def test_boolean_timeout_metadata_is_not_a_deadline(self):
        summary = self.fixture()
        summary['baseline']['suites'][0]['process']['timeoutSeconds'] = True
        suite = summary['baseline']['suites'][0]
        self.write(self.logs / 'summary.json', summary)
        self.write(self.logs / 'results/baseline.json', summary['baseline'])
        self.write(self.logs / 'results/session-suite-1.process.json', suite['process'])
        self.write(self.logs / 'results/session-suite-1.suite.json', suite)
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_cleanup_command_cannot_target_a_foreign_id(self):
        self.fixture()
        path = self.logs / 'stop.process.json'
        process = sandbox._json(path)
        process['arguments'] = ['stop','--id','ffffffff-ffff-ffff-ffff-ffffffffffff','--raw']
        self.write(path, process)
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_cross_provider_clock_rounding_is_bounded(self):
        for milliseconds, accepted in ((1,True),(50,False)):
            self.fixture()
            path = self.logs / 'results/integrity-pre.json'
            value = integrity.load(path)
            value['finishedUtc'] = (integrity.timestamp(value['finishedUtc']) + dt.timedelta(milliseconds=milliseconds)).isoformat()
            self.write(path, value)
            with self.subTest(milliseconds=milliseconds):
                if accepted:
                    self.assertIn('OK', sandbox.validate_evidence(self.logs, self.request))
                else:
                    with self.assertRaises(integrity.IntegrityError):
                        sandbox.validate_evidence(self.logs, self.request)

    def test_missing_suite_and_inconsistent_counts_are_rejected(self):
        for change in ('missing','count','duplicate'):
            summary = self.fixture()
            suites = summary['baseline']['suites']
            if change == 'missing': suites.clear()
            elif change == 'duplicate': suites.append(copy.deepcopy(suites[0]))
            else: suites[0]['tests'] += 1
            self.write(self.logs / 'summary.json', summary)
            self.write(self.logs / 'results/baseline.json', summary['baseline'])
            with self.subTest(change=change), self.assertRaises(ValueError):
                sandbox.validate_evidence(self.logs, self.request)

    def test_copied_receipt_cannot_cross_session_or_invocation(self):
        root = self.logs / ('windows-sandbox-' + '1' * 32)
        root.mkdir()
        summary = self.fixture(root=root)
        self.write(root / 'integrity-verification.json',
            integrity.verify_receipt(root, self.request, summary['baseline'], sandbox._json))
        for run_id, invocation in (('other-session',1),('session',2)):
            with self.subTest(run_id=run_id, invocation=invocation), self.assertRaises(ValueError):
                sandbox.saved_evidence(self.logs, root.name, head=self.head, branch='feature/session',
                    policy=self.policy, index=1, run_id=run_id, invocation=invocation)

    def test_startup_timeout_still_attempts_owned_cleanup_and_list(self):
        calls = []
        def launch(argv, **options):
            calls.append(argv)
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            if '-EncodedCommand' in argv:
                self.write(root / 'launch-attempt.json', {key:request[key] for key in ('runId','sandboxId')})
                error = subprocess.TimeoutExpired(argv, options['timeout'])
                error.cleanup_confirmed = True
                raise error
            if 'stop' in argv:
                self.assertEqual(argv[argv.index('--id') + 1], request['sandboxId'])
                error = subprocess.TimeoutExpired(argv, options['timeout'])
                error.cleanup_confirmed = True
                raise error
            return subprocess.CompletedProcess(argv, 0, '{"WindowsSandboxEnvironments":[]}', '')
        result = self.invoke_adapter(launch)
        self.assertEqual(result[0], 124)
        self.assertTrue(result[2])
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(calls[-1], ['wsb.exe','list','--raw'])

    def test_foreign_instances_remain_untouched_during_rescue(self):
        stopped = []
        foreign = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            if '-EncodedCommand' in argv:
                self.write(root / 'launch-attempt.json', {key:request[key] for key in ('runId','sandboxId')})
                error = subprocess.TimeoutExpired(argv, options['timeout'])
                error.cleanup_confirmed = True
                raise error
            if 'stop' in argv:
                stopped.append(argv[argv.index('--id') + 1])
                return subprocess.CompletedProcess(argv, 0, '', '')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'WindowsSandboxEnvironments':[{'Id':foreign}]}), '')
        result = self.invoke_adapter(launch)
        self.assertEqual(result[0], 124)
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(len(stopped), 1)
        self.assertNotIn(foreign, stopped)


class NativeDeadlineTests(unittest.TestCase):
    def test_native_process_deadline_is_enforced(self):
        if sys.platform != 'win32':
            # Keep this new test compatible with the complete CI skip policy.
            # Exercise the native outer deadline where PowerShell is unavailable.
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                bounded_run([sys.executable, '-c', 'import time; time.sleep(10)'],
                            timeout=1, capture_output=True, shell=False)
            # Unix process-group disappearance does not prove descendant cleanup.
            self.assertFalse(raised.exception.cleanup_confirmed)
            return
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            common = sandbox.SCRIPTS / 'common.ps1'
            powershell = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            sleep = base64.b64encode('Start-Sleep -Seconds 10'.encode('utf-16-le')).decode()
            code = (f". ([scriptblock]::Create([IO.File]::ReadAllText({quote(common)}))); "
                f"Invoke-BoundedProcess -Executable {quote(powershell)} "
                f"-Arguments @('-NoProfile','-NonInteractive','-EncodedCommand','{sleep}') "
                f"-WorkingDirectory {quote(root)} -LogPrefix {quote(root / 'deadline')} -TimeoutSeconds 1 | ConvertTo-Json -Depth 5")
            started = time.monotonic()
            result = bounded_run([str(powershell),'-NoProfile','-NonInteractive','-EncodedCommand',
                base64.b64encode(code.encode('utf-16-le')).decode()], cwd=str(root), timeout=20,
                capture_output=True, shell=False)
            record = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(record['timedOut'])
            self.assertEqual(record['timeoutSeconds'], 1)
            self.assertLess(time.monotonic() - started, 20)
            self.assertTrue((root / 'deadline.process.json').is_file())


if __name__ == '__main__':
    unittest.main()
