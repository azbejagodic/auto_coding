"""Session selection/grouped runner proofs with explicitly mocked VM transport.

No test here launches Windows Sandbox or runs repository CI. PowerShell checks
exercise only the shared command-list helper on the host.
"""
import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from auto_coding import session_testing, source_integrity, windows_sandbox as sandbox, simple_run as runner
from auto_coding.final_verification import verification_policy, policy_sha256
from auto_coding.process_runner import bounded_run
import test_windows_sandbox as fixtures
import test_final_verification as final_fixtures


class CoverageTests(unittest.TestCase):
    setUp = fixtures.AdapterTests.setUp
    git = fixtures.AdapterTests.git
    commit = fixtures.AdapterTests.commit

    def make_policy(self, commands=None):
        commands = commands or [['python', '-m', 'unittest', 'test_feature']]
        return fixtures.scoped_policy({'test_commands': commands, 'minimum_tests': 2 * len(commands), 'timeout_seconds': 30})

    def state(self):
        return {'initial_git_head': self.initial, 'branch': 'feature/session', 'completed': ['one'],
                'run_id': 'session', 'history': [{'task': 'one', 'base_head': self.initial, 'commit': self.head,
                'branch': 'feature/session', 'review': {'decision': 'APPROVED', 'issues': []}}]}

    def checkpoint(self, state, task):
        before = state['history'][-1]['commit']
        self.commit()
        self.head = self.git(self.repo, ['rev-parse', 'HEAD'])
        state['completed'].append(task)
        state['history'].append({'task': task, 'base_head': before, 'commit': self.head,
            'branch': 'feature/session', 'review': {'decision': 'APPROVED', 'issues': []}})

    def select(self, policy=None, state=None):
        policy = policy or self.make_policy()
        return session_testing.select_session(self.repo, state or self.state(), self.head, policy, policy_sha256(policy), self.git)

    def test_whole_session_changes_and_declared_dependencies_are_combined(self):
        state = self.state()
        (self.repo / 'dependency.py').write_text('VALUE = 3\n')
        self.checkpoint(state, 'two')
        policy = self.make_policy([['feature-suite'], ['dependent-suite']])
        policy['session_coverage']['rules'] = [
            {'id': 'feature', 'paths': ['app.py'], 'suite_indices': [1, 2], 'reason': 'Feature also affects its caller'},
            {'id': 'caller', 'paths': ['dependency.py'], 'suite_indices': [2], 'reason': 'Caller regression'}]
        selected = self.select(verification_policy(policy), state)
        self.assertEqual(selected['initialCommit'], self.initial)
        self.assertEqual(selected['finalCommit'], self.head)
        self.assertEqual(len(selected['checkpoints']), 2)
        self.assertEqual({item['path']: item['suite_indices'] for item in selected['coverage']},
                         {'app.py': [1, 2], 'dependency.py': [2]})

    def test_intermediate_changes_are_not_lost_when_later_reverted(self):
        state = self.state()
        (self.repo / 'app.py').write_text('VALUE = 1\n')
        self.checkpoint(state, 'two')
        selected = self.select(state=state)
        self.assertEqual(selected['netChanges'], [])
        self.assertEqual([entry['path'] for entry in selected['coverage']], ['app.py'])
        self.assertTrue(all(entry['changes'] for entry in selected['checkpoints']))

    def test_added_deleted_and_both_rename_paths_are_inventoried(self):
        state = self.state()
        (self.repo / 'new.py').write_text('NEW = True\n')
        self.checkpoint(state, 'two')
        (self.repo / 'new.py').unlink()
        (self.repo / 'app.py').rename(self.repo / 'renamed.py')
        self.checkpoint(state, 'three')
        selected = self.select(state=state)
        self.assertEqual([entry['path'] for entry in selected['coverage']], ['app.py', 'new.py', 'renamed.py'])
        self.assertTrue(any(change['status'].startswith('R') for change in selected['checkpoints'][-1]['changes']))

    def test_new_test_file_without_approved_mapping_fails_closed(self):
        state = self.state()
        (self.repo / 'test_new.py').write_text('# added session regression\n')
        self.checkpoint(state, 'two')
        policy = self.make_policy()
        policy['session_coverage']['rules'][0]['paths'] = ['app.py']
        with self.assertRaisesRegex(ValueError, 'test_new.py'):
            self.select(policy, state)

    def test_security_and_other_mandatory_suites_are_never_filtered_out(self):
        policy = self.make_policy([['feature-suite'], ['security-suite']])
        policy['session_coverage']['rules'][0]['suite_indices'] = [1]
        policy['session_coverage']['mandatory_security_suites'] = [2]
        selected = self.select(policy)
        self.assertEqual(selected['coverage'][0]['suite_indices'], [1])
        self.assertEqual(selected['selectedSuiteIndices'], [1, 2])
        self.assertEqual(selected['mandatorySecuritySuites'], [2])
        reduced = copy.deepcopy(policy)
        reduced['test_commands'].pop()
        reduced['session_coverage']['minimum_tests_per_suite'] = [4]
        with self.assertRaises(ValueError):
            verification_policy(reduced)

    def test_missing_map_does_not_become_a_full_ci_fallback(self):
        with self.assertRaisesRegex(ValueError, 'explicitly approved session_coverage'):
            self.select(self.policy_without_map())
        selected = self.select()
        self.assertEqual(selected['selectedSuiteIndices'], [1])
        self.assertNotIn('ci_unittest.py', json.dumps(self.make_policy()))

    def policy_without_map(self):
        return {key: value for key, value in self.make_policy().items() if key != 'session_coverage'}

    def test_explicit_mandatory_full_ci_policy_is_preserved_without_executing_it(self):
        policy = self.make_policy([['python', 'tests/ci_unittest.py']])
        before = copy.deepcopy(policy)
        self.assertEqual(self.select(policy)['selectedSuiteIndices'], [1])
        self.assertEqual(policy, before)

    def test_malformed_rules_counts_and_git_inventory_are_rejected(self):
        for mutation in ('boolean', 'missing-count', 'unsafe-path', 'nonstring-path', 'duplicate'):
            policy = self.make_policy()
            coverage = policy['session_coverage']
            if mutation == 'boolean': coverage['mandatory_security_suites'] = [True]
            elif mutation == 'missing-count': coverage['minimum_tests_per_suite'] = []
            elif mutation == 'unsafe-path': coverage['rules'][0]['paths'] = ['../outside']
            elif mutation == 'nonstring-path': coverage['rules'][0]['paths'] = [{}]
            else: coverage['rules'].append(copy.deepcopy(coverage['rules'][0]))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                verification_policy(policy)
        for raw in ('M\0app.py', 'R100\0app.py\0', 'U\0app.py\0'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                session_testing.changed_records(raw)


class SessionControllerTests(unittest.TestCase):
    setUp = fixtures.ControllerIntegrationTests.setUp
    git = fixtures.ControllerIntegrationTests.git
    advance = fixtures.ControllerIntegrationTests.advance
    write_plan = fixtures.ControllerIntegrationTests.write_plan
    state = fixtures.ControllerIntegrationTests.state
    events = fixtures.ControllerIntegrationTests.events
    run_fake = fixtures.ControllerIntegrationTests.run_fake
    selected = fixtures.ControllerIntegrationTests.selected

    def combined(self, count):
        result = fixtures.verified_result()
        result.windows_sandbox = 'windows-sandbox-' + '1' * 32
        result.suite_results = [fixtures.verified_result() for _ in range(count)]
        return result

    def test_multiple_tasks_one_final_stage_all_selected_suites_and_no_early_launch(self):
        self.document['tasks'] = [self.document['tasks'][0],
            {'id': 'two', 'prompt': 'Implement the dependent second step', 'test_commands': [['fake-focused']]}]
        self.document['final_verification'] = fixtures.scoped_policy(final_fixtures.POLICY |
            {'test_commands': [['feature-suite'], ['dependent-suite'], ['security-suite']], 'minimum_tests': 6})
        self.document['final_verification']['session_coverage']['mandatory_security_suites'] = [3]
        self.write_plan()
        def hook(role):
            run.assert_not_called()
            if role == 'A':
                name = 'first.py' if fake.calls.count('A') == 1 else 'second.py'
                (self.repo / name).write_text('# controlled session change\n')
        fake = final_fixtures.Executor(reviews=[final_fixtures.CODE_REVIEW_APPROVED] * 2, hook=hook)
        def verify(commands, repo, logs, timeout, **options):
            self.assertEqual(fake.calls, ['A', 'B', 'A', 'B'])
            self.assertEqual(self.state()['completed'], ['one', 'two'])
            selection = options['selection']
            self.assertEqual(selection['initialCommit'], self.initial)
            self.assertEqual(selection['finalCommit'], self.git('rev-parse', 'HEAD'))
            self.assertEqual({entry['path'] for entry in selection['coverage']}, {'app.py', 'first.py', 'second.py'})
            self.assertEqual(selection['mandatorySecuritySuites'], [3])
            self.assertEqual(len(commands), 3)
            return self.combined(3)
        with self.selected(), patch.object(sandbox, 'run_session', side_effect=verify) as run:
            self.assertEqual(self.run_fake(fake), 0)
        run.assert_called_once()
        state = self.state()
        self.assertEqual(state['final_verification_result']['test_count'], 6)
        self.assertEqual(len([event for event in self.events() if event['event'] == 'test_started']), 1)

    def test_completed_receipt_reuse_does_not_launch_again_or_replay_agents(self):
        with self.selected(), patch.object(sandbox, 'run_session', return_value=self.combined(1)) as run:
            self.assertEqual(self.run_fake(final_fixtures.Executor()), 0)
            resumed = final_fixtures.Executor()
            with patch.object(sandbox, 'saved_session_evidence', return_value=[final_fixtures.PASSED]):
                self.assertEqual(self.run_fake(resumed, resume=True), 0)
        run.assert_called_once()
        self.assertEqual(resumed.calls, [])

    def test_missing_or_uncovered_mapping_stops_without_vm_or_automatic_correction(self):
        self.document['final_verification'] = copy.deepcopy(final_fixtures.POLICY)
        self.write_plan()
        fake = final_fixtures.Executor()
        with self.selected(), patch.object(sandbox, 'run_session') as run, self.assertRaisesRegex(runner.PilotError, 'session_coverage'):
            self.run_fake(fake)
        run.assert_not_called()
        self.assertEqual(fake.calls, ['A', 'B'])
        self.assertEqual(self.state()['completed'], ['one'])

    def test_policy_change_cannot_resume_or_silently_drop_security_suite(self):
        with self.selected(), patch.object(sandbox, 'run_session', return_value=self.combined(1)) as run:
            self.assertEqual(self.run_fake(final_fixtures.Executor()), 0)
            self.document['final_verification']['session_coverage']['rules'][0]['reason'] = 'changed mapping'
            self.write_plan()
            with self.assertRaises(runner.PilotError):
                self.run_fake(final_fixtures.Executor(), resume=True)
        run.assert_called_once()

    def test_inconsistent_per_suite_cleanup_is_uncertain(self):
        result = self.combined(1)
        result.suite_results = [runner._CallResult(0, final_fixtures.PASSED, False, False, True)]
        with self.selected(), patch.object(sandbox, 'run_session', return_value=result):
            self.assertEqual(self.run_fake(final_fixtures.Executor()), 1)
        self.assertIn('uncertain_test', self.state())


class SessionAdapterTests(unittest.TestCase):
    setUp = fixtures.AdapterTests.setUp
    git = fixtures.AdapterTests.git
    commit = fixtures.AdapterTests.commit
    write = fixtures.AdapterTests.write

    def prepare(self):
        self.policy = fixtures.scoped_policy({'test_commands': [
            ['python', '-m', 'unittest', 'test_one'], ['python', '-m', 'unittest', 'test_two']],
            'minimum_tests': 4, 'timeout_seconds': 30})
        state = {'initial_git_head': self.initial, 'run_id': 'session', 'branch': 'feature/session',
                 'completed': ['one'], 'history': [{'task': 'one', 'base_head': self.initial, 'commit': self.head,
                 'review': {'decision': 'APPROVED', 'issues': []}}]}
        self.selection = session_testing.select_session(self.repo, state, self.head, self.policy, policy_sha256(self.policy), self.git)

    def fixture(self, request, root):
        # Synthetic receipt double only; never represented as real VM evidence.
        legacy = copy.deepcopy(request)
        legacy.update(command=request['commands'][0], approvedCommand=request['approvedCommands'][0])
        summary = fixtures.AdapterTests.fixture(self, legacy, root)
        result = summary['baseline']
        result.update(executionScope=sandbox.SESSION_SCOPE, sourceStartCommit=request['sourceStartCommit'],
                      selectionSha256=request['selectionSha256'], totalTests=4)
        first = result['suites'][0]
        second = copy.deepcopy(first)
        second['file'] = 'session-suite-2'
        second['process'].update(arguments=request['commands'][1][1:], executable=request['commands'][1][0],
            stdout=r'C:\ACResults\session-suite-2.stdout.log', stderr=r'C:\ACResults\session-suite-2.stderr.log')
        result['suites'] = [first, second]
        for suite in result['suites']:
            name = suite['file']
            self.write(root / 'results' / (name + '.process.json'), suite['process'])
            self.write(root / 'results' / (name + '.suite.json'), suite)
            (root / 'results' / (name + '.stdout.log')).write_text('')
            (root / 'results' / (name + '.stderr.log')).write_text(final_fixtures.PASSED)
        self.write(root / 'request.json', request)
        self.write(root / 'summary.json', summary)
        self.write(root / 'results/baseline.json', result)
        return summary

    def invoke(self, launch):
        locate = sandbox.shutil.which
        with patch.object(sandbox, 'WINDOWS', True), patch.object(sandbox.shutil, 'which',
                side_effect=lambda name: 'wsb.exe' if name == 'wsb' else locate(name)), \
                patch.dict(os.environ, {'SYSTEMROOT': os.environ.get('SYSTEMROOT', str(self.root / 'Windows'))}), \
                patch.object(Path, 'is_file', return_value=True):
            return sandbox.run_session(self.policy['test_commands'], self.repo, self.logs, 30,
                git=self.git, expected_head=self.head, expected_branch='feature/session', policy=self.policy,
                selection=self.selection, session_run_id='session', invocation=1, launch=launch)

    def test_one_launch_one_package_and_one_integrity_pair_for_both_commands(self):
        self.prepare()
        calls = []
        def launch(argv, **options):
            calls.append(argv)
            self.assertEqual(options['timeout'], 1560)
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            self.assertEqual(request['approvedCommands'], self.policy['test_commands'])
            self.fixture(request, root)
            return subprocess.CompletedProcess(argv, 0, '', '')
        result = self.invoke(launch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list(self.logs.glob('windows-sandbox-*'))), 1)
        self.assertEqual(result[0], 0)
        self.assertEqual(len(result.suite_results), 2)
        self.assertTrue(source_integrity.valid_verdict(result.source_integrity))
        outputs = sandbox.saved_session_evidence(self.logs, result.windows_sandbox, head=self.head,
            branch='feature/session', policy=self.policy, run_id='session', invocation=1,
            selection=self.selection, source_repo=self.repo, integrity_verdict=result.source_integrity)
        self.assertEqual(outputs, ['\n' + final_fixtures.PASSED] * 2)
        for field, value in (('policySha256', '0' * 64), ('sourceStartCommit', '0' * 40),
                             ('selectionSha256', '0' * 64)):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            altered = copy.deepcopy(request)
            altered[field] = value
            self.write(root / 'request.json', altered)
            with self.subTest(field=field), self.assertRaises(ValueError):
                sandbox.saved_session_evidence(self.logs, result.windows_sandbox, head=self.head,
                    branch='feature/session', policy=self.policy, run_id='session', invocation=1,
                    selection=self.selection, source_repo=self.repo, integrity_verdict=result.source_integrity)
            self.write(root / 'request.json', request)

    def test_missing_duplicate_or_wrong_second_suite_is_rejected(self):
        self.prepare()
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            summary = self.fixture(request, root)
            for mode in ('missing', 'duplicate', 'command'):
                value = copy.deepcopy(summary)
                if mode == 'missing': value['baseline']['suites'].pop()
                elif mode == 'duplicate': value['baseline']['suites'][1]['file'] = 'session-suite-1'
                else: value['baseline']['suites'][1]['process']['arguments'] = ['different']
                self.write(root / 'summary.json', value)
                self.write(root / 'results/baseline.json', value['baseline'])
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    sandbox.validate_evidence(root, request)
            self.write(root / 'summary.json', summary)
            self.write(root / 'results/baseline.json', summary['baseline'])
            stderr = root / 'results/session-suite-2.stderr.log'
            for output in ('Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n',
                           'Ran 2 tests in 0.01s\n\nOK (skipped=1)\n',
                           'Ran 1 test in 0.01s\n\nOK\n', 'incomplete output'):
                stderr.write_text(output)
                with self.subTest(output=output), self.assertRaises(ValueError):
                    sandbox.validate_evidence(root, request)
            stderr.write_text(final_fixtures.PASSED)
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.assertEqual(self.invoke(launch)[0], 0)


    def test_second_suite_failure_skip_timeout_and_short_count_cannot_pass(self):
        self.prepare()
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            summary = self.fixture(request, root)
            for mode in ('failure', 'error', 'skip', 'timeout', 'short'):
                value = copy.deepcopy(summary)
                suite = value['baseline']['suites'][1]
                suite.update(status='TIMEOUT' if mode == 'timeout' else 'PASS' if mode == 'short' else 'FAIL',
                    tests=1 if mode == 'short' else 2, failures=int(mode == 'failure'),
                    errors=int(mode == 'error'), skips=int(mode == 'skip'))
                suite['process'].update(exitCode=124 if mode == 'timeout' else 1 if mode in ('failure', 'error') else 0,
                                        timedOut=mode == 'timeout')
                value['baseline'].update(totalTests=3 if mode == 'short' else 4, success=False, operationExitCode=1)
                value['outcome'] = 'FAIL'
                verdict = {'failure': 'FAILED (failures=1)', 'error': 'FAILED (errors=1)',
                           'skip': 'OK (skipped=1)', 'timeout': 'OK', 'short': 'OK'}[mode]
                (root / 'results/session-suite-2.stderr.log').write_text(f"Ran {suite['tests']} tests in 0.01s\n\n{verdict}\n")
                self.write(root / 'results/session-suite-2.process.json', suite['process'])
                self.write(root / 'results/session-suite-2.suite.json', suite)
                self.write(root / 'summary.json', value)
                self.write(root / 'results/baseline.json', value['baseline'])
                self.write(root / 'exec-baseline.output.txt', {'ExitCode': 1})
                with self.subTest(mode=mode):
                    evidence = sandbox.validate_evidence(root, request, require_pass=False)
                    self.assertEqual(evidence[0], 1)
                    self.assertEqual(len(evidence.suites), 2)
                    with self.assertRaises(ValueError):
                        sandbox.validate_evidence(root, request)
            self.fixture(request, root)
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.assertEqual(self.invoke(launch)[0], 0)

    def test_post_scan_must_follow_last_suite_not_only_first(self):
        self.prepare()
        def launch(argv, **options):
            root = next(self.logs.glob('windows-sandbox-*'))
            request = sandbox._json(root / 'request.json')
            summary = self.fixture(request, root)
            value = copy.deepcopy(summary)
            suite = value['baseline']['suites'][1]
            late = (sandbox._timestamp(suite['process']['finishedUtc']) + dt.timedelta(seconds=1)).isoformat()
            suite['process']['finishedUtc'] = late
            value['baseline']['finishedUtc'] = late
            self.write(root / 'summary.json', value)
            self.write(root / 'results/baseline.json', value['baseline'])
            with self.assertRaisesRegex(source_integrity.IntegrityError, 'before/after'):
                sandbox.validate_evidence(root, request)
            self.fixture(request, root)
            return subprocess.CompletedProcess(argv, 0, '', '')
        self.assertEqual(self.invoke(launch)[0], 0)


