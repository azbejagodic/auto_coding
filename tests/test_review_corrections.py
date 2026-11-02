"""B03a regression cases use dummy files and fake Codex executors only."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from backend_fixtures import run_with_verification as verified_run
from auto_coding.launch_preflight import (PROTECTED_BRANCH_KEY, configured_protected_branches,
    launch_preflight, validate_feature_branch)
from auto_coding.sensitive_paths import changed_paths
from auto_coding.repository_ownership import execution_lock
from test_sensitive_paths import FakeExecutor, DUMMY


class ChangeMetadataTests(unittest.TestCase):
    def test_both_rename_and_copy_paths_preserve_unusual_filenames(self):
        output = 'R100\0.env.production\0 settings\nfile.txt\0C075\0.aws\\credentials\0copy "name".txt\0M\0ordinary.py\0'
        self.assertEqual(changed_paths(output), ['.env.production', ' settings\nfile.txt',
            '.aws\\credentials', 'copy "name".txt', 'ordinary.py'])

    def test_incomplete_unknown_or_unmerged_metadata_fails_closed(self):
        for output in ('A\0missing-terminator', 'R100\0only-source\0', 'C100\0\0target\0',
                       'R999\0source\0target\0', 'U\0unmerged\0', 'warning text\0file\0'):
            with self.subTest(output=repr(output)), self.assertRaises(ValueError):
                changed_paths(output)
        self.assertEqual(changed_paths(''), [])


class ReviewCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'feature/test')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'seed.txt').write_text('ordinary seed')
        self.git('add', '-A')
        self.git('commit', '-m', 'initial')
        self.logs = self.root / 'logs'
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'Implement ONE',
                                                   'test_commands': [['fake-test']]}]}))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True,
                              capture_output=True, text=True, timeout=15).stdout.strip()

    def run_fake(self, fake, **options):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return verified_run(self.plan, self.repo, self.logs, execute=fake, **options)

    def state(self):
        return json.loads((self.logs / 'state.json').read_text())

    def seed_file(self, name):
        file = self.repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(DUMMY)
        self.git('add', '-A')
        self.git('commit', '-m', 'dummy source fixture')

    def change_executor(self, source, destination, *, copy=False):
        fake = FakeExecutor(destination)
        original = fake.__call__
        def execute(argv, cwd, timeout, *, prompt=None):
            if argv[0] != 'fake-test' and '--output-schema' not in argv:
                fake.calls.append('A')
                target = cwd / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                if copy:
                    shutil.copyfile(cwd / source, target)
                else:
                    (cwd / source).rename(target)
                Path(argv[argv.index('--output-last-message') + 1]).write_text('done')
                return runner._CallResult(0, 'finished', False, True, True)
            return original(argv, cwd, timeout, prompt=prompt)
        return fake, execute

    def assert_sensitive_change_stops(self, source, destination, *, copy=False):
        self.seed_file(source)
        self.git('config', 'diff.renames', 'copies')
        before = self.git('rev-parse', 'HEAD')
        fake, execute = self.change_executor(source, destination, copy=copy)
        self.assertEqual(self.run_fake(execute), 1)
        self.assertEqual(fake.calls, ['A'])
        self.assertEqual(fake.b_prompts, [])
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / destination).read_text(), DUMMY)
        self.assertEqual((self.repo / source).exists(), copy)
        self.assertNotIn(DUMMY, self.state()['history'][-1]['feedback'])
        self.assertEqual(self.state()['completed'], [])

    def test_env_rename_to_ordinary_path_is_rejected_before_tests_b_and_commit(self):
        self.assert_sensitive_change_stops('.env.production', 'settings.txt')
        self.assertIn('.env.production', self.state()['history'][-1]['feedback'])

    def test_aws_rename_to_ordinary_path_is_rejected_before_tests_b_and_commit(self):
        self.assert_sensitive_change_stops('.aws/credentials', 'settings.txt')
        self.assertIn('.aws/credentials', self.state()['history'][-1]['feedback'])

    def test_ordinary_source_renamed_to_sensitive_destination_is_rejected(self):
        self.assert_sensitive_change_stops('ordinary.txt', '.env.production')

    def test_unchanged_sensitive_copy_source_is_rejected(self):
        self.assert_sensitive_change_stops('.env.production', 'settings.txt', copy=True)

    def test_copy_into_sensitive_destination_is_rejected(self):
        self.assert_sensitive_change_stops('ordinary.txt', '.aws/credentials', copy=True)

    def test_ordinary_rename_remains_approvable_with_exact_patch_identity(self):
        self.seed_file('ordinary source.txt')
        self.git('config', 'diff.renames', 'true')
        fake, execute = self.change_executor('ordinary source.txt', 'ordinary destination.txt')
        self.assertEqual(self.run_fake(execute), 0)
        self.assertEqual(fake.calls, ['A', 'test', 'B'])
        self.assertEqual(self.state()['completed'], ['one'])
        runner.verify_approved_commit(self.repo, self.state()['history'][-1], self.git('rev-parse', 'HEAD'))

    def test_ordinary_copy_remains_approvable(self):
        self.seed_file('ordinary.txt')
        fake, execute = self.change_executor('ordinary.txt', 'ordinary copy.txt', copy=True)
        self.assertEqual(self.run_fake(execute), 0)
        self.assertEqual(fake.calls, ['A', 'test', 'B'])
        self.assertTrue((self.repo / 'ordinary.txt').is_file())

    def test_direct_commit_rejects_rename_source_without_discarding_the_index(self):
        self.seed_file('.env.production')
        (self.repo / '.env.production').rename(self.repo / 'settings.txt')
        self.git('add', '-A')
        before, index = self.git('rev-parse', 'HEAD'), (self.repo / '.git/index').read_bytes()
        with self.assertRaisesRegex(runner.PilotError, 'Sensitive file'):
            runner.local_commit(self.repo, 'one')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual((self.repo / 'settings.txt').read_text(), DUMMY)

    def test_direct_commit_rejects_copy_source_even_when_git_rename_detection_is_disabled(self):
        self.seed_file('.env.production')
        self.git('config', 'diff.renames', 'false')
        self.git('config', 'diff.renameLimit', '1')
        shutil.copyfile(self.repo / '.env.production', self.repo / 'settings.txt')
        self.git('add', '-A')
        before, index = self.git('rev-parse', 'HEAD'), (self.repo / '.git/index').read_bytes()
        with self.assertRaisesRegex(runner.PilotError, 'Sensitive file'):
            runner.local_commit(self.repo, 'one')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual((self.repo / '.env.production').read_bytes(), (self.repo / 'settings.txt').read_bytes())

    def test_old_review_ready_rename_cannot_resume_into_b(self):
        self.seed_file('.env.production')
        fake, execute = self.change_executor('.env.production', 'settings.txt')
        with patch.object(runner, 'reject_sensitive_paths'), patch.object(runner, 'review_ready_task', return_value='stopped'):
            self.assertEqual(self.run_fake(execute), 1)
        before = self.git('rev-parse', 'HEAD')
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, 'Sensitive file'):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / 'settings.txt').read_text(), DUMMY)

    def test_old_approved_copy_cannot_resume_into_a_commit(self):
        self.seed_file('.aws/credentials')
        fake, execute = self.change_executor('.aws/credentials', 'settings.txt', copy=True)
        with patch.object(runner, 'reject_sensitive_paths'), \
                patch.object(runner, 'local_commit', side_effect=runner.PilotError('before commit')):
            self.assertEqual(self.run_fake(execute), 1)
        before = self.git('rev-parse', 'HEAD')
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, 'Sensitive file'):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo / '.aws/credentials').read_text(), DUMMY)

    def protect(self, name='backend/core'):
        self.git('config', '--local', '--add', PROTECTED_BRANCH_KEY, name)

    def test_configured_stable_branch_blocks_direct_cli_before_a(self):
        self.git('switch', '-c', 'backend/core')
        self.protect()
        before = self.git('rev-parse', 'HEAD')
        errors = io.StringIO()
        with patch.object(runner.shutil, 'which', return_value='fake-codex'), \
                patch.object(runner, 'codex_argv', side_effect=AssertionError('No Codex invocation')) as codex, \
                contextlib.redirect_stderr(errors):
            self.assertEqual(runner.main([str(self.plan), '--repo', str(self.repo), '--logs', str(self.logs)]), 1)
        codex.assert_not_called()
        self.assertIn('backend/core', errors.getvalue())
        self.assertIn(PROTECTED_BRANCH_KEY, errors.getvalue())
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_backend_core_is_not_universally_protected(self):
        self.git('switch', '-c', 'backend/core')
        self.assertEqual(configured_protected_branches(self.repo), ())
        self.assertEqual(self.run_fake(FakeExecutor('app.py')), 0)

    def test_additive_configuration_preserves_fix_and_feature_branches_and_defaults(self):
        self.protect()
        self.protect('release/stable')
        configured = configured_protected_branches(self.repo)
        for name in ('main', 'master', 'backend/core', 'release/stable', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_feature_branch(name, protected_branches=configured)
        for name in ('feature/one', 'fix/one'):
            self.assertEqual(validate_feature_branch(name, protected_branches=configured), name)

    def test_preflight_reports_configured_protection_without_mutation(self):
        self.git('switch', '-c', 'backend/core')
        self.protect()
        before = (self.repo / '.git/HEAD').read_bytes()
        report = launch_preflight(self.repo, self.root / 'runs')
        self.assertTrue(report['clean'])
        self.assertIn('protected', ' '.join(report['errors']))
        self.assertEqual((self.repo / '.git/HEAD').read_bytes(), before)

    def test_protection_added_during_a_blocks_tests_and_b(self):
        fake = FakeExecutor('app.py')
        def execute(argv, *args, **kwargs):
            result = fake(argv, *args, **kwargs)
            if fake.calls == ['A']:
                self.protect('feature/test')
            return result
        self.assertEqual(self.run_fake(execute), 1)
        self.assertEqual(fake.calls, ['A'])

    def test_protection_added_during_b_blocks_approval_and_commit(self):
        fake = FakeExecutor('app.py')
        before = self.git('rev-parse', 'HEAD')
        def execute(argv, *args, **kwargs):
            result = fake(argv, *args, **kwargs)
            if '--output-schema' in argv:
                self.protect('feature/test')
            return result
        with self.assertRaisesRegex(runner.PilotError, 'protected'):
            self.run_fake(execute)
        self.assertEqual(fake.calls, ['A', 'test', 'B'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertIsNone(self.state()['pending_checkpoint'])

    def test_protection_added_during_testing_blocks_the_next_test(self):
        self.plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'Implement ONE',
            'test_commands': [['fake-test'], ['fake-test']]}]}))
        fake = FakeExecutor('app.py')
        def execute(argv, *args, **kwargs):
            result = fake(argv, *args, **kwargs)
            if argv[0] == 'fake-test':
                self.protect('feature/test')
            return result
        with self.assertRaisesRegex(runner.PilotError, 'protected'):
            self.run_fake(execute)
        self.assertEqual(fake.calls, ['A', 'test'])

    def test_direct_local_commit_enforces_configured_protection(self):
        self.git('switch', '-c', 'backend/core')
        self.protect()
        (self.repo / 'ordinary.py').write_text('ordinary implementation')
        self.git('add', '-A')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(runner.PilotError, 'protected'):
            runner.local_commit(self.repo, 'one')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_configured_protection_blocks_review_ready_resume(self):
        self.git('switch', '-c', 'backend/core')
        with patch.object(runner, 'review_ready_task', return_value='stopped'):
            self.assertEqual(self.run_fake(FakeExecutor('app.py')), 1)
        self.protect()
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, 'protected'):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()

    def test_configured_protection_blocks_approved_commit_recovery(self):
        self.git('switch', '-c', 'backend/core')
        with patch.object(runner, 'local_commit', side_effect=runner.PilotError('before commit')):
            self.assertEqual(self.run_fake(FakeExecutor('app.py')), 1)
        self.protect()
        before = self.git('rev-parse', 'HEAD')
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, 'protected'):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_unreadable_and_invalid_policy_never_allow_execution(self):
        original = runner.bounded_run
        for response in (subprocess.CompletedProcess([], 128, '', 'dummy error'),
                         subprocess.CompletedProcess([], 0, 'backend/core', ''),
                         subprocess.CompletedProcess([], 0, ' backend/core\0', ''),
                         subprocess.CompletedProcess([], 0, 'backend/*\0', '')):
            def command(argv, **kwargs):
                return response if PROTECTED_BRANCH_KEY in argv else original(argv, **kwargs)
            executor = Mock()
            with self.subTest(response=response.returncode), patch.object(runner, 'bounded_run', side_effect=command):
                with self.assertRaises(runner.PilotError):
                    self.run_fake(executor)
            executor.assert_not_called()

    def test_uncertain_policy_query_timeout_retains_execution_ownership(self):
        original = runner.bounded_run
        error = subprocess.TimeoutExpired(['git', 'config'], 10)
        error.cleanup_confirmed = False
        def command(argv, **kwargs):
            if PROTECTED_BRANCH_KEY in argv:
                raise error
            return original(argv, **kwargs)
        executor = Mock()
        with patch.object(runner, 'bounded_run', side_effect=command):
            with self.assertRaisesRegex(runner.PilotError, 'branch protection'):
                self.run_fake(executor)
        executor.assert_not_called()
        self.assertTrue(execution_lock(self.repo, '.job.lock').is_dir())
        self.assertTrue(execution_lock(self.repo, '.process-uncertain.json').is_file())

    def test_confirmed_policy_query_timeout_does_not_leave_false_uncertainty(self):
        original = runner.bounded_run
        error = subprocess.TimeoutExpired(['git', 'config'], 10)
        error.cleanup_confirmed = True
        def command(argv, **kwargs):
            if PROTECTED_BRANCH_KEY in argv:
                raise error
            return original(argv, **kwargs)
        executor = Mock()
        with patch.object(runner, 'bounded_run', side_effect=command):
            with self.assertRaisesRegex(runner.PilotError, 'branch protection'):
                self.run_fake(executor)
        executor.assert_not_called()
        self.assertFalse(execution_lock(self.repo, '.job.lock').exists())
        self.assertFalse(execution_lock(self.repo, '.process-uncertain.json').exists())


if __name__ == '__main__':
    unittest.main()
