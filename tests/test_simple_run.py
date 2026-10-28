import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_coding.simple_run import load_tasks, run, PilotError
from auto_coding.event_log import EventLog


class SimpleRunnerTests(unittest.TestCase):
    def test_auto_push_failure_stops_before_next_task_and_logs_exact_sha(self):
        from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-b', 'feature/auto'], cwd=self.repo, check=True, capture_output=True)
        self.plan = self.root / 'tasks.json'
        self.plan.write_text(json.dumps({'tasks':[{'id':'one', 'prompt':'First'}, {'id':'two', 'prompt':'Second'}]}))
        calls = []
        def fake(argv, cwd, timeout, *, prompt=None):
            calls.append(argv)
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision':'APPROVED', 'issues':[], 'summary':'OK'}))
            else:
                (cwd / 'app.txt').write_text('approved')
                out.write_text('done')
            return 0, '', False
        with patch.object(GitHubWorkspaceManager, 'publish_checkpoint', side_effect=WorkspaceError('authentication', 'Access failed')):
            self.assertEqual(run(self.plan, self.repo, self.logs, execute=fake,
                                 auto_push={'repository':'owner/repo', 'branch':'feature/auto'}), 1)
        self.assertEqual(len(calls), 2)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['one'])
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        push = [e for e in events if e['event'].startswith('push_')]
        self.assertEqual([e['event'] for e in push], ['push_started', 'push_finished'])
        self.assertTrue(all(e['timestamp_utc'] and e['metadata']['commit'] == state['history'][0]['commit'] for e in push))
        self.assertEqual(push[-1]['metadata']['remote'], 'unconfirmed')

    def test_auto_push_recovers_interrupted_commit_before_publication(self):
        import auto_coding.simple_run as runner
        from auto_coding.github_workspace import GitHubWorkspaceManager
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-b', 'feature/auto'], cwd=self.repo, check=True, capture_output=True)
        config = {'repository': 'owner/repo', 'branch': 'feature/auto'}
        def fake(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                (cwd / 'app.txt').write_text('approved')
                out.write_text('done')
            return 0, '', False
        original_commit = runner.local_commit
        def interrupted(repo, task):
            original_commit(repo, task)
            raise PilotError('interrupted after commit')
        with patch.object(runner, 'local_commit', side_effect=interrupted), patch.object(GitHubWorkspaceManager, 'publish_checkpoint') as publish:
            self.assertEqual(run(self.plan, self.repo, self.logs, execute=fake, auto_push=config), 1)
            publish.assert_not_called()
        sha = runner.head(self.repo)
        with patch.object(GitHubWorkspaceManager, 'publish_checkpoint') as publish:
            self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, auto_push=config,
                                 execute=lambda *a, **kw: self.fail('Codex must not rerun')), 0)
            self.assertEqual(publish.call_args.args[3], sha)
        self.assertEqual(runner.head(self.repo), sha)

    def test_auto_push_stops_and_resume_retries_without_codex_or_duplicate_commit(self):
        from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-b', 'feature/auto'], cwd=self.repo, check=True, capture_output=True)
        config = {'repository': 'owner/repo', 'branch': 'feature/auto'}
        def fake(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                (cwd / 'app.txt').write_text('approved')
                out.write_text('done')
            return 0, '', False
        with patch.object(GitHubWorkspaceManager, 'publish_checkpoint', side_effect=WorkspaceError('network', 'Network unavailable')):
            self.assertEqual(run(self.plan, self.repo, self.logs, execute=fake, auto_push=config), 1)
        before = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(before['status'], 'stopped')
        self.assertEqual(before['publication']['outcome'], 'pending')
        sha = before['history'][0]['commit']
        with patch.object(GitHubWorkspaceManager, 'publish_checkpoint') as publish:
            self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, auto_push=config,
                                 execute=lambda *a, **kw: self.fail('Codex must not rerun')), 0)
            self.assertEqual(publish.call_args.args[3], sha)
        after = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(after['publication']['outcome'], 'published')
        self.assertEqual(after['history'], before['history'])
        with patch.object(GitHubWorkspaceManager, 'publish_checkpoint', side_effect=WorkspaceError('network', 'Offline')):
            self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, auto_push=config), 1)
        self.assertEqual(json.loads((self.logs / 'state.json').read_text())['publication']['outcome'], 'pending')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / 'task.txt'
        self.plan.write_text('Create a demo file', encoding='utf-8')
        self.repo = self.root / 'demo'
        self.logs = self.root / 'logs'

    def test_successful_codex_a_and_b(self):
        calls = []
        def fake(argv, cwd, timeout, *, prompt=None):
            calls.append((argv, prompt))
            idx = argv.index('--output-last-message')
            out = Path(argv[idx + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}), encoding='utf-8')
            else:
                (cwd / 'index.html').write_text('<h1>working</h1>', encoding='utf-8')
                out.write_text('Written', encoding='utf-8')
            return 0, 'jsonl events', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['task-1'])
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][calls[1][0].index('--sandbox') + 1], 'read-only')
        for policy in ('substantive, reproducible blockers', 'CURRENT task/slice',
                       'List ALL such blockers', 'optional polish', 'non-blocking suggestions',
                       'run-isolation risks', 'concrete user-visible defects', 'Optional styling changes are not approval blockers'):
            self.assertIn(policy, calls[1][1])
        self.assertNotIn('--model', calls[0][0])
        self.assertNotIn('--model', calls[1][0])
        self.assertTrue((self.repo / 'index.html').exists())
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]['event'], 'run_completed')
        self.assertEqual(json.loads((self.logs / 'run-manifest.json').read_text())['run_id'], events[0]['run_id'])
        self.assertFalse((self.repo / 'events.jsonl').exists())

    def test_failed_approved_test_then_feedback_and_review(self):
        self.plan = self.root / 'task.json'
        self.plan.write_text(json.dumps({'tasks': [
            {'id': 'one', 'prompt': 'Build', 'test_commands': [['py', '-B', 'test.py']]}
        ]}), encoding='utf-8')
        calls = []
        def fake(argv, cwd, timeout, *, prompt=None, env=None):
            calls.append((argv, prompt))
            if argv[:2] == ['py', '-B']:
                return (1, 'AssertionError: expected 2', False) if len([c for c in calls if c[0][:2] == ['py', '-B']]) == 1 else (0, 'OK', False)
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                (cwd / 'test.py').write_text('pass')
                out.write_text('Written')
            return 0, 'events', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        self.assertEqual(len([h for h in json.loads((self.logs / 'state.json').read_text())['history'] if h['task'] == 'one']), 2)
        self.assertTrue(any('AssertionError' in p for a, p in calls if p))
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        names = [event['event'] for event in events]
        self.assertLess(names.index('test_finished'), names.index('correction_requested'))
        self.assertIn('task_completed', names)
        failed_test = next(event for event in events if event['event'] == 'test_finished' and event['outcome'] == 'failed')
        self.assertEqual(failed_test['evidence']['output'], 'one-01-test-1.txt')

    def test_rejected_review_returns_feedback_to_coder(self):
        reviews = 0
        prompts = []
        def fake(argv, cwd, timeout, *, prompt=None):
            nonlocal reviews
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                reviews += 1
                data = {'decision': 'REJECTED', 'issues': ['No reset button'], 'summary': 'Incomplete'} if reviews == 1 else {'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}
                out.write_text(json.dumps(data))
            else:
                prompts.append(prompt)
                (cwd / 'index.html').write_text('<html></html>')
                out.write_text('Written')
            return 0, 'events', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        self.assertEqual(reviews, 2)
        self.assertIn('No reset button', prompts[-1])
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        review_events = [event for event in events if event['event'] == 'review_finished']
        self.assertEqual([event['outcome'] for event in review_events], ['rejected', 'approved'])
        self.assertTrue(any(event['event'] == 'correction_requested' and event.get('evidence', {}).get('verdict') == 'task-1-01-b-verdict.json' for event in events))

    def test_stop_can_resume_same_repo_without_new_branch(self):
        def fail(argv, cwd, timeout, *, prompt=None):
            return 1, 'crashed', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fail), 1)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['status'], 'stopped')
        def success(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            else:
                (cwd / 'hello.txt').write_text('worked')
                out.write_text('Written')
            return 0, 'ok', False
        self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, execute=success), 0)
        timeline = self.logs / 'events.jsonl'
        events = [json.loads(line) for line in timeline.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event['run_id'] for event in events}), 1)
        self.assertEqual(json.loads((self.logs / 'run-manifest.json').read_text())['run_id'], events[0]['run_id'])
        self.assertIn('stopped', [event['event'] for event in events])
        stopped = next(event for event in events if event['event'] == 'stopped')
        self.assertEqual(stopped['metadata']['reason'], 'Codex A process failure')
        self.assertIn('resume_started', [event['event'] for event in events])

    def test_existing_run_refuses_dirty_repo(self):
        self.repo.mkdir()
        subprocess.run(['git', 'init'], cwd=self.repo, stdout=subprocess.PIPE, check=True)
        (self.repo / 'surprise.txt').write_text('existing change')
        with self.assertRaises(PilotError):
            run(self.plan, self.repo, self.logs, execute=lambda *a, **k: (1, '', False))
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'stopped')
        self.assertEqual(events[-1]['metadata']['reason'], 'dirty worktree preflight')

    def test_logs_inside_target_repo_are_rejected_before_creation(self):
        self.repo.mkdir()
        subprocess.run(['git', 'init'], cwd=self.repo, stdout=subprocess.PIPE, check=True)
        nested_logs = self.repo / 'logs'
        with self.assertRaises(PilotError):
            run(self.plan, self.repo, nested_logs, execute=lambda *a, **k: (1, '', False))
        self.assertFalse(nested_logs.exists())

    def test_secret_test_argv_is_absent_from_structured_logs_and_manifest(self):
        secret = 'secret123'
        self.plan = self.root / 'task.json'
        self.plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'Build',
            'test_commands': [['python', 'check.py', '--token=' + secret]]}]}))
        def fake(argv, cwd, timeout, *, prompt=None, env=None):
            if argv[:2] == ['python', 'check.py']:
                return 0, 'ok', False
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                (cwd / 'file.txt').write_text('ok')
            return 0, 'ok', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        for filename in ('events.jsonl', 'run-manifest.json'):
            self.assertNotIn(secret, (self.logs / filename).read_text(encoding='utf-8'))

    def test_manifest_creation_failure_never_publishes_running_state_and_retry_succeeds(self):
        import auto_coding.simple_run as mod
        original = mod.save_json
        failed = {'once': False}
        def flaky(path, value):
            if path.name == 'run-manifest.json' and not failed['once']:
                failed['once'] = True
                raise OSError('simulated manifest storage failure')
            return original(path, value)
        with patch.object(mod, 'save_json', side_effect=flaky):
            with self.assertRaises(OSError):
                run(self.plan, self.repo, self.logs, create_repo=True, execute=lambda *a, **k: (1, '', False))
        state_path = self.logs / 'state.json'
        self.assertFalse(state_path.exists())
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'stopped')
        self.assertEqual(events[-1]['outcome'], 'failed')
        def success(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            else:
                (cwd/'hello.txt').write_text('ok')
            return 0, 'ok', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=success), 0)

    def test_run_started_write_failure_is_retryable_without_running_state(self):
        import auto_coding.event_log as event_mod
        original = event_mod.EventLog.emit
        failed = {'once': False}
        def fail_once(log, actor, event, outcome, **kwargs):
            if event == 'run_started' and not failed['once']:
                failed['once'] = True
                # Simulate a durable append followed by a caller-visible failure.
                original(log, actor, event, outcome, **kwargs)
                raise event_mod.EventLogError('simulated post-write interruption')
            return original(log, actor, event, outcome, **kwargs)
        with patch.object(event_mod.EventLog, 'emit', fail_once):
            with self.assertRaises(event_mod.EventLogError):
                run(self.plan, self.repo, self.logs, create_repo=True,
                    execute=lambda *a, **k: (1, '', False))
        self.assertFalse((self.logs / 'state.json').exists())
        first = json.loads((self.logs / 'events.jsonl').read_text().splitlines()[0])
        self.assertEqual(first['event'], 'run_started')
        def success(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            else:
                (cwd/'hello.txt').write_text('ok')
            return 0, 'ok', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=success), 0)
        records = [json.loads(line) for line in (self.logs/'events.jsonl').read_text().splitlines()]
        self.assertEqual(len({event['run_id'] for event in records}), 1)
        self.assertEqual([event['seq'] for event in records], list(range(1, len(records)+1)))