class PowerShellSessionContractTests(unittest.TestCase):
    def test_shared_host_guest_helper_preserves_all_argv_and_counts(self):
        if sys.platform != 'win32':
            self.assertIn('Get-SessionSuites', (sandbox.SCRIPTS / 'baseline.ps1').read_text())
            self.assertIn('Get-SessionSuites', (sandbox.SCRIPTS / 'run.ps1').read_text())
            return
        common = str(sandbox.SCRIPTS / 'common.ps1').replace("'", "''")
        request = {'executionScope': sandbox.SESSION_SCOPE,
                   'commands': [['python', '-m', 'unittest', 'test_one'], ['python', 'tests/test two.py']],
                   'minimumTestsPerSuite': [2, 3]}
        encoded = base64.b64encode(json.dumps(request).encode('utf-8')).decode()
        run_script = str(sandbox.SCRIPTS / 'run.ps1').replace("'", "''")
        code = (f"$ErrorActionPreference='Stop'; . ([scriptblock]::Create([IO.File]::ReadAllText('{common}'))); "
                f"$a=[Management.Automation.Language.Parser]::ParseFile('{run_script}',[ref]$null,[ref]$null); "
                "$launches=@($a.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst] -and "
                "$n.GetCommandName() -eq 'Invoke-Wsb' -and $n.CommandElements[1].Value -eq 'start'},$true)); "
                "if($launches.Count -ne 1){throw 'Expected exactly one Sandbox start call'}; "
                "$parent=$launches[0].Parent; while($null -ne $parent){"
                "if($parent.GetType().Name -match '^(ForEach|For|While|DoWhile|DoUntil)StatementAst$'){"
                "throw 'Sandbox launch must be outside loops'}; $parent=$parent.Parent}; "
                f"$r=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))|ConvertFrom-Json; "
                "$s=@(Get-SessionSuites $r); ConvertTo-Json -InputObject $s -Depth 6 -Compress")
        process = bounded_run([str(Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
            '-NoProfile', '-NonInteractive', '-EncodedCommand', base64.b64encode(code.encode('utf-16-le')).decode()],
            timeout=20, capture_output=True, shell=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        suites = json.loads(process.stdout)
        self.assertEqual([suite['command'] for suite in suites], request['commands'])
        self.assertEqual([suite['file'] for suite in suites], ['session-suite-1', 'session-suite-2'])
        self.assertEqual([suite['minimumTests'] for suite in suites], [2, 3])
