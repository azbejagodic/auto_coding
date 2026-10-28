"""Full-run ZIP contracts: synthetic evidence only, no runner or network."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_coding.debug_export import export_run
from auto_coding.desktop_app import Bridge
from auto_coding.run_history import RunHistory


class FullRunExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.runs = self.base / 'runs'
        self.runs.mkdir()
        self.run = self.make_run('one')
        self.out = self.base / 'one.zip'

    def put(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding='utf-8')

    def make_run(self, name):
        root = self.runs / name
        root.mkdir()
        self.put(root, 'run-manifest.json', {'run_id': name, 'task_ids': ['first', 'second'], 'start_time_utc': '2026-10-28T12:00:00Z'})
        self.put(root, 'desktop-run.json', {'status': 'stopped', 'plan': 'C:/plans/' + name + '.json'})
        self.put(root, 'state.json', {'run_id': name, 'status': 'completed', 'history': [
            {'task': 'first', 'attempt': 1, 'feedback': 'Rejected: preserve this full explanation'},
            {'task': 'first', 'attempt': 2}, {'task': 'second', 'attempt': 1}]})
        self.put(root, 'events.jsonl', json.dumps({'event': 'test_finished', 'duration_seconds': 12,
            'evidence': {'output': 'first-01-test-1.txt'}}) + '\n')
        self.put(root, 'plan-snapshot.json', {'tasks': [{'id': 'first', 'test_commands': [['python', '-m', 'unittest']]}, {'id': 'second'}]})
        for task, attempt in [('first', 1), ('first', 2), ('second', 1)]:
            prefix = f'{task}-{attempt:02}'
            for suffix in ['a-prompt.txt', 'a-answer.txt', 'a-events.jsonl', 'b-prompt.txt', 'b-events.jsonl', 'test-1.txt', 'diff.txt']:
                self.put(root, prefix + '-' + suffix, f'{name} {prefix} {suffix}\nfull stdout/stderr\n')
            self.put(root, prefix + '-b-verdict.json', {'decision': 'REJECTED' if attempt == 1 and task == 'first' else 'APPROVED', 'issues': ['Original natural language explanation'], 'reason': 'Unabridged feedback'})
        self.put(root, 'checkpoint/records.json', {'sha': 'abc', 'push': 'success'})
        return root

    def test_all_tasks_attempts_original_verdicts_and_bytes(self):
        before = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        result = export_run(self.run, self.out)
        self.assertTrue(result['ok'])
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(set(archive.namelist()), {'manifest.json', *('run/' + name for name in before)})
            for name, raw in before.items():
                self.assertEqual(archive.read('run/' + name), raw)
            rejected = json.loads(archive.read('run/first-01-b-verdict.json'))
            approved = json.loads(archive.read('run/first-02-b-verdict.json'))
            self.assertEqual(rejected['decision'], 'REJECTED')
            self.assertEqual(rejected['reason'], 'Unabridged feedback')
            self.assertEqual(approved['decision'], 'APPROVED')
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['run_id'], 'one')
            self.assertEqual(len(manifest['attempts']), 3)
            self.assertEqual(manifest['task_ids'], ['first', 'second'])
            self.assertTrue(manifest['complete'])
        self.assertEqual(before, {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob('*') if p.is_file()})

    def test_two_runs_discovery_and_isolation_deterministic(self):
        other = self.make_run('two')
        history = RunHistory(self.runs)
        rows = history.list()['runs']
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['plan_filename'] for r in rows}, {'one.json', 'two.json'})
        export_run(self.run, self.out)
        second = self.base / 'repeat.zip'
        export_run(self.run, second)
        self.assertEqual(self.out.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn(b'two', archive.read('run/first-01-a-answer.txt'))
        export_run(other, self.base / 'two.zip')
        with zipfile.ZipFile(self.base / 'two.zip') as archive:
            self.assertEqual(json.loads(archive.read('manifest.json'))['run_id'], 'two')

    def test_stopped_legacy_missing_references_no_external_reads(self):
        self.put(self.run, 'state.json', {'run_id': 'legacy', 'status': 'stopped'})
        (self.run / 'plan-snapshot.json').unlink()
        self.put(self.run, 'events.jsonl', json.dumps({'evidence': {'verdict': 'absent.json', 'escape': '../private.txt', 'absolute': str(self.base / 'private.txt')}}))
        self.put(self.base, 'private.txt', 'UNRELATED PRIVATE CONTENT')
        export_run(self.run, self.out)
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['status'], 'stopped')
            self.assertEqual(manifest['plan'], 'plan unavailable')
            self.assertIn('absent.json', manifest['missing_artifacts'])
            self.assertIn('../private.txt', manifest['missing_artifacts'])
            self.assertFalse(manifest['complete'])
            for member in archive.namelist():
                self.assertFalse(member.startswith('/'))
                self.assertNotIn('..', Path(member).parts)
                self.assertNotIn(b'UNRELATED PRIVATE CONTENT', archive.read(member))

    def test_archives_archive_temps_git_and_links_excluded(self):
        self.put(self.run, 'old.zip', 'previous archive')
        for name in ['run-export-abcd.tmp', 'debug-export-abcd.tmp', 'download.zip.tmp', 'download.zip.part']:
            self.put(self.run, name, 'mutable archive')
        self.put(self.run, 'checkout/.git/config', 'not evidence')
        self.put(self.run, 'checkout/source.py', 'checkout contents')
        self.put(self.base, 'private.txt', 'private outside')
        os.link(self.base / 'private.txt', self.run / 'hard-linked.txt')
        try:
            os.symlink(self.base / 'private.txt', self.run / 'linked.txt')
            os.symlink(self.base, self.run / 'linked-dir', target_is_directory=True)
        except OSError:
            pass  # Windows may deny creation without developer mode.
        export_run(self.run, self.out)
        with zipfile.ZipFile(self.out) as archive:
            names = archive.namelist()
            self.assertFalse(any(n.startswith('run/checkout/') or 'linked' in n for n in names))
            self.assertNotIn('run/old.zip', names)
            for name in ['run-export-abcd.tmp', 'debug-export-abcd.tmp', 'download.zip.tmp', 'download.zip.part']:
                self.assertNotIn('run/' + name, names)
            self.assertTrue(json.loads(archive.read('manifest.json'))['excluded'])
        self.put(self.run, '.git/config', 'repository root')
        with self.assertRaisesRegex(ValueError, 'repository checkout'):
            export_run(self.run, self.base / 'checkout.zip')

    def test_stopped_run_includes_interrupted_checkpoint_save_and_stable_temps(self):
        self.put(self.run, 'state.json', {'run_id': 'one', 'status': 'stopped'})
        checkpoint = {'run_id': 'one', 'status': 'checkpointing',
                      'pending_checkpoint': {'task': 'second', 'attempt': 1, 'sha': 'abc123'}}
        self.put(self.run, 'state.json.tmp', checkpoint)
        self.put(self.run, 'desktop-run.json.tmp', {'status': 'stopped', 'pid': 123})
        self.put(self.run, 'checkpoint/write.tmp', 'original pending push output\n')
        originals = {name: (self.run / name).read_bytes() for name in
                     ['state.json.tmp', 'desktop-run.json.tmp', 'checkpoint/write.tmp']}
        export_run(self.run, self.out)
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['status'], 'stopped')
            for name, raw in originals.items():
                self.assertEqual(archive.read('run/' + name), raw)
                self.assertIn(name, manifest['artifact_names'])
                self.assertIn({'name': name, 'bytes': len(raw)}, manifest['inventory'])
                self.assertEqual((self.run / name).read_bytes(), raw)
            self.assertEqual(json.loads(archive.read('run/state.json.tmp'))['pending_checkpoint'],
                             checkpoint['pending_checkpoint'])

    def test_stopped_error_or_corrupt_state_keeps_all_raw_evidence(self):
        for state in ['{"status":"error"}', 'broken state']:
            self.put(self.run, 'state.json', state)
            export_run(self.run, self.out)
            with zipfile.ZipFile(self.out) as archive:
                self.assertEqual(archive.read('run/state.json').decode(), state)
                self.assertIn('run/first-01-b-verdict.json', archive.namelist())
            self.out.unlink()

    def test_nested_reparse_directory_is_not_followed_without_symlink_privileges(self):
        self.put(self.run, 'redirected/private.txt', 'DO NOT INCLUDE')
        original = Path.lstat

        def simulated_reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.run / 'redirected':
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, 'lstat', simulated_reparse):
            export_run(self.run, self.out)
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn('run/redirected/private.txt', archive.namelist())
            manifest = json.loads(archive.read('manifest.json'))
            self.assertIn({'name': 'redirected', 'reason': 'Linked or redirected path excluded'}, manifest['excluded'])
            self.assertFalse(manifest['complete'])

    def test_corrupt_state_does_not_hide_active_desktop(self):
        self.put(self.run, 'state.json', 'broken')
        self.put(self.run, 'desktop-run.json', {'status':'running', 'pid':123})
        with patch('auto_coding.run_history.DesktopPID.active', return_value=True):
            with self.assertRaisesRegex(ValueError, 'Available when run stops'):
                export_run(self.run, self.out)

    def test_large_active_state_cannot_bypass_listing_limit(self):
        (self.run / 'desktop-run.json').unlink()
        self.put(self.run, 'state.json', {'status': 'running', 'feedback': 'x' * (300 * 1024)})
        self.assertEqual(RunHistory.summary(str(self.run))['status'], 'unavailable')
        with self.assertRaisesRegex(ValueError, 'Available when run stops'):
            export_run(self.run, self.out)
        self.assertFalse(self.out.exists())

    def test_error_state_with_confirmed_exited_desktop_is_downloadable(self):
        self.put(self.run, 'state.json', {'status': 'error'})
        self.put(self.run, 'desktop-run.json', {'status': 'running', 'pid': 123})
        with patch('auto_coding.run_history.DesktopPID.active', return_value=False):
            self.assertEqual(RunHistory.summary(str(self.run))['status'], 'stopped')
            self.assertTrue(export_run(self.run, self.out)['ok'])

    def test_exited_desktop_overrides_stale_running_state_without_modifying_evidence(self):
        # poll() writes stopped desktop metadata after abnormal process exit,
        # while durable runner state may never get a final write.
        for status in ('running', 'initializing', 'error', 'checkpointing'):
            with self.subTest(status=status):
                self.put(self.run, 'state.json', {'run_id':'one', 'status':status,
                    'history':[{'task':'first', 'attempt':1, 'feedback':'original rejection'}]})
                before = (self.run / 'state.json').read_bytes()
                self.assertEqual(RunHistory.summary(str(self.run))['status'], 'stopped')
                bridge, dialogs = self.bridge()
                self.assertTrue(bridge.download_run(str(self.run))['ok'])
                with zipfile.ZipFile(self.out) as archive:
                    self.assertEqual(json.loads(archive.read('manifest.json'))['status'], 'stopped')
                    self.assertEqual(archive.read('run/state.json'), before)
                    self.assertIn('run/first-01-b-verdict.json', archive.namelist())
                self.assertEqual((self.run / 'state.json').read_bytes(), before)
                self.out.unlink()

    def test_missing_reference_lists_and_torn_events_are_indexed(self):
        # This is the runner's actual correction_requested evidence shape.
        self.put(self.run, 'events.jsonl', '\n'.join([
            json.dumps({'event': 'correction_requested', 'task_id': 'first', 'attempt': 1,
                        'evidence': {'test_output': ['first-01-test-1.txt', 'missing-test.txt']}}),
            'broken record',
            json.dumps({'evidence': {'verdict': 'missing-verdict.json'}}),
        ]))
        export_run(self.run, self.out)
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['missing_artifacts'], ['missing-test.txt', 'missing-verdict.json'])
            self.assertTrue(manifest['metadata_errors'])
            self.assertFalse(manifest['complete'])
            self.assertIn('broken record', archive.read('run/events.jsonl').decode())

    def bridge(self):
        controller = Mock(repo=None, logs=None, run_history=RunHistory(self.runs))
        dialogs = Mock()
        dialogs.save_export.return_value = str(self.out)
        bridge = Bridge(controller=controller, dialogs=dialogs)
        return bridge, dialogs

    def test_bridge_row_does_not_select_run_native_save_cancel_invalid_failure(self):
        bridge, dialogs = self.bridge()
        bridge._root = self.make_run('two')
        self.assertTrue(bridge.download_run(str(self.run))['ok'])
        self.assertEqual(bridge._root.name, 'two')
        dialogs.confirm_export.assert_not_called()
        dialogs.save_export.assert_called_once_with('one.json-2026-10-28-one.zip')
        self.out.unlink()
        for destination in [None, 123, str(self.base / 'bad.txt'), str(self.run / 'unsafe.zip')]:
            dialogs.save_export.return_value = destination
            result = bridge.download_run(str(self.run))
            self.assertFalse(result['ok'])
            self.assertTrue(result['error'])
        dialogs.save_export.return_value = str(self.out)
        with patch('auto_coding.desktop_app.export_run', side_effect=OSError('disk full')):
            result = bridge.download_run(str(self.run))
        self.assertFalse(result['ok'])
        self.assertIn('disk full', result['error'])
        self.assertFalse(bridge.download_run(str(self.base))['ok'])

    def test_active_run_disabled_and_write_failure_no_partial_zip(self):
        (self.run / 'desktop-run.json').unlink()
        self.put(self.run, 'state.json', {'run_id': 'one', 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'Available when run stops'):
            export_run(self.run, self.out)
        self.put(self.run, 'state.json', {'run_id': 'one', 'status': 'stopped'})
        with patch.object(zipfile.ZipFile, 'writestr', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                export_run(self.run, self.out)
        self.assertFalse(self.out.exists())
        self.assertFalse(list(self.base.glob('run-export-*.tmp')))

if __name__ == '__main__':
    unittest.main()
