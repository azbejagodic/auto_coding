"""Configuration/capability regression checks. No CLI, network or model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import model_catalog, simple_run
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_app import Bridge
from auto_coding.run_launcher import LaunchState, launch_runner


def catalog_fixture():
    return [dict(model=name, displayName=name, efforts=['low', 'medium', 'high'], default='medium')
            for name in ('fixture-model', 'fixture-auditor', 'fixture-a', 'fixture-b')]


def configure_fixture(controller, audit='fixture-auditor'):
    """Represent previously applied settings without IO in legacy safety fixtures."""
    controller.catalog = catalog_fixture()
    controller.catalog_error = None
    controller.configuration = dict(model_audit=audit, model_a='fixture-a', model_b='fixture-b',
                                    effort_a='medium', effort_b='high', attempts=3, goal='')
    controller._configuration_identity = controller.configuration_identity()
    return dict(controller.configuration)


class CatalogTests(unittest.TestCase):
    def item(self, name, efforts=('low', 'high'), default='high'):
        return dict(model=name, displayName='Display ' + name,
                    supportedReasoningEfforts=[{'reasoningEffort': e} for e in efforts],
                    defaultReasoningEffort=default)

    def test_authenticated_catalog_pages_preserve_real_ids_efforts_and_defaults(self):
        request = Mock(side_effect=[{'account': {'type': 'chatgpt'}},
            {'data': [self.item('real-a')], 'nextCursor': 'page2'},
            {'data': [self.item('real-b', ('minimal', 'xhigh'), 'minimal')], 'nextCursor': None}])
        models = model_catalog.collect_models(request)
        self.assertEqual([m['model'] for m in models], ['real-a', 'real-b'])
        self.assertEqual(models[1]['efforts'], ['minimal', 'xhigh'])
        self.assertEqual(models[1]['default'], 'minimal')
        self.assertEqual(request.call_args.args[0]['params']['cursor'], 'page2')

    def test_failed_unauthenticated_malformed_and_incomplete_catalogs(self):
        responses = [
            [{'account': None}],
            [{'account': {} }],
            [{'account': {'type': 'apiKey'}}, {'data': []}],
            [{'account': {'type': 'chatgpt'}}, {'data': [self.item('bad id')]}],
            [{'account': {'type': 'chatgpt'}}, {'data': [self.item('ok', default='unsupported')]}],
            [{'account': {'type': 'chatgpt'}}, {'data': [self.item('a')], 'nextCursor': 'loop'},
             {'data': [self.item('b')], 'nextCursor': 'loop'}]]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                model_catalog.collect_models(Mock(side_effect=response))
        with self.assertRaises(OSError):
            model_catalog.collect_models(Mock(side_effect=OSError('RPC unavailable')))

    def test_missing_cli_and_rpc_failure_never_provide_custom_fallback(self):
        with patch.object(model_catalog.shutil, 'which', return_value=None), \
                patch.object(model_catalog, 'bounded_run') as run:
            with self.assertRaisesRegex(ValueError, 'Install and authenticate'):
                model_catalog.load_catalog(Path('.'))
            run.assert_not_called()
        with patch.object(model_catalog.shutil, 'which', return_value='codex'), \
                patch.object(model_catalog, 'bounded_run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError, 'sign-in'):
                model_catalog.load_catalog(Path('.'))

    def test_catalog_transport_is_bounded_trusted_and_does_not_request_a_model_call(self):
        models = catalog_fixture()
        with patch.object(model_catalog.shutil, 'which', return_value='codex'), \
                patch.object(model_catalog, 'bounded_run', return_value=Mock(
                    returncode=0, stdout=json.dumps(models))) as run:
            self.assertEqual(model_catalog.load_catalog(Path('target with spaces')), models)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ['-B', '-c'])
        self.assertIn('collect_models(request)', command[3])
        self.assertNotIn('thread/start', command[3])
        self.assertEqual(json.loads(command[4])[0], ['codex', 'app-server', '--strict-config'])
        self.assertEqual(run.call_args.kwargs['timeout'], 40)
        self.assertEqual(run.call_args.kwargs['env']['PYTHONPATH'], run.call_args.kwargs['cwd'])

    def test_audit_high_is_mandatory_and_unsupported_efforts_ids_are_rejected(self):
        profile = dict(model_audit='fixture-auditor', model_a='fixture-a', model_b='fixture-b',
                       effort_a='medium', effort_b='high', attempts=3, goal='')
        for key, value in [('effort_a', 'xhigh'), ('model_b', 'imagined'), ('model_a', []), ('attempts', True),
                           ('attempts', 11), ('audit_reasoning', 'low')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                model_catalog.validate_profile(dict(profile, **{key: value}), catalog_fixture())
        models = catalog_fixture()
        models[1]['efforts'] = ['low']
        with self.assertRaises(ValueError):
            model_catalog.validate_profile(profile, models)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'target'
        self.repo.mkdir()
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'tasks': [{'id': 'one', 'prompt': 'Implement one'}]}))
        self.controller = DesktopController(self.root / 'app' / 'runs')
        self.controller.repo, self.controller.plan = self.repo, self.plan
        self.check = {'branch': 'feature/ui', 'clean': True, 'errors': []}
        self.enterContext(patch.object(self.controller, 'preflight', side_effect=lambda: dict(self.check)))
        self.enterContext(patch.object(self.controller, 'job_status', return_value={'active': False, 'uncertain': False}))
        self.catalog = self.enterContext(patch('auto_coding.desktop_control.load_catalog', return_value=catalog_fixture()))
        self.profile = configure_fixture(self.controller)
        self.controller.configuration = None
        self.bridge = Bridge(controller=self.controller, dialogs=Mock())

    def test_fresh_configuration_is_separate_from_approval_and_revalidates(self):
        self.assertFalse(self.controller.configuration_status()['ready'])
        with patch.object(self.controller, 'start_audit') as launch:
            self.assertFalse(self.bridge.start_audit('fixture-auditor')['ok'])
            launch.assert_not_called()
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        self.assertTrue(self.controller.configuration_status()['ready'])
        with patch.object(self.controller, 'audit_status', return_value={'start_approved': False}), \
                patch.object(self.controller, 'start') as launch:
            self.assertFalse(self.bridge.start()['ok'])
            launch.assert_not_called()
        self.plan.write_text(self.plan.read_text() + '\n')
        self.assertFalse(self.controller.configuration_status()['ready'])
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        self.check['branch'] = 'feature/other'
        self.assertFalse(self.controller.configuration_status()['ready'])

    def test_settings_are_non_secret_outside_checkout_and_reloaded_only_for_same_identity(self):
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        settings = self.controller.runs_dir.parent / 'desktop-settings.json'
        value = json.loads(settings.read_text())
        self.assertEqual(set(value), {'identity', 'profile'})
        self.assertFalse(settings.is_relative_to(self.repo))
        self.controller.configuration = None
        self.controller.load_configuration()
        self.assertEqual(self.controller.configuration, self.profile)
        self.assertTrue(self.controller.configuration_status()['ready'])
        self.check['branch'] = 'feature/different'
        self.controller.configuration = None
        self.controller.load_configuration()
        self.assertIsNone(self.controller.configuration)

    def test_stale_saved_model_and_catalog_errors_relock_without_substitution(self):
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        self.catalog.return_value = catalog_fixture()[:1]
        self.controller.load_configuration()
        self.assertFalse(self.controller.configuration_status()['ready'])
        self.assertEqual(self.controller.configuration, self.profile)
        self.catalog.side_effect = ValueError('Unavailable')
        self.assertFalse(self.bridge.refresh_models()['ok'])
        self.assertEqual(self.controller.catalog, [])
        self.assertFalse(self.bridge.start_audit('fixture-auditor')['ok'])

    def test_active_uncertain_dirty_and_attempts_changes_block_apply_or_launch(self):
        for state in ({'active': True}, {'active': None, 'uncertain': True}):
            self.controller.job_status.return_value = state
            self.assertFalse(self.bridge.apply_configuration(self.profile)['ok'])
        self.controller.job_status.return_value = {'active': False, 'uncertain': False}
        self.check['clean'] = False
        self.assertFalse(self.bridge.apply_configuration(self.profile)['ok'])
        self.check['clean'] = True
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        with patch.object(self.controller, 'start') as launch:
            self.assertFalse(self.bridge.start(4)['ok'])
            launch.assert_not_called()
        with patch.object(self.controller, 'start_audit') as launch:
            self.assertFalse(self.bridge.start_audit('fixture-auditor', 'changed goal')['ok'])
            launch.assert_not_called()

    def test_launcher_passes_separate_argv_and_keeps_sandbox_permissions(self):
        logs = self.root / 'logs'
        logs.mkdir()
        spawn = Mock(return_value=Mock(pid=123))
        result = launch_runner(LaunchState(), repo=self.repo, plan=self.plan, logs=logs,
            attempts=3, model_options=self.profile, popen=spawn, reserve=Mock(return_value=None),
            persist_metadata=Mock())
        self.assertTrue(result['ok'])
        argv = spawn.call_args.args[0]
        for option, expected in [('--model-a', 'fixture-a'), ('--model-b', 'fixture-b'),
                                 ('--reasoning-a', 'medium'), ('--reasoning-b', 'high')]:
            self.assertEqual(argv[argv.index(option) + 1], expected)
        self.assertIs(spawn.call_args.kwargs['shell'], False)
        for readonly in (True, False):
            command = simple_run.codex_argv(readonly=readonly, last_message=logs / 'answer',
                                            schema=None, model='fixture-b', reasoning='high')
            self.assertEqual(command[command.index('--sandbox') + 1], 'read-only' if readonly else 'workspace-write')
            self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(simple_run.model_request_metadata('fixture-b', 'high')['reasoning_effort'], 'high')

    def test_resume_uses_persisted_profile_and_rejects_missing_profile(self):
        logs = self.root / 'logs'
        logs.mkdir()
        self.controller.logs = logs
        requested = dict(model_a='fixture-a', model_b='fixture-b', reasoning_a='medium', reasoning_b='high')
        simple_run.save_json(logs / 'state.json', {'status':'stopped', 'model_profile':requested})
        simple_run.save_json(logs / 'run-manifest.json', {'model_profile':requested})
        simple_run.save_json(logs / 'desktop-models.json', self.profile)
        self.controller.configuration = dict(self.profile, model_a='fixture-model')
        with patch('auto_coding.desktop_control.launch_runner', return_value={'ok': True}) as launch:
            self.assertTrue(self.controller._launch(3, True)['ok'])
            self.assertEqual(launch.call_args.kwargs['model_options'], self.profile)
            tampered = dict(self.profile, effort_b='low')
            simple_run.save_json(logs / 'desktop-models.json', tampered)
            with self.assertRaisesRegex(ValueError, 'differs from original'):
                self.controller._launch(3, True)
            (logs / 'desktop-models.json').unlink()
            with self.assertRaisesRegex(ValueError, 'no authorized model profile'):
                self.controller._launch(3, True)

    def test_engine_resume_rejects_changed_model_or_disagreeing_manifest_before_execution(self):
        logs = self.root / 'engine'
        logs.mkdir()
        _, digest = simple_run.load_tasks(self.plan)
        original = dict(model_a='fixture-a', model_b='fixture-b', reasoning_a='medium', reasoning_b='high')
        state = dict(repo=str(self.repo), plan_sha256=digest, status='stopped',
                     completed=[], history=[], model_profile=original)
        manifest = dict(target_repo=str(self.repo), plan_sha256=digest, task_ids=['one'],
                        run_id='test-run', model_profile=original)
        simple_run.save_json(logs / 'state.json', state)
        simple_run.save_json(logs / 'run-manifest.json', manifest)
        execute = Mock(side_effect=AssertionError('No agent may execute'))
        with patch.object(simple_run, 'safe_repo', return_value=self.repo), \
                patch.object(simple_run, 'assert_test_process_safe'), \
                patch.object(simple_run, 'assert_process_safe'), \
                patch.object(simple_run, 'require_run_branch', return_value='feature/ui'):
            with self.assertRaisesRegex(simple_run.PilotError, 'differs from original'):
                simple_run._run(self.plan, self.repo, logs, resume=True, execute=execute,
                               **dict(original, model_a='fixture-model'))
            manifest['model_profile'] = dict(original, reasoning_b='low')
            simple_run.save_json(logs / 'run-manifest.json', manifest)
            with self.assertRaisesRegex(simple_run.PilotError, 'state and manifest'):
                simple_run._run(self.plan, self.repo, logs, resume=True, execute=execute, **original)
        execute.assert_not_called()

    def test_changed_authorized_models_during_native_confirmation_need_new_consent(self):
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        approval = {'start_approved': True, 'directory': 'verified-fixture',
                    'configuration': {'model': 'fixture-auditor', 'goal': ''}, 'identity': {'test': 'same'}}
        def change(_):
            self.controller.configuration = dict(self.profile, effort_a='low')
            return True
        self.bridge._dialogs.confirm.side_effect = change
        with patch.object(self.controller, 'audit_status', return_value=approval), \
                patch.object(self.controller, 'start') as launch:
            result = self.bridge.start()
            self.assertIn('changed during confirmation', result['error'])
            launch.assert_not_called()

    def test_catalog_uncertain_cleanup_preserves_existing_process_boundary(self):
        error = OSError('Catalog helper cleanup unavailable')
        error.cleanup_confirmed, error.process_started = False, True
        self.catalog.side_effect = error
        with patch('auto_coding.repository_ownership.preserve_process_uncertainty') as preserve:
            self.assertFalse(self.bridge.refresh_models()['ok'])
            self.assertTrue(self.controller._launch_uncertain)
            preserve.assert_called_once_with(self.repo, self.controller.runs_dir)

    def test_invalid_cli_reasoning_is_rejected_before_scratch_or_agent_execution(self):
        for model, effort in [('fixture-a', 'high";permissions'), (None, 'high'), ('invalid id', 'high')]:
            with self.subTest(model=model, effort=effort), \
                    patch.object(simple_run, 'prepare_review') as prepare, \
                    self.assertRaisesRegex(simple_run.PilotError, 'Explicit reasoning requires'):
                simple_run._run(self.plan, self.repo, self.root / 'logs', model_b=model, reasoning_b=effort)
            prepare.assert_not_called()

    def test_settings_collision_preserves_existing_file_and_applied_configuration(self):
        self.assertTrue(self.bridge.apply_configuration(self.profile)['ok'])
        temporary = self.controller.runs_dir.parent / 'desktop-settings-collision.tmp'
        temporary.write_text('Existing controller-owned evidence')
        with patch('auto_coding.desktop_control.uuid.uuid4', return_value=Mock(hex='collision')):
            self.assertFalse(self.bridge.apply_configuration(dict(self.profile, effort_a='low'))['ok'])
        self.assertEqual(temporary.read_text(), 'Existing controller-owned evidence')
        self.assertEqual(self.controller.configuration, self.profile)
