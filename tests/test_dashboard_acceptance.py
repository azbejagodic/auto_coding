"""Integrated desktop/CLI journeys with real Git and durable authorization.

Only the child process and model executors are fake. Audit validation, launch
reservations, CLI parsing, reviews, checkpoints, recovery and final verification
are production code. The fake A runs real focused tests; B only reads code.
"""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import event_log, plan_audit, run_lifecycle, simple_run as runner
from auto_coding.desktop_app import Bridge, HTML
from auto_coding.desktop_control import DesktopController


class Clock(datetime.datetime):
    elapsed = 0

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=cls.elapsed)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class Child:
    """Deferred Popen: the test explicitly executes its exact CLI argv/env."""
    pid = 424242

    def __init__(self, argv, **options):
        self.argv, self.options, self.code = argv, options, None
        assert options['shell'] is False
        assert options['stdin'] == subprocess.DEVNULL

    def poll(self):
        return self.code


class DashboardAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'target'
        self.repo.mkdir()
        self.git('init', '-b', 'feature/acceptance')
        self.git('config', 'user.name', 'Acceptance fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.source = self.repo / 'app.py'
        self.source.write_text('# Three dependent steps\n')
        (self.repo / 'test_app.py').write_text(
            'import unittest\nimport app\nclass AppTests(unittest.TestCase):\n'
            '    def test_one(self): self.assertEqual(app.one(), 1)\n'
            '    def test_two(self): self.assertEqual(app.two(), 2)\n'
            '    def test_three(self): self.assertEqual(app.three(), 3)\n')
        self.git('add', '-A')
        self.git('commit', '-m', 'Seed disposable acceptance fixture')
        self.initial = self.git('rev-parse', 'HEAD')
        self.ids = ['one', 'two', 'three']
        self.parts = ['def one(): return 1\n', 'def two(): return one() + 1\n',
                      'def three(): return two() + 1\n']
        self.focused = [[sys.executable, '-B', '-m', 'unittest', 'test_app.AppTests.test_' + key]
                        for key in self.ids]
        self.final = [sys.executable, '-B', '-m', 'unittest', 'test_app']
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'tasks': [
            {'id': key, 'prompt': 'Implement ' + key + ' using the preceding step; run its focused test',
             'test_commands': [self.focused[i]]} for i, key in enumerate(self.ids)],
            'final_verification': {'test_commands': [self.final], 'minimum_tests': 3, 'timeout_seconds': 30}}))
        self.audit_dir = self.root / 'cli-audit'
        self.run_logs = self.root / 'cli-run'
        self.controller = DesktopController(self.root / 'runs')
        self.dialogs = Mock()
        self.dialogs.confirm.return_value = True
        self.bridge = Bridge(controller=self.controller, dialogs=self.dialogs,
                             controller_factory=lambda: DesktopController(self.root / 'runs'))
        self.assertTrue(self.bridge.select_repo(str(self.repo))['ok'])
        self.assertTrue(self.bridge.select_plan(str(self.plan))['ok'])
        self.calls, self.audits, self.children = [], [], []
        self.decision, self.reject_once, self.stop_on_a = 'APPROVE', False, False
        Clock.elapsed = 0
        self.enterContext(patch.object(event_log.datetime, 'datetime', Clock))
        # Fail closed if any test accidentally reaches the real model launcher.
        self.real_call = runner.call
        def git_only(argv, *args, **kwargs):
            self.assertEqual(argv[0], 'git', 'Real model execution forbidden')
            return self.real_call(argv, *args, **kwargs)
        self.enterContext(patch.object(runner, 'call', side_effect=git_only))
        self.enterContext(patch('auto_coding.desktop_control.check_audit_model'))
        self.spawn = self.enterContext(patch('auto_coding.desktop_control.Popen', side_effect=self.spawn_child))
        locate = shutil.which
        self.enterContext(patch.object(runner.shutil, 'which', side_effect=lambda name, *a, **kw:
            'fake-codex' if name == 'codex' else locate(name, *a, **kw)))
        original_audit, original_run = runner.audit, runner.run
        self.enterContext(patch.object(runner, 'audit', side_effect=lambda *a, **kw:
            original_audit(*a, execute=self.auditor, **kw)))
        self.enterContext(patch.object(runner, 'run', side_effect=lambda *a, **kw:
            original_run(*a, execute=self.execute, **kw)))

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, capture_output=True,
                              text=True, check=True, timeout=15).stdout.strip()

    def spawn_child(self, argv, **options):
        child = Child(argv, **options)
        self.children.append(child)
        return child

    def cli(self, argv, env=None):
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return runner.main(argv)

    def finish_child(self):
        child = self.children[-1]
        child.code = self.cli(child.argv[4:], child.options['env'])
        self.bridge._controller.poll()
        return child.code

    def cli_audit(self):
        return self.cli(['audit', '--repo', str(self.repo), '--logs', str(self.audit_dir),
                         '--audit-model', 'fixture-auditor', str(self.plan)])

    def cli_start(self, resume=False):
        return self.cli(['start', '--repo', str(self.repo), '--logs', str(self.run_logs),
                         *(['--resume'] if resume else ['--approved-audit', str(self.audit_dir)]), str(self.plan)])

    def auditor(self, argv, cwd, timeout, *, prompt):
        self.audits.append(argv)
        self.assertEqual(cwd, self.repo)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertTrue(any('extends=":read-only"' in value for value in argv))
        report = {'decision': self.decision, 'summary': 'Reviewed all three dependent steps', 'overall_risk': 'low',
                  'tasks': [{'task_id': key, 'findings': ['Dependency and focused check specified'],
                             'recommended_changes': [], 'blocking_issues': []} for key in self.ids],
                  'recommended_changes': ['Clarify requirements'] if self.decision == 'REVISE' else [],
                  'blocking_issues': ['Unresolved requirement'] if self.decision == 'BLOCK' else []}
        Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(report))
        return runner._CallResult(0, 'Auditor response', False, True, True)

    def execute(self, argv, cwd, timeout, *, prompt=None):
        self.assertEqual(cwd, self.repo)
        state = plan_audit.read_json(self.run_logs / 'state.json')
        if argv == self.final:
            self.assertEqual(state['completed'], self.ids)
            self.assertEqual(self.git('status', '--porcelain'), '')
            self.calls.append('final')
            Clock.elapsed += 10
            return self.real_call(argv, cwd, timeout)
        # No focused command is dispatched by the controller. Fake A owns it.
        self.assertIn('--output-last-message', argv)
        answer = Path(argv[argv.index('--output-last-message') + 1])
        index = len(state['completed'])
        self.assertIn(self.ids[index], prompt)
        if '--output-schema' in argv:
            self.calls.append('B')
            self.assertIn('Do not execute tests, linting, builds or verification commands', prompt)
            self.assertTrue(any('extends=":read-only"' in value for value in argv))
            rejected = self.reject_once
            self.reject_once = False
            verdict = {'decision': 'REJECTED' if rejected else 'APPROVED',
                       'issues': ['Correct one() to return 1'] if rejected else [],
                       'summary': 'Incorrect value' if rejected else 'Implementation meets requirements',
                       'verification': {'status': 'not_run', 'details': 'Code review only'}}
            answer.write_text(json.dumps(verdict))
        else:
            self.calls.append('A')
            self.assertEqual(state['completed'], self.ids[:index])
            self.assertIn(json.dumps([self.focused[index]]), prompt)
            if len(self.calls) > 1 and self.calls[-2] == 'B' and index == 0:
                self.assertIn('Correct one()', prompt)
            # Build from the real approved predecessors, including correction.
            content = '# Three dependent steps\n' + ''.join(self.parts[:index + 1])
            if self.reject_once:
                content = content.replace('return 1', 'return 0')
            self.source.write_text(content)
            result = self.real_call(self.focused[index], cwd, timeout)
            self.assertEqual(result[0], 1 if self.reject_once else 0, result[1])
            answer.write_text('Executed ' + json.dumps(self.focused[index]) + '\n' + result[1])
            if self.stop_on_a:
                self.stop_on_a = False
                identity = self.bridge.job_status()['stop_identity']
                self.assertTrue(self.bridge.stop_after_task(identity)['ok'])
                self.assertTrue(self.bridge.stop_after_task(identity)['ok'])
        Clock.elapsed += 5
        return runner._CallResult(0, 'Fake agent finished', False, True, True)

    def gui_audit(self):
        self.assertTrue(self.bridge.job_status()['audit_ready'])
        self.assertTrue(self.bridge.start_audit('fixture-auditor')['ok'])
        self.assertFalse(self.bridge.start_audit('fixture-auditor')['ok'])
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.finish_child(), 0 if self.decision == 'APPROVE' else 1)
        self.audit_dir = Path(self.bridge.job_status()['audit']['directory'])
        self.assertEqual(len(self.audits), 1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def completed(self):
        data = self.bridge.snapshot()
        self.assertEqual(data['state']['status'], 'completed')
        self.assertEqual(data['state']['completed'], self.ids)
        self.assertEqual(data['task_count'], 3)
        self.assertEqual(data['summary']['current'], 3)
        self.assertTrue(data['summary']['completion_confirmed'])
        self.assertEqual(data['summary']['duration_seconds'], Clock.elapsed)
        state = data['state']
        self.assertEqual(state['final_verification_result']['test_count'], 3)
        self.assertEqual(state['final_verification_result']['head'], self.git('rev-parse', 'HEAD'))
        self.assertTrue(runner.final_evidence_valid(self.repo, self.run_logs, state))
        approved = [item for item in state['history'] if item.get('commit')]
        self.assertEqual([item['task'] for item in approved], self.ids)
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '3')
        self.assertTrue(all(item['tests'] == [] for item in state['history']))
        events = data['events']
        self.assertLess(max(e['seq'] for e in events if e['event'] == 'checkpoint_committed'),
                        next(e['seq'] for e in events if e['event'] == 'final_verification_started'))
        return data

    def test_selected_json_audit_once_native_start_three_checkpoints_completed(self):
        idle = self.bridge.snapshot()
        self.assertFalse(idle['controller']['start_ready'])
        self.gui_audit()
        approved = self.bridge.snapshot()
        self.assertEqual(approved['controller']['audit']['status'], 'APPROVE')
        self.assertTrue(approved['controller']['audit']['verified'])
        self.assertTrue(approved['controller']['start_ready'])
        self.dialogs.confirm.return_value = False
        self.assertTrue(self.bridge.start()['cancelled'])
        self.assertEqual(self.spawn.call_count, 1)
        self.dialogs.confirm.return_value = True
        result = self.bridge.start()
        self.assertTrue(result['ok'], result)
        self.assertIn('Approved audit: ' + str(self.audit_dir), self.dialogs.confirm.call_args.args[0])
        self.run_logs = self.controller.logs
        self.assertFalse(self.bridge.start()['ok'])
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.finish_child(), 0)
        done = self.completed()
        self.assertEqual(self.calls, ['A', 'B'] * 3 + ['final'])
        self.render_snapshots([idle, approved, done])
        before = {'calls': list(self.calls), 'head': self.git('rev-parse', 'HEAD')}
        self.assertTrue(self.bridge.open_run(str(self.run_logs))['ok'])
        self.completed()
        self.assertFalse(self.bridge.job_status()['resume_ready'])
        self.assertEqual(self.cli_start(resume=True), 0)
        self.assertEqual(self.calls, before['calls'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), before['head'])
        self.assertEqual(len(self.audits), 1)

    def render_snapshots(self, snapshots):
        node = shutil.which('node')
        if node is None:
            return  # Existing mandatory UI tests report the precise blocker.
        fixture = self.root / 'ui.json'
        fixture.write_text(json.dumps({'script': HTML.split('<script>')[1].split('</script>')[0],
                                      'snapshots': snapshots}), encoding='utf-8')
        from test_desktop_ui import RUN_COMMONJS
        result = subprocess.run([node, '-e', RUN_COMMONJS,
            str(Path(__file__).with_name('dashboard_acceptance_ui.cjs')), str(fixture)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_audit_gui_start_same_evidence(self):
        self.assertEqual(self.cli_audit(), 0)
        evidence = (self.audit_dir / 'audit-evidence.json').read_bytes()
        self.assertTrue(self.bridge.open_run(str(self.audit_dir))['ok'])
        self.assertTrue(self.bridge.select_plan(str(self.plan))['ok'])
        self.assertTrue(self.bridge.job_status()['start_ready'])
        self.assertTrue(self.bridge.start()['ok'])
        self.assertEqual(self.children[-1].argv[self.children[-1].argv.index('--approved-audit') + 1], str(self.audit_dir))
        self.run_logs = self.bridge._controller.logs
        self.assertEqual(self.finish_child(), 0)
        self.completed()
        self.assertEqual((self.audit_dir / 'audit-evidence.json').read_bytes(), evidence)
        self.assertEqual(len(self.audits), 1)

    def test_gui_audit_cli_start_reopen_exact_status_tasks_logs_and_exports(self):
        self.gui_audit()
        self.assertEqual(self.cli_start(), 0)
        self.assertFalse((self.run_logs / 'desktop-run.json').exists())
        self.assertTrue(self.bridge.open_run(str(self.run_logs))['ok'])
        data = self.completed()
        # Reader annotations describe display order and evidence availability;
        # the underlying durable event fields must remain byte-for-byte values.
        self.assertEqual([{k: v for k, v in event.items() if not k.startswith('_')}
                          for event in data['events']],
                         [json.loads(line) for line in (self.run_logs / 'events.jsonl').read_text().splitlines()])
        self.assertFalse(data['controller']['active'])
        output = data['state']['final_verification_result']['tests'][0]['output']
        self.assertEqual(self.bridge.evidence(output)['text'], (self.run_logs / output).read_text())
        for method, name in ((self.bridge.download_audit, 'audit-export.json'), (self.bridge.download_logs, 'logs-export.json')):
            destination = self.root / name
            self.dialogs.save.return_value = str(destination)
            self.assertTrue(method()['ok'])
            payload = json.loads(destination.read_text())
            if name.startswith('audit'):
                self.assertEqual(payload['report'], plan_audit.read_json(self.audit_dir / 'audit-report.json'))
            else:
                self.assertEqual(payload['state']['completed'], self.ids)
                self.assertEqual(payload['files'][output]['text'], (self.run_logs / output).read_text())
            self.assertFalse(method()['ok'], 'Existing exports must not be overwritten')
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.calls, ['A', 'B'] * 3 + ['final'])

    def test_b_rejection_correction_and_stop_handoff_resume_without_replay(self):
        self.gui_audit()
        self.assertTrue(self.bridge.start()['ok'])
        self.run_logs = self.controller.logs
        self.reject_once, self.stop_on_a = True, True
        self.assertEqual(self.finish_child(), 1)
        stopped = self.bridge.snapshot()
        self.assertEqual(stopped['state']['status'], 'stopped')
        self.assertEqual(stopped['state']['completed'], ['one'])
        self.assertEqual(self.calls, ['A', 'B', 'A', 'B'])
        self.assertEqual([h['review']['decision'] for h in stopped['state']['history']], ['REJECTED', 'APPROVED'])
        checkpoint = self.git('rev-parse', 'HEAD')
        self.assertTrue(self.bridge.open_run(str(self.run_logs))['ok'])
        self.controller = self.bridge._controller
        self.assertTrue(self.bridge.job_status()['resume_ready'])
        self.assertTrue(self.bridge.resume()['ok'])
        self.assertFalse(self.bridge.resume()['ok'])
        self.assertEqual(self.finish_child(), 0)
        done = self.completed()
        self.assertEqual(done['state']['history'][1]['commit'], checkpoint)
        self.assertEqual(self.calls, ['A', 'B', 'A', 'B', 'A', 'B', 'A', 'B', 'final'])
        self.assertEqual(self.cli_start(resume=True), 0)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(len(self.audits), 1)

    def test_missing_changed_and_rejected_approval_blocks_both_entry_points(self):
        for change in ('missing', 'REJECT', 'REVISE', 'BLOCK', 'plan', 'head', 'branch', 'report', 'ledger'):
            with self.subTest(change=change):
                self.audit_dir = self.root / ('audit-' + change)
                if change != 'missing':
                    self.decision = change if change in ('REJECT', 'REVISE', 'BLOCK') else 'APPROVE'
                    self.assertEqual(self.cli_audit(), 1 if self.decision != 'APPROVE' else 0)
                if change == 'plan':
                    self.plan.write_text(self.plan.read_text() + '\n')
                elif change == 'head':
                    self.git('commit', '--allow-empty', '-m', 'Unrelated fixture change')
                elif change == 'branch':
                    self.git('checkout', '-b', 'feature/changed')
                elif change == 'report':
                    report = plan_audit.read_json(self.audit_dir / 'audit-report.json')
                    report['summary'] = 'Altered report'
                    runner.save_json(self.audit_dir / 'audit-report.json', report)
                elif change == 'ledger':
                    evidence = plan_audit.read_json(self.audit_dir / 'audit-evidence.json')
                    plan_audit.ledger_path(self.repo, evidence['identity']).unlink()
                self.assertFalse(self.bridge.job_status()['start_ready'])
                self.assertFalse(self.bridge.start()['ok'])
                self.run_logs = self.root / ('blocked-' + change)
                self.assertEqual(self.cli_start(), 1)
                self.assertEqual(self.calls, [])
                # A revised exact-byte identity for each subsequent audit.
                self.plan.write_text(self.plan.read_text() + '\n')
        self.spawn.assert_not_called()

    def test_approval_arrives_during_active_run_is_revalidated_without_second_audit(self):
        self.assertTrue(self.bridge.start_audit('fixture-auditor')['ok'])
        child = self.children[-1]
        # The audit writes its real evidence while the parent still sees a live
        # child. Approval must be retained even while Run Start is unavailable.
        code = self.cli(child.argv[4:], child.options['env'])
        self.assertEqual(code, 0)
        busy = self.bridge.job_status()
        self.assertTrue(busy['active'])
        self.assertEqual(busy['audit']['status'], 'APPROVE')
        self.assertTrue(busy['audit']['verified'])
        self.assertFalse(busy['start_ready'])
        evidence = Path(busy['audit']['directory']) / 'audit-evidence.json'
        before = evidence.read_bytes()
        child.code = code
        self.controller.poll()
        # Another run remains active as the audit's child finishes. Use real
        # launch metadata and a real live PID, not a mocked preflight verdict.
        other = self.controller.runs_dir / 'run-other'
        other.mkdir()
        run_lifecycle.write_launch_metadata(other, repo=self.repo, plan=self.plan,
                                            pid=os.getpid(), status='running')
        busy = self.bridge.job_status()
        self.assertEqual(busy['audit']['status'], 'APPROVE')
        self.assertTrue(busy['audit']['verified'])
        self.assertFalse(busy['start_ready'])
        self.assertFalse(self.bridge.start()['ok'])
        self.assertEqual(self.spawn.call_count, 1)
        run_lifecycle.write_launch_metadata(other, repo=self.repo, plan=self.plan, status='stopped')
        self.assertTrue(self.bridge.job_status()['start_ready'])
        self.assertFalse(self.bridge.job_status()['audit_ready'])
        self.assertEqual(evidence.read_bytes(), before)
        self.assertTrue(self.bridge.start()['ok'])
        self.run_logs = self.controller.logs
        self.assertEqual(self.finish_child(), 0)
        self.completed()
        self.assertEqual(len(self.audits), 1)

    def test_download_permissions_paths_changed_opened_evidence_and_tampered_audit(self):
        self.assertEqual(self.cli_audit(), 0)
        self.assertEqual(self.cli_start(), 0)
        self.assertTrue(self.bridge.open_run(str(self.run_logs))['ok'])
        data = self.completed()
        output = data['state']['final_verification_result']['tests'][0]['output']
        for path in ('../plan.json', str(self.plan), 'review-scratch/file.txt', '.env'):
            self.assertFalse(self.bridge.evidence(path)['ok'])
        self.assertTrue(self.bridge.evidence(output)['ok'])
        original = (self.run_logs / output).read_bytes()
        (self.run_logs / output).write_bytes(original + b'changed\n')
        self.dialogs.save.return_value = str(self.root / 'changed.json')
        self.assertTrue(self.bridge.download_logs()['ok'])
        exported = plan_audit.read_json(self.root / 'changed.json')
        self.assertFalse(exported['files'][output]['ok'])
        self.assertIn('changed since', exported['files'][output]['text'])
        (self.run_logs / output).write_bytes(original)
        for method in (self.bridge.download_audit, self.bridge.download_logs):
            for destination in (self.repo / 'export.json', self.run_logs / 'export.json', Path('relative.json')):
                self.dialogs.save.return_value = str(destination)
                self.assertFalse(method()['ok'])
                self.assertFalse(destination.exists())
            self.dialogs.save.return_value = str(self.root / 'denied.json')
            original_open = Path.open
            def denied(path, *args, **kwargs):
                if path == self.root / 'denied.json':
                    raise PermissionError('Fixture denied export destination')
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', denied):
                result = method()
                self.assertFalse(result['ok'])
                self.assertIn('denied', result['error'])
        report = self.audit_dir / 'audit-report.json'
        report.write_text(report.read_text().replace('Reviewed', 'Tampered'))
        self.dialogs.save.return_value = str(self.root / 'tampered.json')
        self.assertFalse(self.bridge.download_audit()['ok'])
        self.assertFalse((self.root / 'tampered.json').exists())
        self.assertEqual(self.calls, ['A', 'B'] * 3 + ['final'])


if __name__ == '__main__':
    unittest.main()
