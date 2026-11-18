"""Host authority and cooperative inventory regressions in disposable directories."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import stat
from types import SimpleNamespace
from unittest.mock import patch

from auto_coding import source_integrity as integrity


class SourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.request = {'sourceCommit':'a' * 40,'sourceBranch':'feature/integrity','sessionRunId':'session',
            'runId':'8dd2dbe0-a7fd-4d57-a580-86a78b4d0cfa','sandboxId':'224cc0db-e49c-4a9d-9d35-47b97f3c95f1',
            'invocation':1,'suiteIndex':1,'policySha256':'b' * 64}
        self.files = [integrity.file_record('app.py', b'VALUE = 2\n'),
                      integrity.file_record('tests/test_app.py', b'# tracked test\n')]
        for item, data in zip(self.files, (b'VALUE = 2\n', b'# tracked test\n')):
            path = self.source / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.expected = integrity.baseline(self.request, self.files)

    def observed(self, stage='post'):
        return integrity.observe(self.source, self.expected, stage)

    def test_unchanged_inventory_passes(self):
        self.assertTrue(integrity.validate_observation(self.observed(), self.expected, 'post')['matchesBaseline'])

    def test_modified_file_fails(self):
        (self.source / 'app.py').write_text('VALUE = 3\n')
        result = integrity.validate_observation(self.observed(), self.expected, 'post')
        self.assertFalse(result['matchesBaseline'])
        self.assertEqual(result['differences']['modified'], ['app.py'])

    def test_deleted_file_fails(self):
        (self.source / 'app.py').unlink()
        self.assertEqual(self.observed()['differences']['deleted'], ['app.py'])

    def test_added_file_fails_even_if_ignored_by_git(self):
        (self.source / 'unexpected.log').write_text('unexpected')
        self.assertEqual(self.observed()['differences']['added'], ['unexpected.log'])

    def test_rename_is_addition_and_deletion(self):
        (self.source / 'app.py').rename(self.source / 'renamed.py')
        value = self.observed()
        self.assertEqual(value['differences']['added'], ['renamed.py'])
        self.assertEqual(value['differences']['deleted'], ['app.py'])

    def test_new_tracked_file_is_protected(self):
        path = self.source / 'new.py'
        path.write_bytes(b'# newly approved\n')
        self.expected = integrity.baseline(self.request, self.files + [integrity.file_record('new.py', path.read_bytes())])
        self.assertTrue(self.observed()['matchesBaseline'])
        path.write_text('# changed\n')
        self.assertEqual(self.observed()['differences']['modified'], ['new.py'])

    def test_manifest_order_is_deterministic(self):
        other = integrity.baseline(self.request, list(reversed(self.files)))
        self.assertEqual(integrity.canonical(self.expected), integrity.canonical(other))
        self.assertEqual(integrity.digest(self.expected), integrity.digest(other))

    def test_malformed_sha256_and_size_rejected(self):
        for field, value in (('sha256','not-a-hash'), ('sha256','A' * 64), ('size',True), ('size',-1)):
            bad = copy.deepcopy(self.files)
            bad[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(integrity.IntegrityError):
                integrity.records(bad)

    def test_duplicate_unsafe_and_ambiguous_paths_rejected(self):
        for name in ('../app.py','/app.py','a//b','a/./b','a\\b','x:stream','NUL.txt','x.','x/../y','.git/config'):
            with self.subTest(path=name), self.assertRaises(integrity.IntegrityError):
                integrity.file_record(name, b'')
        for names in (('app.py','app.py'),('app.py','APP.py'),('A/x.py','a/y.py'),('x','x/y')):
            values = [{'path':name,'size':0,'sha256':'a' * 64} for name in names]
            with self.subTest(names=names), self.assertRaises(integrity.IntegrityError):
                integrity.records(values)

    def test_wrong_commit_session_run_or_stage_rejected(self):
        for field in ('sourceCommit','sessionRunId','runId'):
            value = self.observed()
            value['identity'][field] = 'stale'
            with self.subTest(field=field), self.assertRaises(integrity.IntegrityError):
                integrity.validate_observation(value, integrity.baseline(self.request, self.files), 'post')
            self.expected = integrity.baseline(self.request, self.files)
        with self.assertRaises(integrity.IntegrityError):
            integrity.validate_observation(self.observed('pre'), self.expected, 'post')

    def test_truncated_inconsistent_and_incomplete_inventory_rejected(self):
        for change in ('truncate','count','total','incomplete','missing-hash','lying-verdict'):
            value = copy.deepcopy(self.observed())
            if change == 'truncate': value['files'].pop()
            elif change == 'count': value['fileCount'] += 1
            elif change == 'total': value['totalBytes'] += 1
            elif change == 'incomplete': value['complete'] = False
            elif change == 'missing-hash': del value['files'][0]['sha256']
            elif change == 'lying-verdict': value['matchesBaseline'] = False
            with self.subTest(change=change), self.assertRaises(integrity.IntegrityError):
                integrity.validate_observation(value, self.expected, 'post')

    def test_exclusions_are_narrow_and_never_hide_tracked_files(self):
        (self.source / '.git').mkdir()
        (self.source / '.git/config').write_text('disposable metadata')
        cache = self.source / '__pycache__'
        cache.mkdir()
        path = cache / 'app.cpython-313.pyc'
        path.write_bytes(b'generated')
        self.assertTrue(self.observed()['matchesBaseline'])
        (cache / 'hidden.py').write_text('# not a cache')
        self.assertFalse(self.observed()['matchesBaseline'])
        self.expected = integrity.baseline(self.request, self.files + [integrity.file_record('__pycache__/app.cpython-313.pyc', b'approved')])
        self.assertIn('__pycache__/app.cpython-313.pyc', self.observed()['differences']['modified'])

    def test_links_are_not_followed(self):
        real_stat = Path.lstat
        for mode, flags in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            def metadata(path):
                if path == self.source / 'app.py':
                    return SimpleNamespace(st_mode=mode, st_file_attributes=flags)
                return real_stat(path)
            with self.subTest(mode=mode, flags=flags), patch.object(Path, 'lstat', metadata):
                value = self.observed()
                self.assertFalse(value['complete'])
                self.assertIn('link/reparse', value['error'])

    def test_duplicate_json_fields_are_rejected(self):
        path = self.root / 'bad.json'
        path.write_text('{"files":[],"files":[]}')
        with self.assertRaises(integrity.IntegrityError):
            integrity.load(path)

    def test_host_baseline_cannot_be_replaced_by_guest_expected_hashes(self):
        wrong = copy.deepcopy(self.expected)
        wrong['files'][0]['sha256'] = '0' * 64
        with self.assertRaises(integrity.IntegrityError):
            integrity.validate_baseline(wrong, self.request, integrity.digest(self.expected))

    def test_archive_names_and_commit_comment_do_not_hide_wrong_bytes(self):
        import zipfile
        path = self.root / 'wrong.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.comment = self.request['sourceCommit'].encode('ascii')
            archive.writestr('app.py', b'VALUE = 999\n')
            archive.writestr('tests/test_app.py', b'# tracked test\n')
        with self.assertRaises(integrity.IntegrityError):
            integrity.check_archive(path, self.expected)

    def test_controlled_modification_during_real_test_process_is_detected(self):
        test = self.source / 'test_mutation.py'
        test.write_text('import unittest\nfrom pathlib import Path\nclass Mutation(unittest.TestCase):\n'
                        ' def test_change(self):\n  Path("app.py").write_text("VALUE = 3\\n")\n  self.assertTrue(True)\n')
        self.expected = integrity.baseline(self.request, self.files + [integrity.file_record('test_mutation.py', test.read_bytes())])
        self.assertTrue(self.observed('pre')['matchesBaseline'])
        result = subprocess.run([sys.executable,'-B','-m','unittest','test_mutation','-v'], cwd=self.source,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK', result.stderr)
        self.assertEqual(self.observed()['differences']['modified'], ['app.py'])


if __name__ == '__main__':
    unittest.main()
