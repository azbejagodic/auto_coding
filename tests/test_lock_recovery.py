import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_coding import github_workspace as workspace
from auto_coding import simple_run as runner
from auto_coding.desktop_control import DesktopController
from auto_coding.event_log import EventLog

COLLISION = "fatal: Unable to create 'C:/project/.git/index.lock': File exists."


class LockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'workspaces' / ('a' * 64)
        self.repo.mkdir(parents=True)
        self.lock = workspace.managed_lock(self.repo)
        self.addCleanup(workspace._lock_records.clear)

    @contextlib.contextmanager
    def retry_clock(self):
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(runner, 'GIT_LOCK_DEADLINE', 1), \
                patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner.time, 'sleep', side_effect=advance) as wait:
            yield wait

    def test_transient_retry_once_with_durable_result(self):
        logs = EventLog(self.root / 'events.jsonl', 'test')
        token = runner._git_events.set(logs)
        self.addCleanup(runner._git_events.reset, token)
        with self.retry_clock() as wait, patch.object(runner, 'call', side_effect=[
                (128, COLLISION, False), (0, 'staged', False)]) as call, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.git(self.repo, ['add', '-A']), 'staged')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0].args[0], call.call_args_list[1].args[0])
        wait.assert_called_once_with(.25)
        events = [json.loads(line) for line in logs.path.read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['index_lock_retry', 'index_lock_finished'])
        self.assertEqual(events[-1]['outcome'], 'success')
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_commit_contention_retries_same_approved_operation(self):
        args = ['-c', 'user.name=Local', 'commit', '-m', 'task', '--no-gpg-sign']
        with self.retry_clock(), patch.object(runner, 'call', side_effect=[
                (128, COLLISION, False), (0, 'committed', False)]) as call:
            self.assertEqual(runner.git(self.repo, args), 'committed')
        self.assertEqual(call.call_args_list[0].args[0], call.call_args_list[1].args[0])

    def test_persistent_lock_preserves_work_and_lock(self):
        project = self.repo / 'work.txt'
        project.write_text('unreviewed work')
        gitdir = self.repo / '.git'
        gitdir.mkdir()
        lock = gitdir / 'index.lock'
        lock.write_text('owned elsewhere')
        with self.retry_clock() as wait, patch.object(runner, 'call', return_value=(128, COLLISION, False)):
            with self.assertRaisesRegex(runner.GitLockError, 'Working tree preserved'):
                runner.git(self.repo, ['add', '-A'])
        self.assertEqual(wait.call_count, 4)
        self.assertEqual(project.read_text(), 'unreviewed work')
        self.assertEqual(lock.read_text(), 'owned elsewhere')

    def test_other_errors_and_timeouts_are_not_retried(self):
        for diagnostic, timed in [('fatal: permission denied', False),
                                  ('index.lock exists but permission denied', False),
                                  (COLLISION, True)]:
            with self.subTest(diagnostic=diagnostic, timed=timed), self.retry_clock() as wait, \
                    patch.object(runner, 'call', return_value=(128, diagnostic, timed)) as call:
                with self.assertRaises(runner.PilotError):
                    runner.git(self.repo, ['add', '-A'])
                self.assertEqual(call.call_count, 1)
                wait.assert_not_called()
        with self.retry_clock() as wait, patch.object(runner, 'call', return_value=(128, COLLISION, False)) as call:
            with self.assertRaises(runner.PilotError):
                runner.git(self.repo, ['push', 'origin', 'feature/test'])
            self.assertEqual(call.call_count, 1)
            wait.assert_not_called()

    def test_competing_operation_cannot_enter_or_release_owner(self):
        failures = []
        with workspace.checkout_lock(self.lock):
            original = (self.lock / 'owner').read_text()
            def contender():
                try:
                    with workspace.checkout_lock(self.lock):
                        failures.append('entered')
                except workspace.WorkspaceError as exc:
                    failures.append(exc.code)
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, ['busy'])
            self.assertEqual((self.lock / 'owner').read_text(), original)
        self.assertFalse(self.lock.exists())

    def test_clean_exception_releases_own_checkout_and_job(self):
        with self.assertRaisesRegex(ValueError, 'safe failure'):
            with workspace.checkout_lock(self.lock):
                raise ValueError('safe failure')
        self.assertFalse(self.lock.exists())
        @workspace.managed_job
        def job(plan, repo, logs):
            raise ValueError('safe validation')
        with self.assertRaises(ValueError):
            job(None, self.repo, None)
        self.assertFalse(workspace.managed_lock(self.repo, '.job.lock').exists())

    def test_replaced_owner_is_never_released(self):
        with workspace.checkout_lock(self.lock):
            (self.lock / 'owner').write_text('another owner')
        self.assertTrue(self.lock.exists())
        self.assertFalse(workspace.diagnose_lock(self.lock)['recoverable'])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)

    def test_active_uncertain_and_legacy_locks_refuse_recovery(self):
        with workspace.checkout_lock(self.lock):
            self.assertIn('Another run is active', workspace.diagnose_lock(self.lock)['message'])
            with self.assertRaises(workspace.WorkspaceError):
                workspace.recover_lock(self.lock)
            workspace.mark_process_uncertain()
        before = {p.name: p.read_bytes() for p in self.lock.iterdir()}
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.lock.iterdir()})
        legacy = self.lock.with_name('legacy.lock')
        legacy.mkdir()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(legacy)
        self.assertTrue(legacy.exists())

    def completed_gate(self):
        original = Path.rmdir
        def fail_cleanup(path):
            if path == self.lock:
                raise PermissionError('transient filesystem error')
            return original(path)
        with patch.object(Path, 'rmdir', fail_cleanup):
            with workspace.checkout_lock(self.lock):
                pass
        self.assertTrue(workspace.diagnose_lock(self.lock)['recoverable'])

    def test_explicit_recovery_archives_proven_completed_gate(self):
        self.completed_gate()
        controller = DesktopController(self.root / 'runs')
        controller.repo = self.repo
        with patch.object(controller, 'job_status', return_value={'active': False, 'uncertain': False}):
            result = controller.recover_workspace_lock('.lock')
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.lock.exists())
        self.assertTrue(Path(result['archive']).is_dir())
        with workspace.checkout_lock(self.lock):
            pass

    def test_recovery_rechecks_uncertainty_and_identity(self):
        self.completed_gate()
        (self.lock / 'uncertain').touch()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)
        (self.lock / 'uncertain').unlink()
        original = self.lock.with_name('original.lock')
        self.lock.rename(original)
        self.lock.mkdir()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)
        self.assertTrue(original.exists())
        self.assertTrue(self.lock.exists())

    def test_recovery_refuses_new_uncertainty_during_inspection(self):
        self.completed_gate()
        original = workspace.diagnose_lock
        calls = []
        def diagnose(path):
            calls.append(path)
            if len(calls) == 2:
                (path / 'uncertain').touch()
            return original(path)
        with patch.object(workspace, 'diagnose_lock', side_effect=diagnose):
            with self.assertRaises(workspace.WorkspaceError):
                workspace.recover_lock(self.lock)
        self.assertTrue((self.lock / 'uncertain').exists())
        self.assertEqual(list(self.lock.parent.glob('*.archive-*')), [])

    def test_missing_in_memory_proof_refuses_recovery(self):
        self.completed_gate()
        workspace._lock_records.clear()
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)
        self.assertTrue(self.lock.exists())

    def test_completed_launcher_is_not_proof_its_reserved_child_finished(self):
        self.completed_gate()
        job = workspace.managed_lock(self.repo, '.job.lock')
        job.mkdir()
        (job / 'reservation').write_text('child may still be waiting for handoff')
        self.assertFalse(workspace.diagnose_lock(self.lock)['recoverable'])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.recover_lock(self.lock)
        self.assertTrue(job.exists())
        self.assertTrue(self.lock.exists())

    def test_desktop_recovery_rejects_active_unknown_and_arbitrary_path(self):
        self.completed_gate()
        controller = DesktopController(self.root / 'runs')
        controller.repo = self.repo
        for status, suffix in [({'active': True, 'uncertain': False}, '.lock'),
                               ({'active': False, 'uncertain': True}, '.lock'),
                               ({'active': False, 'uncertain': False}, '../arbitrary.lock')]:
            with patch.object(controller, 'job_status', return_value=status), \
                    patch('auto_coding.desktop_control.recover_lock') as recover:
                self.assertFalse(controller.recover_workspace_lock(suffix)['ok'])
                recover.assert_not_called()
        self.assertTrue(self.lock.exists())

    def test_failed_owner_persistence_does_not_leave_empty_orphan(self):
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path == self.lock / 'owner':
                raise PermissionError('cannot persist owner')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            with self.assertRaises(PermissionError):
                with workspace.checkout_lock(self.lock):
                    self.fail('operation must not start')
        self.assertFalse(self.lock.exists())

    def test_debug_report_retains_lock_and_timeout_classification(self):
        from auto_coding.debug_export import _summary
        for reason, event in [('git index lock', 'index_lock_finished'), ('test timeout', 'stopped')]:
            summary = _summary('events.jsonl', json.dumps({'actor': 'Git', 'event': event,
                               'outcome': 'failed', 'metadata': {'reason': reason, 'message': 'private path'}}).encode())
            self.assertEqual(summary[0]['results'], {'reason': reason})
            self.assertEqual(summary[0]['event'], event)
        state = {'history': [{'task': 'one', 'attempt': 1, 'interruption': 'git index lock',
                              'feedback': 'private diagnostic', 'review': None, 'tests': []}]}
        summary = _summary('state.json', json.dumps(state).encode())
        self.assertEqual(summary['history'][0]['interruption'], 'git index lock')
        self.assertNotIn('feedback', summary['history'][0])

    def test_persistent_commit_lock_remains_desktop_resumable(self):
        plan = self.root / 'plan.txt'
        logs = self.root / 'logs'
        plan.write_text('Implement file')
        def execute(argv, cwd, timeout, *, prompt=None):
            answer = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                answer.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                (cwd / 'work.txt').write_text('reviewed work')
                answer.write_text('done')
            return 0, 'mocked', False
        subprocess.run(['git', 'init', '-b', 'feature/locks'], cwd=self.repo, check=True, capture_output=True)
        original = runner.call
        def call(argv, cwd, timeout, **kwargs):
            self.assertNotIn('push', argv)
            return (128, COLLISION, False) if 'commit' in argv else original(argv, cwd, timeout, **kwargs)
        with self.retry_clock(), patch.object(runner, 'call', side_effect=call):
            self.assertEqual(runner.run(plan, self.repo, logs, execute=execute), 1)
        state = json.loads((logs / 'state.json').read_text())
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['completed'], [])
        self.assertIsNotNone(state['pending_checkpoint'])
        diagnostic = 'Git index.lock still owned by editor; preserve the working tree'
        with patch.object(runner, 'local_commit', side_effect=runner.GitLockError(diagnostic)):
            with self.assertRaises(runner.GitLockError):
                runner.run(plan, self.repo, logs, resume=True,
                           execute=lambda *a, **kw: self.fail('pending approval needs no new process'))
        events = [json.loads(line) for line in (logs / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'stopped')
        self.assertEqual(events[-1]['metadata']['reason'], 'git index lock')
        self.assertEqual(events[-1]['metadata']['message'], diagnostic)
        state = json.loads((logs / 'state.json').read_text())
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['completed'], [])
        self.assertIsNotNone(state['pending_checkpoint'])
        self.assertEqual(runner.run(plan, self.repo, logs, resume=True,
                                   execute=lambda *a, **kw: self.fail('reviewed bytes need no new Codex process')), 0)
        state = json.loads((logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['task-1'])
        self.assertIsNone(state['pending_checkpoint'])

    def test_reservation_claim_is_once_and_cancellation_cannot_release_job(self):
        with workspace.checkout_lock(self.lock):
            token = workspace.reserve_job(self.repo)
        @workspace.managed_job
        def child(plan, repo, logs):
            workspace.cancel_reservation(repo, token)
            self.assertTrue(workspace.managed_lock(repo, '.job.lock').exists())
            with self.assertRaises(workspace.WorkspaceError):
                child(plan, repo, logs)
        with patch.dict(workspace.os.environ, {'AUTOCODING_JOB_RESERVATION': token}):
            child(None, self.repo, None)
        self.assertFalse(workspace.managed_lock(self.repo, '.job.lock').exists())

    def test_stopped_run_resumes_with_review_and_keeps_completed_commit(self):
        plan = self.root / 'plan.json'
        logs = self.root / 'logs'
        plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'First'}, {'id': 'two', 'prompt': 'Second'}]}))
        reviews = []
        def execute(argv, cwd, timeout, *, prompt=None):
            answer = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                reviews.append(prompt)
                answer.write_text(json.dumps({'decision': 'APPROVED', 'issues': [], 'summary': 'OK'}))
            else:
                name = 'one' if 'First' in prompt else 'two'
                (cwd / (name + '.txt')).write_text(name)
                answer.write_text('done')
            return 0, 'mocked', False
        subprocess.run(['git', 'init', '-b', 'feature/locks'], cwd=self.repo, check=True, capture_output=True)
        original_call = runner.call
        def call(argv, cwd, timeout, **kwargs):
            self.assertNotIn('push', argv)
            if argv[:3] == ['git', 'add', '-A'] and (self.repo / 'two.txt').exists():
                return 128, COLLISION, False
            return original_call(argv, cwd, timeout, **kwargs)
        with self.retry_clock(), patch.object(runner, 'call', side_effect=call):
            self.assertEqual(runner.run(plan, self.repo, logs, attempts=1, execute=execute), 1)
            # Repeated interruptions on the last available attempt must remain
            # resumable without overwriting the original implementation logs.
            original_evidence = (logs / 'two-01-a-prompt.txt').read_bytes()
            self.assertEqual(runner.run(plan, self.repo, logs, resume=True, attempts=1, execute=execute), 1)
            self.assertEqual((logs / 'two-01-a-prompt.txt').read_bytes(), original_evidence)
            self.assertTrue((logs / 'two-02-a-prompt.txt').exists())
        state = json.loads((logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['one'])
        self.assertIsNone(state['history'][-1]['review'])
        self.assertEqual(state['history'][-1]['interruption'], 'git index lock')
        self.assertEqual(len(reviews), 1)
        sha = state['history'][0]['commit']
        events = [json.loads(line) for line in (logs / 'events.jsonl').read_text().splitlines()]
        self.assertFalse(any(e['event'] == 'correction_requested' for e in events))
        self.assertEqual(events[-1]['metadata']['reason'], 'git index lock')
        # Only a witnessed clean completion is recoverable. Resume still reviews task two.
        self.completed_gate()
        workspace.recover_lock(self.lock)
        self.assertEqual(runner.run(plan, self.repo, logs, resume=True, attempts=1, execute=execute), 0)
        state = json.loads((logs / 'state.json').read_text())
        self.assertEqual(state['completed'], ['one', 'two'])
        self.assertEqual(state['history'][0]['commit'], sha)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(subprocess.check_output(['git', 'rev-parse', 'HEAD^'], cwd=self.repo, text=True).strip(), sha)


if __name__ == '__main__':
    unittest.main()
