"""Task tests must import the selected checkout even under a different launcher."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_coding import simple_run as runner


class TestCheckoutTests(unittest.TestCase):
    def test_normal_and_resumed_tests_import_checkout_instead_of_launcher(self):
        source_package = Path(runner.__file__).resolve().parent
        for launcher in ('stable main', 'managed ui', 'packaged app'):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                repo = root / 'managed ui č š'
                logs = root / 'logs'
                old_src = root / 'old stable č' / 'src'
                old_package = old_src / 'auto_coding'
                old_package.mkdir(parents=True)
                (old_package / '__init__.py').write_text("VERSION = 'old'\n", encoding='utf-8')
                (old_package / 'checkout_probe.py').write_text("VERSION = 'old'\n", encoding='utf-8')
                extra = root / 'unrelated packages'
                extra.mkdir()
                (extra / 'user_helper.py').write_text("VALUE = 'preserved'\n", encoding='utf-8')
                inherited = os.pathsep.join((str(old_src), str(extra)))
                command = [sys.executable, '-B', '-m', 'unittest', 'tests.test_origin']
                plan = root / 'plan.json'
                plan.write_text(json.dumps({'tasks': [{
                    'id': 'one', 'prompt': 'Create fixture', 'test_commands': [command]
                }]}), encoding='utf-8')
                real_run = subprocess.run
                test_calls, codex_calls = [], []
                first = True

                def launch(argv, **options):
                    nonlocal first
                    if argv[0] == 'codex':
                        codex_calls.append((argv, options))
                        self.assertEqual(Path(options['cwd']).resolve(), repo.resolve())
                        self.assertNotIn('env', options)
                        answer = Path(argv[argv.index('--output-last-message') + 1])
                        if '--output-schema' in argv:
                            answer.write_text(json.dumps({
                                'decision': 'APPROVED', 'issues': [], 'summary': 'OK'
                            }), encoding='utf-8')
                        else:
                            package = repo / 'src' / 'auto_coding'
                            # Copy the real layout, including __init__.py only if
                            # the source has one; a synthetic initializer hid the
                            # namespace-versus-regular-package regression.
                            shutil.copytree(source_package, package, dirs_exist_ok=True,
                                            ignore=shutil.ignore_patterns('__pycache__'))
                            (package / 'checkout_probe.py').write_text("VERSION = 'managed'\n", encoding='utf-8')
                            tests = repo / 'tests'
                            tests.mkdir(exist_ok=True)
                            (tests / '__init__.py').write_text('', encoding='utf-8')
                            (tests / 'test_origin.py').write_text(
                                "import json, os, pathlib, unittest\n"
                                "import auto_coding, user_helper\n"
                                "from auto_coding import checkout_probe\n"
                                "class OriginTests(unittest.TestCase):\n"
                                "    def test_origin(self):\n"
                                "        self.assertEqual(checkout_probe.VERSION, 'managed')\n"
                                "        self.assertEqual(user_helper.VALUE, 'preserved')\n"
                                "        self.assertEqual(os.environ['CHECKOUT_TEST_VALUE'], 'retained')\n"
                                "        print(json.dumps({'origin': auto_coding.__file__, 'module': checkout_probe.__file__, 'cwd': os.getcwd()}))\n",
                                encoding='utf-8')
                            answer.write_text('Created fixture', encoding='utf-8')
                        return subprocess.CompletedProcess(argv, 0, 'fake codex')
                    if argv == command:
                        test_calls.append(options)
                        self.assertEqual(Path(options['cwd']).resolve(), repo.resolve())
                        self.assertEqual(options['env']['PYTHONPATH'],
                                         str((repo / 'src').resolve()) + os.pathsep + inherited)
                        self.assertEqual(options['env']['CHECKOUT_TEST_VALUE'], 'retained')
                        self.assertEqual(options['timeout'], 37)
                        self.assertEqual(options['stdout'], subprocess.PIPE)
                        self.assertEqual(options['stderr'], subprocess.STDOUT)
                        self.assertFalse(options['shell'])
                        if sys.platform == 'win32':
                            self.assertEqual(options['creationflags'], subprocess.CREATE_NO_WINDOW)
                        else:
                            self.assertNotIn('creationflags', options)
                        result = real_run(argv, **options)
                        self.assertEqual(result.returncode, 0, result.stdout)
                        evidence = next(json.loads(line) for line in result.stdout.splitlines()
                                        if line.startswith('{'))
                        self.assertEqual(Path(evidence['origin']).resolve(),
                                         (repo / 'src' / 'auto_coding' / '__init__.py').resolve())
                        self.assertEqual(Path(evidence['module']).resolve(),
                                         (repo / 'src' / 'auto_coding' / 'checkout_probe.py').resolve())
                        self.assertEqual(Path(evidence['cwd']).resolve(), repo.resolve())
                        if first:
                            first = False
                            raise subprocess.TimeoutExpired(argv, options['timeout'], output=result.stdout)
                        return result
                    return real_run(argv, **options)

                # Launcher location and parent sys.path must not select child imports.
                with patch.dict(os.environ, {'PYTHONPATH': inherited, 'CHECKOUT_TEST_VALUE': 'retained'}), \
                        patch.object(runner, '__file__', str(root / launcher / 'launcher.py')), \
                        patch.object(sys, 'path', [str(old_src), *sys.path]), \
                        patch.object(runner.subprocess, 'run', side_effect=launch), \
                        patch.object(runner, 'mark_process_uncertain') as uncertain:
                    parent_env = os.environ.copy()
                    self.assertEqual(runner.run(plan, repo, logs, create_repo=True,
                                                attempts=1, timeout=19, test_timeout=37), 1)
                    self.assertEqual(runner.run(plan, repo, logs, resume=True,
                                                attempts=2, timeout=19), 0)
                    self.assertEqual(dict(os.environ), parent_env)
                    uncertain.assert_called_once_with()
                self.assertEqual(len(test_calls), 2)
                self.assertIsNot(test_calls[0]['env'], test_calls[1]['env'])
                self.assertEqual(len(codex_calls), 3)
                self.assertTrue(all(options['timeout'] == 19 for _, options in codex_calls))
                self.assertEqual([argv[argv.index('--sandbox') + 1] for argv, _ in codex_calls],
                                 ['workspace-write', 'workspace-write', 'read-only'])
                self.assertEqual((old_package / '__init__.py').read_text(encoding='utf-8'), "VERSION = 'old'\n")


if __name__ == '__main__':
    unittest.main()
