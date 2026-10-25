import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_coding.simple_run import load_tasks, run, PilotError


class SimpleRunnerTests(unittest.TestCase):
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
        self.assertTrue((self.repo / 'index.html').exists())

    def test_failed_approved_test_then_feedback_and_review(self):
        self.plan = self.root / 'task.json'
        self.plan.write_text(json.dumps({'tasks': [
            {'id': 'one', 'prompt': 'Build', 'test_commands': [['py', '-B', 'test.py']]}
        ]}), encoding='utf-8')
        calls = []
        def fake(argv, cwd, timeout, *, prompt=None):
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

    def test_existing_run_refuses_dirty_repo(self):
        self.repo.mkdir()
        subprocess.run(['git', 'init'], cwd=self.repo, stdout=subprocess.PIPE, check=True)
        (self.repo / 'surprise.txt').write_text('existing change')
        with self.assertRaises(PilotError):
            run(self.plan, self.repo, self.logs, execute=lambda *a, **k: (1, '', False))

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