if __name__ == '__main__':
    unittest.main()

class CheckpointAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "target"
        self.logs = self.root / "logs"
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"tasks": [
            {"id": "first", "prompt": "Make first file"},
            {"id": "second", "prompt": "Make second file"}
        ]}), encoding="utf-8")

    def test_test_timeout_default_and_custom_are_independent_of_codex(self):
        import auto_coding.simple_run as runner
        tasks = json.loads(self.plan.read_text())
        for task in tasks['tasks']:
            task['test_commands'] = [['fake-test', task['id']], ['another-test']]
        self.plan.write_text(json.dumps(tasks))
        for limit in (None, 1234):
            with self.subTest(limit=limit):
                self.repo = self.root / f'target-{limit}'
                self.logs = self.root / f'logs-{limit}'
                codex, _ = self.make_executor()
                calls = []
                def fake(argv, cwd, timeout, *, prompt=None, env=None):
                    calls.append((argv, cwd, timeout))
                    if prompt is None:
                        return 0, 'test stdout and stderr', False
                    return codex(argv, cwd, timeout, prompt=prompt)
                self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True,
                                     timeout=30, test_timeout=limit, execute=fake), 0)
                expected = runner.DEFAULT_TEST_TIMEOUT if limit is None else limit
                self.assertEqual(expected, 900 if limit is None else 1234)
                self.assertEqual([t for a, c, t in calls if a[0] != 'codex'], [expected] * 4)
                self.assertEqual([t for a, c, t in calls if a[0] == 'codex'], [30] * 4)
                self.assertTrue(all(c == self.repo for a, c, t in calls))
                state = json.loads((self.logs / 'state.json').read_text())
                self.assertEqual(state['test_timeout'], expected)
                self.assertEqual((self.logs / 'second-01-test-2.txt').read_text(), 'test stdout and stderr')

    def test_test_timeout_stops_and_resume_preserves_limit_and_approved_checkpoint(self):
        import auto_coding.simple_run as runner
        tasks = json.loads(self.plan.read_text())
        tasks['tasks'][1]['test_commands'] = [['hung-test'], ['must-not-run']]
        self.plan.write_text(json.dumps(tasks))
        codex, seen = self.make_executor()
        tests = []
        hung = True
        def fake(argv, cwd, timeout, *, prompt=None, env=None):
            if prompt is not None:
                return codex(argv, cwd, timeout, prompt=prompt)
            tests.append((argv, timeout))
            with patch.object(runner.subprocess, 'run') as process, patch.object(runner, 'mark_process_uncertain') as uncertain:
                if hung:
                    process.side_effect = subprocess.TimeoutExpired(argv, timeout, output=b'partial stdout/stderr')
                else:
                    process.return_value = subprocess.CompletedProcess(argv, 0, 'ok')
                result = runner.call(argv, cwd, timeout, env=env)
                self.assertEqual(process.call_args.args, (argv,))
                self.assertEqual(process.call_args.kwargs['timeout'], 1200)
                self.assertEqual(process.call_args.kwargs['stdout'], subprocess.PIPE)
                self.assertEqual(process.call_args.kwargs['stderr'], subprocess.STDOUT)
                self.assertEqual(uncertain.call_count, int(hung))
                return result
        with patch('builtins.print') as printed:
            self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True,
                                 timeout=30, test_timeout=1200, execute=fake), 1)
        self.assertEqual(tests, [(['hung-test'], 1200)])
        self.assertEqual([role for role, p in seen], ['a', 'b', 'a'])
        self.assertTrue(any('[second] test 1 timed out after 1200 seconds' in str(c) for c in printed.call_args_list))
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['completed'], ['first'])
        checkpoint = state['history'][0]['commit']
        self.assertEqual(state['history'][-1]['tests'], [
            {'index': 1, 'exit_code': 124, 'timed_out': True, 'timeout_seconds': 1200}])
        self.assertIsNone(state['history'][-1]['review'])
        self.assertIn('partial stdout/stderr', (self.logs / 'second-01-test-1.txt').read_text())
        events = [json.loads(line) for line in (self.logs / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['metadata'], {'reason': 'test timeout', 'index': 1, 'timeout_seconds': 1200})
        finished = [e for e in events if e['event'] == 'test_finished'][0]
        self.assertEqual(finished['outcome'], 'timed_out')
        self.assertEqual(finished['metadata']['exit_code'], 124)
        hung = False
        self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, timeout=30, execute=fake), 0)
        self.assertEqual([t for a, t in tests], [1200, 1200, 1200])
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['history'][0]['commit'], checkpoint)
        self.assertEqual(state['completed'], ['first', 'second'])
        self.assertEqual([role for role, p in seen], ['a', 'b', 'a', 'a', 'b'])

    def test_legacy_resume_default_and_explicit_override(self):
        tasks = json.loads(self.plan.read_text())
        tasks['tasks'][1]['test_commands'] = [['fake-test']]
        self.plan.write_text(json.dumps(tasks))
        codex, _ = self.make_executor(fail_second=True)
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=codex), 1)
        state_path = self.logs / 'state.json'
        state = json.loads(state_path.read_text())
        state.pop('test_timeout')
        state_path.write_text(json.dumps(state))
        limits = []
        def fake(argv, cwd, timeout, *, prompt=None, env=None):
            if prompt is None:
                limits.append(timeout)
                return 124, 'timeout', True
            return codex(argv, cwd, timeout, prompt=prompt)
        self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, execute=fake), 1)
        self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, test_timeout=1500, execute=fake), 1)
        self.assertEqual(limits, [900, 1500])
        self.assertEqual(json.loads(state_path.read_text())['test_timeout'], 1500)

    def test_test_timeout_cli_wiring_and_invalid_values(self):
        import auto_coding.simple_run as runner
        args = [str(self.plan), '--repo', str(self.repo), '--logs', str(self.logs)]
        with patch.object(runner.shutil, 'which', return_value='codex'), patch.object(runner, 'run', return_value=0) as invoked:
            self.assertEqual(runner.main(args + ['--test-timeout', '1234', '--timeout', '60']), 0)
            self.assertEqual(invoked.call_args.kwargs['test_timeout'], 1234)
            self.assertEqual(invoked.call_args.kwargs['timeout'], 60)
            invoked.reset_mock()
            for value in ('0', '-1', '86401', '1.5', 'nan', 'inf', 'no'):
                with self.subTest(value=value), patch('sys.stderr'), self.assertRaises(SystemExit) as exc:
                    runner.main(args + ['--test-timeout', value])
                self.assertEqual(exc.exception.code, 2)
            invoked.assert_not_called()
        for value in (0, -1, 86401, 1.5, float('nan'), float('inf'), True, '900'):
            with self.subTest(value=value), patch('sys.stderr'), self.assertRaises(PilotError):
                run(self.plan, self.repo, self.logs, create_repo=True, test_timeout=value,
                    execute=lambda *a, **kw: self.fail('Invalid timeout must not execute'))

    def make_executor(self, fail_second=False, write_second=True):
        seen = []
        def fake(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                seen.append(('b', prompt))
                out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            else:
                seen.append(('a', prompt))
                if 'Make second file' in prompt:
                    if fail_second and not (cwd / 'fail.marker').exists():
                        (cwd / 'fail.marker').write_text('marker')
                        return 1, 'failed once', False
                    if write_second:
                        (cwd / 'two.txt').write_text('second')
                else:
                    (cwd / 'one.txt').write_text('first')
                out.write_text('Completed')
            return 0, 'events', False
        return fake, seen

    def test_each_task_has_its_own_local_commit_and_review_diff(self):
        fake, seen = self.make_executor()
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['first','second'])
        self.assertEqual(len({h['commit'] for h in state['history']}), 2)
        commits = subprocess.check_output(['git','log','--format=%s'],cwd=self.repo,text=True).splitlines()
        self.assertEqual(commits, ['auto-coding: second', 'auto-coding: first'])
        reviews = [p for role,p in seen if role == 'b']
        self.assertIn('one.txt', reviews[0])
        self.assertIn('two.txt', reviews[1])
        self.assertNotIn('one.txt', reviews[1].split('DIFF (truncated; inspect repository for full):')[1].split('TEST RESULTS:')[0])
        self.assertEqual(subprocess.check_output(['git','status','--porcelain'],cwd=self.repo,text=True),'')

    def test_no_second_change_never_reuses_first_task_as_evidence(self):
        fake, seen = self.make_executor(write_second=False)
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, attempts=2, execute=fake), 1)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['first'])
        self.assertEqual([role for role,p in seen], ['a','b','a','a'])
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(len(subprocess.check_output(['git','log','--format=%s'],cwd=self.repo,text=True).splitlines()),1)


    def test_resume_after_stopped_second_task_keeps_first_commit(self):
        calls = {'second':0}
        def fake(argv,cwd,timeout,*,prompt=None):
            path = Path(argv[argv.index('--output-last-message')+1])
            if '--output-schema' in argv:
                path.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            elif 'Make second file' in prompt:
                calls['second']+=1
                if calls['second']==1:
                    return 1, 'failed', False
                (cwd/'two.txt').write_text('second'); path.write_text('ok')
            else:
                (cwd/'one.txt').write_text('first');path.write_text('ok')
            return 0,'ok',False
        self.assertEqual(run(self.plan,self.repo,self.logs,create_repo=True,execute=fake),1)
        original_sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repo,text=True).strip()
        self.assertEqual(run(self.plan,self.repo,self.logs,resume=True,execute=fake),0)
        history = json.loads((self.logs/'state.json').read_text())['history']
        self.assertEqual([h['task'] for h in history],['first','second','second'])
        self.assertEqual(history[0]['commit'],original_sha)
        self.assertEqual(calls['second'],2)

    def test_generated_bytecode_is_not_committed_or_sent_to_reviewer(self):
        reviews = []
        def fake(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                reviews.append(prompt)
                out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
            else:
                if 'Make second file' in prompt:
                    (cwd/'viewer.html').write_text('<h1>Viewer</h1>')
                else:
                    (cwd/'run_summary.py').write_text('VALUE = 1')
                    cache = cwd/'__pycache__'/'run_summary.cpython-313.pyc'
                    cache.parent.mkdir(exist_ok=True)
                    cache.write_bytes(b'\x00\x01bytecode')
                    # Simulate a previous V2 run that had already staged this cache.
                    subprocess.run(['git', 'add', '-A'], cwd=cwd, check=True)
                out.write_text('ok')
            return 0, 'events', False
        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 0)
        committed = subprocess.check_output(['git', 'ls-files'], cwd=self.repo, text=True)
        self.assertIn('run_summary.py', committed)
        self.assertNotIn('__pycache__', committed)
        self.assertTrue((self.repo/'__pycache__'/'run_summary.cpython-313.pyc').exists())
        self.assertTrue(reviews)
        self.assertNotIn('__pycache__', reviews[0])
        self.assertEqual(subprocess.check_output(['git','status','--porcelain'], cwd=self.repo, text=True), '')

    def test_resume_with_old_staged_generated_file_preserves_first_commit(self):
        attempts = {'second': 0}
        def fake(argv, cwd, timeout, *, prompt=None):
            out = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                out.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            elif 'Make second file' in prompt:
                attempts['second'] += 1
                (cwd/'viewer.html').write_text('<h1>Viewer</h1>')
                cache = cwd/'__pycache__'/'run_summary.cpython-313.pyc'
                cache.parent.mkdir(exist_ok=True)
                cache.write_bytes(b'temporary bytecode')
                # Previous runner attempted to stage the unwanted artifact.
                subprocess.run(['git', 'add', '-A'], cwd=cwd, check=True)
                subprocess.run(['git', 'add', '-f', '--', '__pycache__/run_summary.cpython-313.pyc'], cwd=cwd, check=True)
                if attempts['second'] == 1:
                    return 1, 'simulated interruption before review', False
                out.write_text('completed')
            else:
                (cwd/'run_summary.py').write_text('VALUE = 1')
                out.write_text('completed')
            return 0, 'events', False

        self.assertEqual(run(self.plan, self.repo, self.logs, create_repo=True, execute=fake), 1)
        before = json.loads((self.logs/'state.json').read_text())
        self.assertEqual(before['completed'], ['first'])
        original = before['history'][0]['commit']
        self.assertIn('__pycache__', subprocess.check_output(['git', 'diff', '--cached', '--name-only'], cwd=self.repo, text=True))

        self.assertEqual(run(self.plan, self.repo, self.logs, resume=True, execute=fake), 0)
        after = json.loads((self.logs/'state.json').read_text())
        self.assertEqual(after['completed'], ['first', 'second'])
        self.assertEqual(after['history'][0]['commit'], original)
        self.assertNotIn('__pycache__', subprocess.check_output(['git', 'ls-files'], cwd=self.repo, text=True))
        self.assertEqual(subprocess.check_output(['git', 'status', '--porcelain'], cwd=self.repo, text=True), '')

    def test_sensitive_file_does_not_get_approved_or_committed(self):
        def fake(argv,cwd,timeout,*,prompt=None):
            out=Path(argv[argv.index('--output-last-message')+1])
            if '--output-schema' in argv:
                raise AssertionError('Reviewer must not run on unapproved secret file')
            (cwd/'.env').write_text('NOT_A_REAL_SECRET=test');out.write_text('ok')
            return 0,'ok',False
        self.assertEqual(run(self.plan,self.repo,self.logs,create_repo=True,execute=fake),1)
        self.assertEqual(subprocess.run(['git','rev-parse','--verify','HEAD'],cwd=self.repo,capture_output=True).returncode,128)

    def test_recovers_commit_that_succeeded_before_state_write(self):
        import auto_coding.simple_run as mod
        fake, seen = self.make_executor()
        first_only = self.root/'first.json'
        first_only.write_text(json.dumps({'tasks':[{'id':'first','prompt':'Make first file'}]}))
        original = mod.local_commit
        calls = {'count':0}
        def commit_then_crash(repo,ident):
            calls['count']+=1
            sha = original(repo,ident)
            if calls['count']==1:
                raise mod.PilotError('simulated interruption after commit')
            return sha
        with patch.object(mod,'local_commit',commit_then_crash):
            self.assertEqual(run(first_only,self.repo,self.logs,create_repo=True,execute=fake),1)
        self.assertEqual(json.loads((self.logs/'state.json').read_text())['status'],'checkpointing')
        self.assertEqual(run(first_only,self.repo,self.logs,resume=True,execute=fake),0)
        state=json.loads((self.logs/'state.json').read_text())
        self.assertEqual(state['completed'],['first'])
        self.assertEqual(len(seen),2)
        self.assertEqual(len(subprocess.check_output(['git','log','--format=%s'],cwd=self.repo,text=True).splitlines()),1)
        timeline = [json.loads(line) for line in (self.logs/'events.jsonl').read_text().splitlines()]
        reconciled = [e for e in timeline if e['event'] in {'checkpoint_recovered','checkpoint_committed'}]
        self.assertEqual([e['event'] for e in reconciled], ['checkpoint_recovered','checkpoint_committed'])
        self.assertEqual(reconciled[-1]['metadata']['commit'], state['history'][0]['commit'])

    def test_normal_commit_rejects_last_second_unapproved_bytes(self):
        """Even a successful Git commit cannot bypass the approved diff check."""
        import auto_coding.simple_run as mod
        fake, _ = self.make_executor()
        only = self.root / 'only.json'
        only.write_text(json.dumps({'tasks': [{'id': 'first', 'prompt': 'Make first file'}]}))
        original = mod.local_commit
        def changed_commit(repo, ident):
            (repo / 'one.txt').write_text('tampered after review')
            subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
            return original(repo, ident)
        with patch.object(mod, 'local_commit', side_effect=changed_commit):
            self.assertEqual(run(only, self.repo, self.logs, create_repo=True, execute=fake), 1)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['status'], 'checkpointing')
        self.assertEqual(state['completed'], [])
        events = [json.loads(line) for line in (self.logs/'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(e['event'] == 'checkpoint_committed' for e in events))
        self.assertTrue(any(e['event'] == 'stopped' for e in events))

    def test_patch_hash_preserves_trailing_whitespace(self):
        """Do not use .strip() on patches: it can erase a last-line space."""
        import auto_coding.simple_run as mod
        self.repo.mkdir()
        subprocess.run(['git', 'init'], cwd=self.repo, stdout=subprocess.PIPE, check=True)
        (self.repo / 'one.txt').write_text('some text\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        first = mod.git_patch(self.repo, ['diff', '--cached', '--binary', '--no-ext-diff'])
        (self.repo / 'one.txt').write_text('some text \n')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        second = mod.git_patch(self.repo, ['diff', '--cached', '--binary', '--no-ext-diff'])
        self.assertNotEqual(first, second)

    def test_resume_rejects_same_subject_with_unapproved_content(self):
        """A forged local checkpoint must never be certified as Codex B approved."""
        import auto_coding.simple_run as mod
        fake, _ = self.make_executor()
        only = self.root / 'only.json'
        only.write_text(json.dumps({'tasks': [{'id': 'first', 'prompt': 'Make first file'}]}))
        with patch.object(mod, 'local_commit', side_effect=mod.PilotError('before commit')):
            self.assertEqual(run(only, self.repo, self.logs, create_repo=True, execute=fake), 1)
        original_state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(original_state['status'], 'checkpointing')
        self.assertIsNone(original_state['pending_checkpoint']['base_head'])
        (self.repo / 'one.txt').write_text('unapproved bytes')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--no-gpg-sign', '-m', 'auto-coding: first'],
                       cwd=self.repo, stdout=subprocess.PIPE, check=True)
        with self.assertRaisesRegex(PilotError, 'does not match the approved parent and diff'):
            run(only, self.repo, self.logs, resume=True, execute=fake)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], [])
        self.assertIsNotNone(state['pending_checkpoint'])
        events = [json.loads(line) for line in (self.logs/'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'stopped')
        self.assertFalse(any(e['event'] == 'checkpoint_committed' for e in events))

    def test_resume_rejects_matching_diff_on_unapproved_parent(self):
        """A task checkpoint must extend the exact reviewed base HEAD."""
        import auto_coding.simple_run as mod
        self.repo.mkdir()
        subprocess.run(['git', 'init'], cwd=self.repo, stdout=subprocess.PIPE, check=True)
        (self.repo / 'base.txt').write_text('base')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--no-gpg-sign', '-m', 'base'],
                       cwd=self.repo, stdout=subprocess.PIPE, check=True)
        base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
        only = self.root / 'only.json'
        only.write_text(json.dumps({'tasks': [{'id': 'first', 'prompt': 'Make first file'}]}))
        fake, _ = self.make_executor()
        with patch.object(mod, 'local_commit', side_effect=mod.PilotError('before commit')):
            self.assertEqual(run(only, self.repo, self.logs, execute=fake), 1)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['pending_checkpoint']['base_head'], base)
        # Reparent to a new unreviewed commit; keep task diff and subject identical.
        subprocess.run(['git', 'reset', '--mixed', base], cwd=self.repo,
                       stdout=subprocess.PIPE, check=True)
        (self.repo / 'one.txt').unlink()
        (self.repo / 'extra.txt').write_text('unreviewed base change')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--no-gpg-sign', '-m', 'unreviewed'],
                       cwd=self.repo, stdout=subprocess.PIPE, check=True)
        (self.repo / 'one.txt').write_text('first')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--no-gpg-sign', '-m', 'auto-coding: first'],
                       cwd=self.repo, stdout=subprocess.PIPE, check=True)
        with self.assertRaisesRegex(PilotError, 'does not match the approved parent and diff'):
            run(only, self.repo, self.logs, resume=True, execute=fake)
        state = json.loads((self.logs / 'state.json').read_text())
        self.assertEqual(state['completed'], [])
        self.assertEqual(state['pending_checkpoint']['base_head'], base)
        self.assertEqual([e['event'] for e in [json.loads(x) for x in (self.logs/'events.jsonl').read_text().splitlines()][-2:]], ['resume_started', 'stopped'])

    def test_resume_commits_pending_approved_diff_once_and_records_sha(self):
        import auto_coding.simple_run as mod
        fake, _ = self.make_executor()
        only = self.root/'only.json'
        only.write_text(json.dumps({'tasks':[{'id':'first','prompt':'Make first file'}]}))
        original = mod.local_commit
        with patch.object(mod, 'local_commit', side_effect=mod.PilotError('before commit')):
            self.assertEqual(run(only, self.repo, self.logs, create_repo=True, execute=fake), 1)
        self.assertEqual(run(only, self.repo, self.logs, resume=True, execute=fake), 0)
        state = json.loads((self.logs/'state.json').read_text())
        self.assertEqual(len(subprocess.check_output(['git','log','--format=%H'],cwd=self.repo,text=True).splitlines()), 1)
        timeline = [json.loads(line) for line in (self.logs/'events.jsonl').read_text().splitlines()]
        events = [e for e in timeline if e['event'] in {'checkpoint_started','checkpoint_committed'}]
        self.assertEqual([e['event'] for e in events], ['checkpoint_started','checkpoint_started','checkpoint_committed'])
        self.assertEqual(events[-1]['metadata']['commit'], state['history'][0]['commit'])
        self.assertLess(events[-2]['seq'], events[-1]['seq'])


