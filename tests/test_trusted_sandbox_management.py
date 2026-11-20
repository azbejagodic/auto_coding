"""Sealed management boundary; all native processes/VM transport are doubled."""
import base64
import contextlib
import copy
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from auto_coding import trusted_sandbox_management as management


class ManagementPolicyTests(unittest.TestCase):
    @contextlib.contextmanager
    def native_double(self,on_flags=None):
        process=types.SimpleNamespace(pid=7,returncode=0,poll=lambda:0,
            stdout=io.BytesIO(b''),stderr=io.BytesIO(b''))
        def flags():
            if on_flags:on_flags()
            return 0
        job=types.SimpleNamespace(processes={},creation_flags=flags,assign_and_resume=lambda child:None,
            survivors=lambda pid:[],cancel_transients=lambda child:True,close=lambda:None)
        module=types.SimpleNamespace(ManagementJob=lambda *args:job)
        with patch.dict('sys.modules',{'auto_coding.windows_management_job':module}), \
                patch.object(management.subprocess,'Popen',return_value=process) as spawn:
            yield spawn

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / ('windows-sandbox-'+'a'*32);self.root.mkdir()
        self.repo = self.base / 'source';self.repo.mkdir()
        self.request = {'runId':'8dd2dbe0-a7fd-4d57-a580-86a78b4d0cfa',
            'sandboxId':'224cc0db-e49c-4a9d-9d35-47b97f3c95f1',
            'executionScope':'approved-session-v2','policy':{'test_commands':[['one'],['two']], 'timeout_seconds':30}}
        self.package = {'signatureVerified':True,'packagePayloadVerified':True,
            'managementHostVerified':True,'activationAttempted':False,'cli':str(self.base/'wsb.exe'),
            'image':str(self.base/'WindowsSandboxServer.exe'),'packageFullName':'signed-package','brokerSha256':'1'*64}
        (self.root/'request.json').write_text(json.dumps(self.request))
        (self.root/'input').mkdir()
        worker=management.CONTROLLER/'src/auto_coding/source_integrity.py'
        (self.root/'input/source_integrity.py').write_bytes(worker.read_bytes())

    def session(self):
        with patch.object(management.sys,'platform','win32'), patch.dict(os.environ,{'SYSTEMROOT':str(self.base/'Windows')}), \
                patch.object(management,'bounded_run',return_value=subprocess.CompletedProcess([],0,json.dumps(self.package),'')) as validate:
            value=management.ManagementSession(self.root,self.request,self.repo)
        command=base64.b64decode(validate.call_args.args[0][-1]).decode('utf-16-le')
        self.assertIn('-ValidateOnly',command)
        self.assertNotIn('ActivateApplication',command)
        self.assertEqual(validate.call_args.kwargs['timeout'],45)
        value.bind()
        return value

    def test_reviewed_script_hashes_match_and_target_files_cannot_substitute(self):
        value=self.session()
        self.assertEqual(len(value.snapshot),6)
        for name,digest in value.snapshot.items():
            self.assertEqual(hashlib.sha256((value.scripts/name).read_bytes()).hexdigest(),digest)
        (self.repo/'run.ps1').write_text('throw "project script"')
        value._check()
        (value.scripts/'run.ps1').write_text('throw "replaced snapshot"')
        with self.assertRaisesRegex(ValueError,'snapshot'):value._check()

    def test_modified_controller_launcher_is_rejected_before_package_or_cli(self):
        real=management.script_bytes
        def changed(path):return b'changed' if path.name=='run.ps1' else real(path)
        with patch.object(management.sys,'platform','win32'),patch.object(management,'script_bytes',side_effect=changed), \
                patch.object(management,'bounded_run') as run:
            with self.assertRaisesRegex(ValueError,'Unreviewed'):management.ManagementSession(self.root,self.request,self.repo)
        run.assert_not_called()

    def test_only_fixed_runner_and_owned_rescue_commands_can_execute(self):
        value=self.session()
        with patch.object(value,'_execute',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            value.run(value.runner_argv(),cwd=str(value.scripts),timeout=1560)
            run.assert_called_once()
            (value.root/'launch-attempt.json').write_text(json.dumps({key:self.request[key] for key in ('runId','sandboxId')}))
            value.run([value.cli,'stop','--id',self.request['sandboxId'],'--raw'],cwd=str(value.scripts),timeout=120)
            for argv in ([str(self.base/'evil.exe')], [value.cli,'stop','--id','ffffffff-ffff-ffff-ffff-ffffffffffff','--raw']):
                with self.subTest(argv=argv),self.assertRaises((ValueError,OSError)):
                    value.run(argv,cwd=str(value.scripts),timeout=120)
            self.assertEqual(run.call_count,2)

    def test_foreign_inventory_is_retained_but_invalid_ids_and_duplicate_ids_reject(self):
        value=self.session()
        foreign='ffffffff-ffff-ffff-ffff-ffffffffffff'
        for inventory,ok in (({'WindowsSandboxEnvironments':[{'Id':foreign}]},True),
            ({},False),({'WindowsSandboxEnvironments':[{'Id':'invalid'}]},False),
            ({'WindowsSandboxEnvironments':[{'Id':foreign},{'Id':foreign}]},False)):
            with patch.object(value,'_execute',return_value=subprocess.CompletedProcess([],0,json.dumps(inventory),'')) as run:
                if ok:self.assertEqual(value.preflight(),inventory)
                else:
                    with self.assertRaises(ValueError):value.preflight()
                self.assertEqual(run.call_args.args[1],[value.cli,'list','--raw'])

    def test_cli_failure_empty_output_and_timeout_never_pass_preflight(self):
        value=self.session()
        for code,text in ((1,'{"WindowsSandboxEnvironments":[]}'),(0,''),(0,'not-json')):
            with patch.object(value,'_execute',return_value=subprocess.CompletedProcess([],code,text,'')):
                with self.assertRaises(ValueError):value.preflight()
        with patch.object(value,'_execute',side_effect=subprocess.TimeoutExpired('CLI',30)):
            with self.assertRaises(subprocess.TimeoutExpired):value.preflight()

    def test_own_id_in_inventory_cannot_be_reclaimed(self):
        value=self.session()
        text=json.dumps({'WindowsSandboxEnvironments':[{'Id':self.request['sandboxId']}]})
        with patch.object(value,'_execute',return_value=subprocess.CompletedProcess([],0,text,'')):
            with self.assertRaisesRegex(ValueError,'already exists'):value.preflight()

    def test_guest_policy_argv_never_enters_host_runner_code(self):
        self.request['policy']['test_commands'][0]=['python','-c','host injection; $(danger)']
        (self.root/'request.json').write_text(json.dumps(self.request))
        value=self.session()
        code=base64.b64decode(value.runner_argv()[-1]).decode('utf-16-le')
        self.assertNotIn('host injection',code)
        self.assertNotIn('danger',code)
        self.assertIn('-Mode Session',code)
        with self.assertRaises(ValueError):value.run(value.runner_argv(),cwd=str(self.repo),timeout=1560)

    def test_request_change_or_noncanonical_execution_options_fail_closed(self):
        value=self.session()
        with self.assertRaises(ValueError):value.run(value.runner_argv(),cwd=str(value.scripts),timeout=1560,shell=True)
        (self.root/'request.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'request changed'):value._check()

    def test_direct_execute_rejects_unauthorized_commands_before_job_or_process_creation(self):
        value=self.session()
        marker={key:self.request[key] for key in ('runId','sandboxId')}
        (value.root/'launch-attempt.json').write_text(json.dumps(marker))
        runner=value.runner_argv()
        altered_runner=runner[:-1]+['arbitrary encoded host command']
        attempts=[('preflight',[str(self.base/'evil.exe')],30),
            ('preflight',[value.cli,'stop','--all','--raw'],30),
            ('preflight',[value.cli,'list','--raw','extra'],30),
            ('preflight',[value.cli,'list','--raw'],31),
            ('preflight',[value.cli,'list','--raw'],30.0),
            ('preflight',tuple([value.cli,'list','--raw']),30),
            ('anything',[value.cli,'list','--raw'],30),
            ('runner',altered_runner,value.timeout),('runner',runner,value.timeout+1),
            ('runner',[value.cli,'list','--raw'],value.timeout),
            ('rescue-stop',[value.cli,'stop','--id','ffffffff-ffff-ffff-ffff-ffffffffffff','--raw'],120),
            ('rescue-stop',[value.cli,'stop','--id',self.request['sandboxId'],'--raw'],30),
            ('rescue-list',[value.cli,'list','--raw'],120)]
        constructor=unittest.mock.Mock()
        module=types.SimpleNamespace(ManagementJob=constructor)
        with patch.dict('sys.modules',{'auto_coding.windows_management_job':module}), \
                patch.object(management.subprocess,'Popen') as spawn:
            for label,argv,timeout in attempts:
                with self.subTest(label=label,argv=argv,timeout=timeout),self.assertRaises(ValueError):
                    value._execute(label,argv,timeout)
        constructor.assert_not_called();spawn.assert_not_called()

    def test_direct_execute_rejects_unbound_runner_and_missing_or_foreign_marker(self):
        value=self.session()
        with patch.object(management.subprocess,'Popen') as spawn:
            value.request_digest=None
            with self.assertRaises(ValueError):value._execute('runner',value.runner_argv(),value.timeout)
            value.bind()
            with self.assertRaises(OSError):value._execute('rescue-stop',[value.cli,'stop','--id',self.request['sandboxId'],'--raw'],120)
            (value.root/'launch-attempt.json').write_text(json.dumps({'runId':self.request['runId'],'sandboxId':'foreign'}))
            with self.assertRaises(ValueError):value._execute('rescue-list',[value.cli,'list','--raw'],30)
        spawn.assert_not_called()

    def test_execute_rechecks_snapshot_immediately_before_process_creation(self):
        value=self.session()
        class Job:
            processes={}
            def creation_flags(self):
                (value.scripts/'run.ps1').write_text('substituted after first authorization')
                return 0
            def close(self):pass
        module=types.SimpleNamespace(ManagementJob=lambda *args:Job())
        with patch.dict('sys.modules',{'auto_coding.windows_management_job':module}), \
                patch.object(management.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'snapshot changed'):
                value._execute('preflight',[value.cli,'list','--raw'],30)
        spawn.assert_not_called()

    def test_direct_runner_consumes_single_invocation_at_execute_boundary(self):
        value=self.session()
        with self.native_double() as spawn:
            self.assertEqual(value._execute('runner',value.runner_argv(),value.timeout).returncode,0)
            self.assertTrue(value.runner_started)
            with self.assertRaisesRegex(ValueError,'repeated'):
                value._execute('runner',value.runner_argv(),value.timeout)
            self.assertEqual(spawn.call_count,1)

    def test_execute_owns_validated_argv_against_caller_mutation(self):
        value=self.session();argv=[value.cli,'list','--raw']
        with self.native_double(on_flags=lambda:argv.__setitem__(0,'arbitrary.exe')) as spawn:
            self.assertEqual(value._execute('preflight',argv,30).returncode,0)
        self.assertEqual(spawn.call_args.args[0],[value.cli,'list','--raw'])


class ManagementLifecycleTests(unittest.TestCase):
    def test_real_powershell_pinned_cli_does_not_depend_on_path_alias(self):
        source=(management.CONTROLLER/'tools/windows_sandbox_poc/run.ps1').read_text()
        start=source.index("if ($Mode -eq 'Session' -and $request.trustedManagement)")
        block=source[start:source.index('function Invoke-Wsb',start)]
        if os.name!='nt':
            self.assertIn('else { $wsbExe = (Get-Command',block)
            return
        from auto_coding.process_runner import bounded_run
        with tempfile.TemporaryDirectory() as temporary:
            cli=Path(temporary)/'wsb.exe';cli.write_bytes(b'not executed')
            request={'trustedManagement':{'cli':str(cli),'lifetimePolicy':'authenticated-package-broker-only',
                'package':{'signatureVerified':True,'packagePayloadVerified':True}}}
            value=base64.b64encode(json.dumps(request).encode()).decode()
            script=base64.b64encode(block.encode()).decode()
            code=("$ErrorActionPreference='Stop'; function Get-Command {throw 'PATH lookup forbidden'}; $Mode='Session'; "
                f"$request=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{value}'))|ConvertFrom-Json; "
                f". ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{script}')))); "
                "Write-Output $wsbExe")
            result=bounded_run([str(management.system_root()/'System32/WindowsPowerShell/v1.0/powershell.exe'),
                '-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(code.encode('utf-16-le')).decode()],
                capture_output=True,shell=False,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),str(cli))

    def test_broker_authentication_rejects_name_only_forged_path_hash_and_foreign_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            image=Path(temporary)/'WindowsSandboxServer.exe';image.write_bytes(b'verified package payload')
            package={'image':str(image),'brokerSha256':hashlib.sha256(image.read_bytes()).hexdigest(),'packageFullName':'expected'}
            value={'pid':1,'createdFileTime':123,'jobMembershipVerified':True,'image':str(image),'packageFullName':'expected'}
            self.assertTrue(management.authenticated_broker(value,package))
            self.assertFalse(management.authenticated_broker(value|{'packageFullName':None},package))
            for change in ({'image':str(image.parent/'elsewhere'/image.name)}, {'packageFullName':'foreign'},
                           {'jobMembershipVerified':False}, {'createdFileTime':0}, {'pid':True}):
                self.assertFalse(management.authenticated_broker(value|change,package))
            image.write_bytes(b'substituted payload')
            self.assertFalse(management.authenticated_broker(value,package))

    def execute(self, *, survivors=None, running=False, capture_error=False, spawn_error=None, cancel_error=None):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        session=management.ManagementSession.__new__(management.ManagementSession)
        session.root=Path(temporary.name);session.scripts=session.root;session.sequence=0
        session.package={};session.environment={};session.snapshot={};session.request_digest=None
        session.cli=str(session.root/'wsb.exe')
        class Process:
            pid=7
            def __init__(self):
                self.stdout=io.BytesIO(b'{"WindowsSandboxEnvironments":[]}')
                self.stderr=io.BytesIO(b'');self.code=None if running else 0
                if capture_error:self.stdout.close()
            def poll(self):return self.code
            @property
            def returncode(self):return self.code
        process=Process()
        class Job:
            def __init__(self,*args):self.processes={};self.cancelled=False
            def creation_flags(self):return 0x00000004|0x08000000
            def assign_and_resume(self,child):self.child=child
            def survivors(self,pid):return survivors
            def cancel_transients(self,child):
                self.cancelled=True
                if cancel_error:raise cancel_error
                child.code=124
                return survivors is not None
            def close(self):pass
        job=Job()
        module=types.SimpleNamespace(ManagementJob=lambda *args:job)
        clock=management.time.monotonic
        with patch.dict('sys.modules',{'auto_coding.windows_management_job':module}), \
                patch.object(management.time,'monotonic',side_effect=itertools.count(0,31) if running else clock), \
                patch.object(management.subprocess,'Popen',return_value=process,side_effect=spawn_error):
            try:
                result=session._execute('preflight',[session.cli,'list','--raw'],30)
            except BaseException as exc:result=exc
        record=json.loads(next(session.root.glob('*.process.json')).read_text())
        return result,record,job

    def test_authenticated_persistent_broker_does_not_cause_false_timeout(self):
        result,record,job=self.execute(survivors=[{'pid':10,'authentication':'signed-msix-payload+owned-job'}])
        self.assertEqual(result.returncode,0)
        self.assertTrue(record['completeOutput'])
        self.assertEqual(record['authenticatedBrokerSurvivors'][0]['pid'],10)
        self.assertFalse(job.cancelled)

    def test_root_exit_does_not_hide_unfinished_or_unknown_descendants(self):
        result,record,job=self.execute(survivors=None)
        self.assertIsInstance(result,OSError)
        self.assertFalse(record['completed'])
        self.assertFalse(result.cleanup_confirmed)

    def test_timeout_and_broken_output_cannot_complete(self):
        for options in ({'running':True},{'capture_error':True}):
            result,record,job=self.execute(survivors=[],**options)
            self.assertIsInstance(result,(OSError,subprocess.TimeoutExpired))
            self.assertFalse(record['completed'])
            self.assertFalse(record['completeOutput'])

    def test_ambiguous_creation_error_is_not_confirmed_cleanup(self):
        result,record,job=self.execute(spawn_error=PermissionError('ambiguous launch'))
        self.assertFalse(result.cleanup_confirmed)
        self.assertIsNone(result.process_started)
        result,record,job=self.execute(spawn_error=FileNotFoundError('never launched'))
        self.assertTrue(result.cleanup_confirmed)
        self.assertIs(result.process_started,False)

    def test_cancellation_deadline_keeps_cleanup_uncertain(self):
        result,record,job=self.execute(survivors=[],running=True,cancel_error=subprocess.TimeoutExpired('cancel',5))
        self.assertIsInstance(result,subprocess.TimeoutExpired)
        self.assertFalse(result.cleanup_confirmed)
        self.assertFalse(record['cleanupConfirmed'])

    def test_native_job_policy_keeps_general_runner_and_breakaway_unchanged(self):
        source=(management.CONTROLLER/'src/auto_coding/windows_management_job.py').read_text()
        self.assertIn('limits = _ExtendedLimits()',source)
        self.assertNotIn('LimitFlags = 0x00000800',source)
        self.assertNotIn('TerminateJobObject',source)
        self.assertIn('jobMembershipVerified',source)
        self.assertIn('brokerSha256',source)
        original=(management.CONTROLLER/'src/auto_coding/windows_job.py').read_text()
        self.assertIn('0x00002000',original)

    def test_untrusted_caller_job_cannot_access_management_separation(self):
        for flags in (0,0x2000,0x1000):
            with self.subTest(flags=flags),self.assertRaises(ValueError):
                management.management_creation_flags(True,flags)
        self.assertFalse(management.management_creation_flags(False,0)&0x01000000)
        self.assertTrue(management.management_creation_flags(True,0x2800)&0x01000000)

    def test_actual_strict_windows_job_denies_the_management_creation_path(self):
        if os.name!='nt':
            with self.assertRaises(ValueError):management.management_creation_flags(True,0x2000)
            return
        import sys
        from auto_coding.process_runner import bounded_run
        code=('from auto_coding.windows_management_job import ManagementJob; '
              'j=ManagementJob.__new__(ManagementJob); '
              'j.creation_flags()')
        result=bounded_run([sys.executable,'-B','-c',code],capture_output=True,shell=False,timeout=15)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Caller Job does not authorize',result.stderr)
