"""Deterministic adapter/controller regressions; fixtures are not live Sandbox proof."""
import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from auto_coding import simple_run as runner, windows_sandbox as sandbox, source_integrity as integrity
import test_final_verification as fixtures
from test_final_verification import Executor, POLICY, PASSED


def verified_result(code=0, output=PASSED, timed=False, clean=True, started=True):
    result = runner._CallResult(code, output, timed, clean, started)
    result.source_integrity = {'schemaVersion':1,'baselineSha256':'a' * 64,'verified':True,'hostPackageVerified':True,
        'observations':{stage:{'complete':True,'matchesBaseline':True,'fileCount':1,
            'differences':{'added':[],'deleted':[],'modified':[]}} for stage in ('pre','post')}}
    return result


class ControllerIntegrationTests(unittest.TestCase):
    setUp = fixtures.FinalVerificationTests.setUp
    advance = fixtures.FinalVerificationTests.advance
    git = fixtures.FinalVerificationTests.git
    write_plan = fixtures.FinalVerificationTests.write_plan
    state = fixtures.FinalVerificationTests.state
    events = fixtures.FinalVerificationTests.events
    run_fake = fixtures.FinalVerificationTests.run_fake

    def selected(self):
        return patch.dict(os.environ, {sandbox.BACKEND_ENV: 'windows-sandbox'})

    def test_automatic_final_only_invocation_uses_approved_checkpoint_and_policy(self):
        observed = []
        def verify(command, repo, logs, timeout, **options):
            observed.append(options)
            self.assertEqual(options['expected_head'], self.git('rev-parse', 'HEAD'))
            self.assertNotEqual(options['expected_head'], self.initial)
            self.assertEqual(options['expected_branch'], 'feature/verification')
            self.assertEqual(options['policy'], POLICY)
            self.assertEqual(command, POLICY['test_commands'][0])
            self.assertEqual(timeout, POLICY['timeout_seconds'])
            self.assertEqual(self.state()['history'][0]['review']['decision'], 'APPROVED')
            self.assertEqual(self.state()['completed'], ['one'])
            self.assertEqual(self.git('status', '--porcelain'), '')
            result = verified_result()
            result.windows_sandbox = 'fixture-receipt'
            return result
        fake = Executor()
        with self.selected(), patch.object(sandbox, 'run', side_effect=verify) as verify_call, \
                patch.object(runner.controller_test_sandbox, 'run') as legacy:
            self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ['A', 'B'])
        self.assertEqual(verify_call.call_count, 1)
        legacy.assert_not_called()
        self.assertEqual(self.state()['final_test_backend'], 'windows-sandbox')
        self.assertEqual(self.state()['final_verification_result']['tests'][0]['windows_sandbox'], 'fixture-receipt')

    def test_all_approved_commands_propagate_with_combined_minimum(self):
        self.document['final_verification'] = POLICY | {'test_commands': [['suite-one'], ['suite-two']], 'minimum_tests': 4}
        self.write_plan()
        with self.selected(), patch.object(sandbox, 'run', return_value=verified_result()) as run:
            self.assertEqual(self.run_fake(Executor()), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [['suite-one'], ['suite-two']])
        self.assertEqual([call.kwargs['index'] for call in run.call_args_list], [1, 2])
        self.assertEqual(self.state()['final_verification_result']['test_count'], 4)

    def test_rejected_review_never_launches_final_sandbox(self):
        fake = Executor(reviews=[{'decision': 'REJECTED', 'issues': ['Defect'], 'summary': 'Broken'}] * 10)
        with self.selected(), patch.object(sandbox, 'run') as run:
            self.assertEqual(self.run_fake(fake, attempts=1), 1)
        run.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)

    def test_confirmed_timeout_stops_and_preserves_approved_commit(self):
        with self.selected(), patch.object(sandbox, 'run', return_value=runner._CallResult(124, 'TIMEOUT', True, True, True)):
            self.assertEqual(self.run_fake(Executor()), 1)
        state = self.state()
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['final_verification_result']['outcome'], 'infrastructure_timeout')
        self.assertEqual(state['completed'], ['one'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), state['history'][0]['commit'])

    def test_uncertain_cleanup_keeps_existing_safety_gate(self):
        with self.selected(), patch.object(sandbox, 'run', return_value=runner._CallResult(127, 'uncertain', False, False, True)):
            self.assertEqual(self.run_fake(Executor()), 1)
        self.assertIn('uncertain_test', self.state())
        self.assertTrue((self.repo / '.git/auto-coding-test-process-uncertain.json').exists())

    def test_backend_cannot_switch_on_resume(self):
        with self.selected(), patch.object(sandbox, 'run', return_value=runner._CallResult(127, 'missing tool', False, True, False)):
            self.assertEqual(self.run_fake(Executor()), 1)
        with patch.dict(os.environ, {sandbox.BACKEND_ENV: 'mxc'}), self.assertRaises(runner.PilotError):
            self.run_fake(Executor(), resume=True)

    def test_saved_windows_evidence_is_required_for_reuse(self):
        with self.selected(), patch.object(sandbox, 'run', return_value=verified_result()):
            self.assertEqual(self.run_fake(Executor()), 0)
        with patch.object(sandbox, 'saved_evidence', side_effect=ValueError('Missing logs')):
            self.assertFalse(runner.final_evidence_valid(self.repo, self.logs, self.state()))

    def test_passing_tests_cannot_override_failed_source_integrity(self):
        result = verified_result()
        result.source_integrity['verified'] = False
        with self.selected(), patch.object(sandbox, 'run', return_value=result):
            self.assertEqual(self.run_fake(Executor()), 1)
        final = self.state()['final_verification_result']
        self.assertEqual(final['outcome'], 'source_integrity_failure')
        self.assertTrue(final['tests'][0]['summary']['successful'])

    def test_batch1_saved_evidence_without_integrity_cannot_be_reused(self):
        with self.selected(), patch.object(sandbox, 'run', return_value=verified_result()):
            self.assertEqual(self.run_fake(Executor()), 0)
        state = self.state()
        del state['final_verification_result']['tests'][0]['source_integrity']
        self.assertFalse(runner.final_evidence_valid(self.repo, self.logs, state))

    def test_failed_tests_cannot_be_overridden_by_passing_integrity(self):
        result = verified_result(1, fixtures.FAILED)
        with self.selected(), patch.object(sandbox, 'run', return_value=result):
            self.assertEqual(self.run_fake(Executor()), 1)
        final = self.state()['final_verification_result']
        self.assertEqual(final['outcome'], 'test_failure')
        self.assertTrue(final['tests'][0]['source_integrity']['verified'])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.git(self.repo, ['init', '-b', 'feature/session'])
        self.git(self.repo, ['config', 'user.name', 'Fixture'])
        self.git(self.repo, ['config', 'user.email', 'fixture@example.invalid'])
        (self.repo / 'app.py').write_text('VALUE = 1\n')
        self.commit()
        self.initial = self.git(self.repo, ['rev-parse', 'HEAD'])
        (self.repo / 'app.py').write_text('VALUE = 2\n')
        self.commit()
        self.head = self.git(self.repo, ['rev-parse', 'HEAD'])
        self.policy = {'test_commands': [['python', '-m', 'unittest', 'discover', '-v']], 'minimum_tests': 2, 'timeout_seconds': 30}
        self.request = {'runId': '8dd2dbe0-a7fd-4d57-a580-86a78b4d0cfa', 'sandboxId': '224cc0db-e49c-4a9d-9d35-47b97f3c95f1',
            'sourceCommit': self.head, 'sourceBranch': 'feature/session', 'policy': self.policy,
            'policySha256': sandbox.policy_sha256(self.policy), 'suiteIndex': 1,
            'command': [r'C:\Python313Embed\python.exe', '-m', 'unittest', 'discover', '-v'],
            'approvedCommand': self.policy['test_commands'][0], 'sessionRunId': 'session', 'invocation': 1}

    def git(self, repo, arguments):
        return subprocess.run(['git', *arguments], cwd=repo, check=True, capture_output=True, text=True, timeout=20).stdout.strip()

    def commit(self):
        self.git(self.repo, ['add', '.'])
        self.git(self.repo, ['commit', '-m', 'fixture checkpoint'])

    def fixture(self, request=None, root=None):
        request = request or self.request
        root = root or self.logs
        (root / 'results').mkdir(exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        name = f"session-suite-{request['suiteIndex']}"
        process = {'executable': request['command'][0], 'arguments': request['command'][1:],
            'workingDirectory': sandbox.GUEST, 'startedUtc': now, 'finishedUtc': now, 'exitCode': 0,
            'timedOut': False, 'timeoutSeconds': 30, 'stdout': rf'C:\ACResults\{name}.stdout.log',
            'stderr': rf'C:\ACResults\{name}.stderr.log'}
        suite = {'file': name, 'process': process, 'tests': 2, 'failures': 0, 'errors': 0, 'skips': 0, 'status': 'PASS'}
        result = {key: request[key] for key in ('runId','sandboxId','sourceCommit','sourceBranch','policySha256','suiteIndex')}
        result.update(disposableCommit='d' * 40, computerName='GUEST', account='NT AUTHORITY\\SYSTEM',
            versions={'git':'git version 2.53.0.windows.4','python':'Python 3.13.17','node':'v26.13.1'},
            startedUtc=now, finishedUtc=now, suites=[suite], totalTests=2, success=True, operationExitCode=0, error=None)
        (root / 'input').mkdir(exist_ok=True)
        manifest_path = root / 'source-baseline.json'
        if manifest_path.exists():
            manifest = integrity.load(manifest_path)
        else:
            manifest = integrity.baseline(request, [integrity.file_record('app.py', b'VALUE = 2\n')])
            manifest_path.write_bytes(integrity.canonical(manifest))
        request['sourceIntegrity'] = {'schemaVersion':1,'baselineSha256':integrity.digest(manifest)}
        (root / 'input/source-baseline.json').write_bytes(integrity.canonical(manifest))
        if not (root / 'input/baseline.zip').exists():
            with sandbox.zipfile.ZipFile(root / 'input/baseline.zip', 'w') as package:
                package.writestr('app.py', b'VALUE = 2\n')
        result['sourceIntegrity'] = dict(request['sourceIntegrity'])
        for stage in ('pre','post'):
            observation = {'schemaVersion':1,'kind':'source-observation','stage':stage,'identity':manifest['identity'],
                'baselineSha256':integrity.digest(manifest),'complete':True,'files':manifest['files'],
                'fileCount':manifest['fileCount'],'totalBytes':manifest['totalBytes'],'excluded':[],
                'matchesBaseline':True,'differences':{'added':[],'deleted':[],'modified':[]},
                'startedUtc':now,'finishedUtc':now,'error':None}
            self.write(root / 'results' / f'integrity-{stage}.json', observation)
            self.write(root / 'results' / f'integrity-{stage}-hash.process.json', {'exitCode':0,'timedOut':False,
                'executable':r'C:\Python313Embed\python.exe','arguments':integrity.worker_arguments(stage, integrity.digest(manifest)),
                'workingDirectory':sandbox.GUEST,'timeoutSeconds':120,'startedUtc':now,'finishedUtc':now,
                'stdout':rf'C:\ACResults\integrity-{stage}-hash.stdout.log','stderr':rf'C:\ACResults\integrity-{stage}-hash.stderr.log'})
            for stream in ('stdout','stderr'):
                (root / 'results' / f'integrity-{stage}-hash.{stream}.log').write_text('')
            result['sourceIntegrity'][stage] = {'complete':True,'matchesBaseline':True}
        summary = {'outcome':'PASS','mode':'Session','runId':request['runId'],'sandboxId':request['sandboxId'],
            'requestedSandboxId':request['sandboxId'],'baseline':result,'cleanupVerified':True,'failure':None}
        self.write(root / 'request.json', request)
        self.write(root / 'summary.json', summary)
        self.write(root / 'results/baseline.json', result)
        self.write(root / 'start.output.txt', {'Id':request['sandboxId']})
        self.write(root / 'after.output.txt', {'WindowsSandboxEnvironments':[]})
        self.write(root / 'exec-baseline.output.txt', {'ExitCode':0})
        for label in ('start','exec-baseline','stop','after'):
            self.write(root / (label + '.process.json'), {'exitCode':0,'timedOut':False,'startedUtc':now,'finishedUtc':now})
        self.write(root / 'results' / (name + '.process.json'), process)
        self.write(root / 'results' / (name + '.suite.json'), suite)
        (root / 'results' / (name + '.stdout.log')).write_text('', encoding='utf-8')
        (root / 'results' / (name + '.stderr.log')).write_text(PASSED, encoding='utf-8')
        return summary

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_packages_actual_session_commit_without_live_metadata(self):
        target = self.logs / 'source.zip'
        sandbox.package_source(self.repo, target, self.head, 'feature/session', self.git)
        with sandbox.zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read('app.py'), b'VALUE = 2\n')
            self.assertNotIn('.git/config', archive.namelist())
        self.assertNotEqual(self.head, self.initial)

    def test_stale_head_wrong_branch_and_dirty_worktree_are_rejected(self):
        for head, branch in ((self.initial, 'feature/session'), (self.head, 'wrong')):
            with self.subTest(head=head, branch=branch), self.assertRaises(ValueError):
                sandbox.package_source(self.repo, self.logs / 'source.zip', head, branch, self.git)
        (self.repo / 'extra.txt').write_text('unapproved')
        with self.assertRaises(ValueError):
            sandbox.package_source(self.repo, self.logs / 'source.zip', self.head, 'feature/session', self.git)

    def test_sensitive_committed_files_are_not_packaged(self):
        (self.repo / '.env').write_text('credential fixture')
        self.commit()
        with self.assertRaises(ValueError):
            sandbox.package_source(self.repo, self.logs / 'source.zip', self.git(self.repo,['rev-parse','HEAD']), 'feature/session', self.git)

    def test_hidden_worktree_flags_cannot_bypass_clean_source_gate(self):
        self.git(self.repo, ['update-index','--assume-unchanged','app.py'])
        (self.repo / 'app.py').write_text('VALUE = 99\n')
        self.assertEqual(self.git(self.repo, ['status','--porcelain']), '')
        with self.assertRaisesRegex(ValueError, 'hidden working-tree flags'):
            sandbox.package_source(self.repo, self.logs / 'source.zip', self.head, 'feature/session', self.git)

    def test_newly_added_committed_file_has_authoritative_sha256(self):
        content = b'# newly approved source\n'
        (self.repo / 'new.py').write_bytes(content)
        self.commit()
        head = self.git(self.repo, ['rev-parse','HEAD'])
        protected = sandbox.package_source(self.repo, self.logs / 'source.zip', head, 'feature/session', self.git)
        self.assertIn(integrity.file_record('new.py', content), protected)

    def test_export_attributes_cannot_omit_approved_source(self):
        (self.repo / '.gitattributes').write_text('app.py export-ignore\n')
        self.commit()
        sandbox.package_source(self.repo, self.logs / 'source.zip', self.git(self.repo,['rev-parse','HEAD']), 'feature/session', self.git)
        with sandbox.zipfile.ZipFile(self.logs / 'source.zip') as archive:
            self.assertEqual(archive.read('app.py'), b'VALUE = 2\n')

    def test_command_relocation_preserves_argv_and_rejects_host_paths(self):
        command = ['python', '-B', str(self.repo / 'test_app.py'), '--flag', 'a b']
        self.assertEqual(sandbox.guest_command(command, self.repo), [r'C:\Python313Embed\python.exe','-B',r'C:\AutoCodingTest\test_app.py','--flag','a b'])
        with self.assertRaises(ValueError):
            sandbox.guest_command(['python', str(self.root / 'private.py')], self.repo)

    def test_environment_relocates_imports_without_host_credentials(self):
        env = sandbox.guest_environment(self.repo, {'PYTHONPATH':os.pathsep.join([str(self.repo),str(self.repo / 'lib')]),
            'CI':'true','CODEX_API_KEY':'secret','PATH':'host-path','USERPROFILE':'host-home'})
        self.assertEqual(env['PYTHONPATH'], r'C:\AutoCodingTest;C:\AutoCodingTest\src;C:\AutoCodingTest\lib')
        self.assertEqual(env['CI'], 'true')
        self.assertEqual(env['AUTOCODING_CONFIG_DIR'], r'C:\AutoCodingConfig')
        self.assertFalse({'CODEX_API_KEY','PATH','USERPROFILE'} & set(env))

    def test_valid_suite_and_complete_original_output_are_parsed(self):
        self.fixture()
        self.assertEqual(sandbox.validate_evidence(self.logs, self.request), '\n' + PASSED)

    def test_missing_malformed_failed_skipped_and_incorrect_counts_rejected(self):
        for text in ('OK\n', 'Ran 0 tests in 0.1s\n\nOK\n', 'Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n',
                     'Ran 2 tests in 0.1s\n\nOK (skipped=1)\n', 'Ran 3 tests in 0.1s\n\nOK\n'):
            with self.subTest(output=text):
                self.fixture()
                (self.logs / 'results/session-suite-1.stderr.log').write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    sandbox.validate_evidence(self.logs, self.request)
        self.fixture()
        (self.logs / 'results/session-suite-1.stdout.log').unlink()
        with self.assertRaises(OSError):
            sandbox.validate_evidence(self.logs, self.request)
        self.fixture()
        (self.logs / 'summary.json').write_text('{')
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_guest_nonzero_cannot_be_hidden_by_zero_host_cli_exit(self):
        self.fixture()
        self.write(self.logs / 'exec-baseline.output.txt', {'ExitCode':23})
        with self.assertRaisesRegex(ValueError, 'exit codes disagree'):
            sandbox.validate_evidence(self.logs, self.request)

    def test_wrong_source_policy_run_or_cleanup_is_rejected(self):
        for field in ('sourceCommit','sourceBranch','policySha256','runId','suiteIndex'):
            self.fixture()
            request = copy.deepcopy(self.request)
            request[field] = 'stale'
            with self.subTest(field=field), self.assertRaises(ValueError):
                sandbox.validate_evidence(self.logs, request)
        summary = self.fixture()
        summary['cleanupVerified'] = False
        self.write(self.logs / 'summary.json', summary)
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_owned_instance_still_listed_rejects_cleanup_claim(self):
        self.fixture()
        self.write(self.logs / 'after.output.txt', {'WindowsSandboxEnvironments':[{'Id':self.request['sandboxId']}]})
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_host_timeout_and_process_failure_rejected(self):
        for field, value in (('timedOut',True),('exitCode',127)):
            self.fixture()
            self.write(self.logs / 'exec-baseline.process.json', {'exitCode':0,'timedOut':False} | {field:value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                sandbox.validate_evidence(self.logs, self.request)

    def invoke_adapter(self, launch):
        real_which = sandbox.shutil.which
        with patch.object(sandbox, 'WINDOWS', True), \
                patch.dict(os.environ, {'SYSTEMROOT':os.environ.get('SYSTEMROOT', str(self.root / 'Windows'))}), \
                patch.object(Path, 'is_file', return_value=True), \
                patch.object(sandbox.shutil, 'which', side_effect=lambda name: 'wsb.exe' if name == 'wsb' else real_which(name)):
            return sandbox.run(self.policy['test_commands'][0], self.repo, self.logs, 30,
                git=self.git, expected_head=self.head, expected_branch='feature/session', policy=self.policy,
                index=1, session_run_id='session', invocation=1, launch=launch)

    def test_adapter_invokes_phase3_session_and_returns_real_suite_output(self):
        def launch(argv, **options):
            self.assertEqual(options['timeout'], 1530)
            code = sandbox.base64.b64decode(argv[-1]).decode('utf-16-le')
            self.assertIn('-Mode Session -RequestPath', code)
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            self.assertEqual(request['policy'], self.policy)
            self.assertEqual(request['sourceCommit'], self.head)
            self.fixture(request, root)
            return subprocess.CompletedProcess(argv, 0, 'Host diagnostic', '')
        result = self.invoke_adapter(launch)
        self.assertEqual(result, (0, '\n' + PASSED, False))
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(sandbox.saved_evidence(self.logs, result.windows_sandbox, head=self.head,
            branch='feature/session', policy=self.policy, index=1, run_id='session', invocation=1), result[1])

    def test_nonzero_suite_exit_survives_zero_native_cli_exit(self):
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            summary = self.fixture(request, root)
            result = summary['baseline']
            result.update(operationExitCode=1, success=False, error='Test suite failed')
            suite = result['suites'][0]
            suite.update(status='FAIL', failures=1)
            suite['process']['exitCode'] = 23
            summary.update(outcome='FAIL', failure='Test suite failed')
            self.write(root / 'summary.json', summary)
            self.write(root / 'results/baseline.json', result)
            self.write(root / 'results/session-suite-1.process.json', suite['process'])
            self.write(root / 'results/session-suite-1.suite.json', suite)
            self.write(root / 'exec-baseline.output.txt', {'ExitCode':1})
            (root / 'results/session-suite-1.stderr.log').write_text('Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n')
            return subprocess.CompletedProcess(argv, 1, '', '')
        result = self.invoke_adapter(launch)
        self.assertEqual(result[0], 23)
        self.assertIn('FAILED', result[1])
        self.assertTrue(result.cleanup_confirmed)

    def test_killed_host_runner_attempts_only_owned_id_cleanup(self):
        owned = []
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            if '-EncodedCommand' in argv:
                owned.append(request['sandboxId'])
                self.write(root / 'launch-attempt.json', {key:request[key] for key in ('runId','sandboxId')})
                error = subprocess.TimeoutExpired(argv, options['timeout'])
                error.cleanup_confirmed = True  # The bounded host Job was terminated.
                raise error
            if 'stop' in argv:
                self.assertEqual(argv, ['wsb.exe','stop','--id',owned[0],'--raw'])
                return subprocess.CompletedProcess(argv, 0, '', '')
            self.assertEqual(argv, ['wsb.exe','list','--raw'])
            return subprocess.CompletedProcess(argv, 0, '{"WindowsSandboxEnvironments":[]}', '')
        result = self.invoke_adapter(launch)
        self.assertEqual(result[0], 124)
        self.assertTrue(result[2])
        self.assertTrue(result.cleanup_confirmed)

    def test_unexpected_host_failure_cannot_claim_cleanup_from_host_exit(self):
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            if '-EncodedCommand' in argv:
                self.write(root / 'launch-attempt.json', {key:request[key] for key in ('runId','sandboxId')})
            raise OSError('Runner/list failed')
        with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
            self.invoke_adapter(launch)
        self.assertFalse(caught.exception.cleanup_confirmed)

    def test_packaging_host_process_uncertainty_is_not_lost(self):
        error = OSError('Git process cleanup unknown')
        error.cleanup_confirmed = False
        with patch.object(sandbox, 'package_source', side_effect=error), \
                self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
            self.invoke_adapter(lambda *args, **options: self.fail('Guest must not start'))
        self.assertFalse(caught.exception.cleanup_confirmed)

    def test_missing_pre_or_post_integrity_evidence_rejects_passing_tests(self):
        for stage in ('pre','post'):
            self.fixture()
            (self.logs / 'results' / f'integrity-{stage}.json').unlink()
            with self.subTest(stage=stage), self.assertRaises(integrity.IntegrityError):
                sandbox.validate_evidence(self.logs, self.request)

    def test_malformed_and_stale_integrity_records_cannot_pass(self):
        for change in ('hash','duplicate','truncate','run','unsafe','complete','time'):
            self.fixture()
            path = self.logs / 'results/integrity-post.json'
            value = json.loads(path.read_text())
            if change == 'hash': value['files'][0]['sha256'] = 'invalid'
            elif change == 'duplicate': value['files'].append(value['files'][0])
            elif change == 'truncate': value['files'].clear()
            elif change == 'run': value['identity']['runId'] = 'older-run'
            elif change == 'unsafe': value['files'][0]['path'] = '../app.py'
            elif change == 'complete': value['complete'] = False
            elif change == 'time': value['startedUtc'] = '2000-01-01T00:00:00Z'
            self.write(path, value)
            with self.subTest(change=change), self.assertRaises(integrity.IntegrityError):
                sandbox.validate_evidence(self.logs, self.request)

    def test_actual_passed_suite_with_post_mutation_returns_failed_integrity(self):
        summary = self.fixture()
        path = self.logs / 'results/integrity-post.json'
        value = json.loads(path.read_text())
        value['files'][0]['sha256'] = '0' * 64
        value['matchesBaseline'] = False
        value['differences']['modified'] = ['app.py']
        self.write(path, value)
        worker = self.logs / 'results/integrity-post-hash.process.json'
        process = json.loads(worker.read_text())
        process['exitCode'] = 1
        self.write(worker, process)
        result = summary['baseline']
        result.update(success=False, operationExitCode=1, error='Post integrity mismatch')
        result['sourceIntegrity']['post']['matchesBaseline'] = False
        summary.update(outcome='FAIL', failure='Post integrity mismatch')
        self.write(self.logs / 'summary.json', summary)
        self.write(self.logs / 'results/baseline.json', result)
        self.write(self.logs / 'exec-baseline.output.txt', {'ExitCode':1})
        # A guest-selected expected manifest is never consulted.
        self.write(self.logs / 'results/source-baseline.json', {'files':value['files']})
        code, output, timed, verdict = sandbox.validate_evidence(self.logs, self.request, require_pass=False)
        self.assertEqual(code, 0)
        self.assertIn('OK', output)
        self.assertFalse(verdict['verified'])
        with self.assertRaises(ValueError):
            sandbox.validate_evidence(self.logs, self.request)

    def test_legacy_receipt_and_missing_authoritative_baseline_rejected(self):
        self.fixture()
        legacy = copy.deepcopy(self.request)
        del legacy['sourceIntegrity']
        with self.assertRaises(integrity.IntegrityError):
            sandbox.validate_evidence(self.logs, legacy)
        (self.logs / 'source-baseline.json').unlink()
        with self.assertRaises(integrity.IntegrityError):
            sandbox.validate_evidence(self.logs, self.request)


if __name__ == '__main__':
    unittest.main()