class EventLogRecoveryTests(unittest.TestCase):
    def test_torn_final_line_is_preserved_and_sequence_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            log = EventLog(path, 'run-1')
            log.emit('controller', 'run_started', 'started')
            with path.open('ab') as stream:
                stream.write(b'{"partial":')
            resumed = EventLog(path, 'run-1')
            resumed.emit('controller', 'resume_started', 'started')
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([record['seq'] for record in records], [1, 2])
            self.assertEqual((Path(directory) / 'events.incomplete-0001').read_bytes(), b'{"partial":')
            with path.open('ab') as stream:
                stream.write(b'{"partial-again":')
            (Path(directory) / 'events.jsonl.recovery-tmp').write_text('stale')
            EventLog(path, 'run-1')
            self.assertEqual((Path(directory) / 'events.incomplete-0002').read_bytes(), b'{"partial-again":')

    def test_legacy_resume_uses_existing_timeline_id_before_creating_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, logs = root / 'repo', root / 'logs'
            plan = root / 'plan.json'
            plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'Build'}]}))
            def fail(argv, cwd, timeout, *, prompt=None):
                return 1, 'failure', False
            self.assertEqual(run(plan, repo, logs, create_repo=True, execute=fail), 1)
            timeline = logs / 'events.jsonl'
            records = [json.loads(line) for line in timeline.read_text().splitlines()]
            expected_id = records[0]['run_id']
            # Simulate a legacy stopped state with no manifest and a different
            # state run_id. Existing event identity must win without reset.
            (logs / 'run-manifest.json').unlink()
            state_path = logs / 'state.json'
            state = json.loads(state_path.read_text())
            state['run_id'] = 'legacy-state-id'
            state_path.write_text(json.dumps(state))
            self.assertEqual(run(plan, repo, logs, resume=True, execute=fail), 1)
            manifest = json.loads((logs / 'run-manifest.json').read_text())
            self.assertEqual(manifest['run_id'], expected_id)
            records = [json.loads(line) for line in timeline.read_text().splitlines()]
            self.assertTrue(all(record['run_id'] == expected_id for record in records))
            self.assertEqual([record['seq'] for record in records], list(range(1, len(records) + 1)))

    def test_manifest_mismatch_does_not_rewrite_manifest_or_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root/'plan.txt'; plan.write_text('Build')
            repo, logs = root/'repo', root/'logs'
            self.assertEqual(run(plan, repo, logs, create_repo=True, execute=lambda *a, **k: (1, 'x', False)), 1)
            manifest_path, timeline_path = logs/'run-manifest.json', logs/'events.jsonl'
            manifest_before = manifest_path.read_bytes()
            timeline_before = timeline_path.read_bytes()
            manifest = json.loads(manifest_before); manifest['task_ids'] = ['different']
            manifest_path.write_text(json.dumps(manifest))
            mismatch = manifest_path.read_bytes()
            with self.assertRaises(PilotError):
                run(plan, repo, logs, resume=True, execute=lambda *a, **k: (1, 'x', False))
            self.assertEqual(manifest_path.read_bytes(), mismatch)
            self.assertEqual(timeline_path.read_bytes(), timeline_before)

    def test_resume_safety_failure_is_recorded_as_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / 'plan.json'; plan.write_text(json.dumps({'tasks': [
                {'id': 'first', 'prompt': 'First'}, {'id': 'second', 'prompt': 'Second'}]}))
            repo = root / 'repo'; logs = root / 'logs'
            calls = {'a': 0}
            def fails(argv, cwd, timeout, *, prompt=None):
                out = Path(argv[argv.index('--output-last-message') + 1])
                if '--output-schema' in argv:
                    out.write_text(json.dumps({'decision':'APPROVED','issues':[],'summary':'OK'}))
                    return 0, 'ok', False
                calls['a'] += 1
                if calls['a'] == 1:
                    (cwd / 'first.txt').write_text('first')
                    out.write_text('ok')
                    return 0, 'ok', False
                return 1, 'failed', False
            self.assertEqual(run(plan, repo, logs, create_repo=True, execute=fails), 1)
            state_path = logs / 'state.json'
            state = json.loads(state_path.read_text())
            state['pending_checkpoint'] = {'task': 'broken'}
            state_path.write_text(json.dumps(state))
            with self.assertRaises(PilotError):
                run(plan, repo, logs, resume=True, execute=fails)
            events = [json.loads(line) for line in (logs / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[-1]['event'], 'stopped')
            self.assertEqual(events[-1]['metadata']['reason'], 'resume reconciliation failed')
