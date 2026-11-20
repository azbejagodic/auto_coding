# Windows Sandbox: Smoke, Baseline145 and controller Session

Run from `C:\Users\HP\Desktop\auto_coding` in host PowerShell:

```powershell
$pocSource = Join-Path (Get-Location) 'tools\windows_sandbox_poc'
& ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $pocSource 'run.ps1')))) -SourceDirectory $pocSource -Mode Smoke
```

For the six-suite baseline, use the same command with `-Mode Baseline145`.
The default is `Smoke`. These entry points work with Restricted script execution
policy. The guest receives an encoded command. No policies or security protections
are changed. Requires the existing Windows Sandbox and `wsb` CLI (0.8.114.0).
No downloads, installations, networking, or interactive guest commands.

## Inputs and guest preparation

Smoke preserves the Phase 2 arithmetic and child-process exit 0/23 probes,
with evidence under `AutoCodingSandboxRepro\Phase2POC\<run-id>`.
Baseline145 inputs and results are under
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase3Runs\<run-id>`.

Baseline145 uses host `git archive --format=zip --output <run-input>\baseline.zip
84c3bd826aadad24a39c4739c39414bf836373cd`, never the working directory.
Existing Git, Python ZIP and Node executable hashes are checked against saved
tool hashes. This is not a source SHA-256 integrity feature.

| Host path (tool paths relative to AutoCodingSandboxRepro) | Guest path | Access |
|---|---|---|
| Per-run input: scripts, baseline.zip, python.zip | `C:\ACSetup` | Read-only |
| Per-run results | `C:\ACResults` | Writable |
| `GitHubDesktop-3.6.5-x64-full\lib\net45\resources\app\git` | `C:\ACGit` | Read-only |
| `node-v26.13.1-win-x64\node-v26.13.1-win-x64` | `C:\ACNode` | Read-only |

The complete Git and Node distributions are reused in place. Only the existing
Python ZIP and fresh source archive are copied per run. There is no live repository
mapping. Networking, vGPU, clipboard, audio/video input and printer redirection
are disabled. The old policy-changing LogonCommand is omitted. Saved evidence
is untouched.

`baseline.ps1` extracts source to `C:\AutoCodingTest`, extracts Python to
`C:\Python313Embed`, and renames `python313._pth` to `python313._pth.bak`.
It sets `PYTHONPATH=C:\AutoCodingTest\src`, and adds Git, Node and Python to PATH.
It sets TEMP and TMP to a newly created `C:\AutoCodingTemp` in the guest.
SYSTEM's inherited `C:\Windows\TEMP` spelling differs from the resolved
`C:\Windows\Temp`, causing three unchanged bridge tests to fail string comparisons
in the diagnostic run. The dedicated directory preserves consistent path casing.
It requires Git `2.53.0.windows.4`, Python `3.13.17`, Node `v26.13.1`.
It initializes a disposable repository, sets local identity only, commits the
source and verifies a clean initial worktree. Source and disposable commits are
recorded separately. No global Git configuration or security exceptions are set.

Using `wsb exec --id <returned-id> --run-as System --command <command> --raw`,
the guest runs these sequentially from `C:\AutoCodingTest`:

```text
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_desktop_ui.py -v
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_dashboard_acceptance.py -v
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_desktop_bridge.py -v
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_desktop_control.py -v
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_plan_audit.py -v
C:\Python313Embed\python.exe -m unittest discover -s tests -p test_isolated_workspace.py -v
```

Expected counts: 4, 7, 39, 37, 40, 18; total 145. No tests are modified or skipped.
Tool setup failure stops before tests. A timeout aborts further suites; ordinary
test failures are saved while remaining suites execute. No agents run in the guest.

## Deadlines and cleanup

`common.ps1` bounds every native call and saves separate raw stdout/stderr and
exact command/process metadata. Defaults:

| Operation | Deadline |
|---|---:|
| Source archive | 120 seconds |
| CLI help/version/list and smoke exec | 60 seconds per call |
| Sandbox launch | 180 seconds |
| Guest setup native command | 120 seconds, within overall budget |
| Each suite | 900 seconds |
| Complete guest baseline, including preparation | 3600 seconds |
| Host baseline exec | Overall budget plus 30 seconds |
| Final result transfer | 60 seconds |
| Sandbox stop / final list | 120 / 30 seconds |

Parameters: `-SuiteTimeoutSeconds`, `-OverallTimeoutSeconds`,
`-ResultTimeoutSeconds`. Each suite is capped by the remaining overall budget.
Guest archive extraction is bounded by the host exec deadline. Termination and
output draining have bounded five-second grace waits. A timeout always fails.

Launch passes XML contents to `start --config` and parses the returned `Id`.
Cleanup in `finally` stops only the fresh ID reserved for this launch, even after
an unreadable/timed-out launch response. It attempts `list --raw` after stop,
including stop errors, and requires absence. Stop errors still fail the run.
No pre-existing Sandbox or unrelated host process is stopped. Suite timeouts
kill the direct process; any guest descendants disappear at Sandbox shutdown.
Forced host termination or machine shutdown can bypass cleanup; inspect the
recorded Sandbox ID after interruption.

## Evidence and acceptance

Each run saves CLI help, configuration XML, exact CLI arguments, raw streams,
host exits and timeout flags. Suite files are `*.stdout.log`, `*.stderr.log`,
`*.process.json` (command, cwd, timestamps, exit, timeout) and `*.suite.json`
(counts/status). `results\baseline.json` is published atomically by the guest.
The host atomically publishes `summary.json` after cleanup with the guest result,
independently validated counts, both commits, account/versions and cleanup.
Guest metadata paths under `C:\ACResults` correspond to the host results folder.

PASS requires all six suites and exact counts, zero failures/errors/skips,
clean unittest footers, exit 0, no timeouts, all logs, matching run/source/Sandbox
identities, coherent timestamps and metadata, agreement between CLI and guest
status, and verified cleanup. The host reparses raw unittest logs and compares
them with both per-suite metadata and final guest summary. CLI host exit 0 alone
never establishes success. Missing, malformed, stale, failed or contradictory
evidence fails. Exit 0 means the complete proof passed; exit 1 means failure.
Early failed diagnostic runs are preserved.

Mapped-folder evidence is guest reported, not cryptographic attestation. Forced
process termination cannot guarantee normal cleanup. Smoke/Baseline145 remain
standalone regression modes; neither substitutes for testing a session output.

## Phase 4 Batch 1: controller Session mode

The experimental adapter in `src/auto_coding/windows_sandbox.py` invokes this same
runner as `-Mode Session -RequestPath <external-run-folder>\request.json`.
Do not hand-author session requests to bypass controller approvals. Select this
mode through the operator-only `AUTOCODING_FINAL_TEST_BACKEND=windows-sandbox`
setting before creating a normal approved workflow; MXC remains the default.

The controller's existing final-verification loop supplies the exact approved
HEAD/branch, full normalized policy, suite index, invocation and session run ID.
The adapter reads the commit's regular blobs into `input\baseline.zip`; this
filename is retained for compatibility, but its contents are the session commit.
Git filters/export attributes cannot change the package. Sensitive names, links,
submodules, dirty/stale source and unsupported host command paths fail closed.
The live checkout and Git metadata are never mapped. Session source-object capture
is bounded by the existing 16 MiB process-output limit. There is no fallback to
the historical Phase 3 source or to a different backend after failure.

Each required command gets one fresh Sandbox and the same offline setup. This
preserves per-suite controller events, safety gates, failure handling and resume.
Commands are argv arrays, never concatenated shell strings. Python aliases and the
controller interpreter use `C:\Python313Embed\python.exe`; source paths relocate
to `C:\AutoCodingTest`. Other required commands run with the offline guest PATH;
missing dependencies/tools fail. Final evidence still requires actual unittest
summaries. Runtime settings retain the existing non-secret allowlist, source-local
imports relocate, and config/temp directories are guest-local. Host PATH, home,
Git configuration, credentials and unrelated directories are never copied.

The existing policy defines per-command timeout and combined minimum coverage,
not exact per-suite counts. Session mode runs its selected command with that
timeout (30..86400 seconds), allows 300 additional seconds for guest preparation,
and bounds CLI exec to that total plus 30 seconds. Other CLI deadlines are as
above. The adapter has a final outer bound of command timeout plus 1500 seconds,
covering launch/setup/CLI/cleanup budgets. A killed host wrapper triggers an
exact-ID stop/list attempt using its host-owned atomic launch marker. Unverified
guest or host-process cleanup retains the controller's existing uncertainty gate.
Forced machine or controller termination can still interrupt cleanup.

Request, inputs, original guest stdout/stderr, CLI host exits, CLI-reported guest
exits, process metadata, `results\baseline.json`, `summary.json`, and adapter
streams remain under the normal session logs in `windows-sandbox-<uuid>`.
Host receipts remain outside the writable guest results mapping. Existing
`final-verification-NNNN-test-N.txt` artifacts and output hashes are retained.
The Python host reparses suite logs and compares command, source/policy identity,
counts, timestamps, exits and cleanup; cached results require revalidation too.
Ordinary test exits, skips, timeouts, launch errors and malformed evidence cannot
authorize completion. The controller enforces the combined policy minimum only
after every required suite passes. Overall guest exit and CLI JSON must agree;
a native host CLI exit of 0 is insufficient.

Session mode remains experimental. Batch 2 adds the integrity contract below;
UI03, dashboard integration, dependency removal and production cutover remain excluded.

## Batch 2: protected SHA-256 source inventories

Only integrated Session mode qualifies for this contract. Smoke/Baseline145 remain
standalone diagnostics; their PASS is not a final-verification integrity result.

The Python adapter builds `source-baseline.json` while packaging exact committed
Git blobs. This authoritative file is at the receipt root, alongside `request.json`,
outside the writable `results` mapping. `input/source-baseline.json` is a read-only
copy; expected hashes are never loaded from a guest-writable results file.
The host compares every ZIP entry against its baseline before Sandbox launch and
again when validating the receipt. Archive names and commit comments alone do
not establish byte identity. No source is read from the live working tree.

Baseline schema v1 has `kind: source-baseline`, `schemaVersion: 1`, `identity`,
`exclusions`, `files`, `fileCount` and `totalBytes`. Identity binds source commit,
branch, session run ID, per-Sandbox run/owned ID, invocation, suite index and policy
SHA-256. Each sorted file record contains exactly `path`, integer `size`, and
lowercase 64-digit `sha256`. Canonical ASCII JSON with sorted keys determines the
baseline SHA-256 recorded in the host request. Enumeration order never changes
the manifest for the same identity. All tracked regular files are included,
including newly committed source, tests, scripts and configuration. Sensitive
source paths retain existing packaging restrictions. Traversal, links/reparse
points, submodules, duplicate/case-aliased paths, non-NFC paths, Windows reserved
names and file/directory identity conflicts fail closed.

After disposable Git setup, `baseline.ps1` starts the read-only trusted
`source_integrity.py` using the offline Python with `-I -B`. It cannot import
target modules via PYTHONPATH. A failed pre-inventory throws before any test
command. A post-inventory runs in the guest's final cleanup path after testing;
failure or missing evidence forces overall failure. Each worker has a 120-second
maximum, capped by the existing guest overall deadline. Existing launch, test,
transfer, shutdown and outer deadlines still apply. Timeout never implies PASS.

Observation files `results/integrity-pre.json` and `integrity-post.json` contain
the full sorted inventory, identity, baseline fingerprint, coverage counts,
explicit exclusions, timestamps, completion/error fields, computed differences
and matching verdict. Worker commands/exits/raw streams are preserved as
`integrity-<stage>-hash.process.json`, `.stdout.log` and `.stderr.log`.
The host reparses records, rejects malformed/truncated/duplicate/stale evidence,
checks worker execution and stage ordering, and independently recomputes all
comparisons. `integrity-verification.json` distinguishes the verified host package
from guest observations. Its verdict is included as `source_integrity` in the
existing final-test state entry and revalidated on reuse. Legacy Batch 1 receipts
without this contract cannot authorize a new PASS.

Exclusion policy v1 is fixed, not selected by a plan or guest:

- Root `.git/` metadata created for the disposable repository; links at that root
  are still rejected. Nested Git directories are not excluded.
- Untracked `__pycache__/<name>.cpython-<digits>[.opt-<digits>].pyc` files only.
  A matching file already present in the baseline remains protected.

`.gitignore` rules do not authorize exclusions. Ordinary `.py`, logs, temporary
files, `.pytest_cache` and other new outputs are unexpected source additions.
Use the existing guest-local config/temp paths or results mapping for outputs.
Empty directories are not Git-tracked protected files; inventories protect files.

The guest checks file identity/size/mtime across hashing and ctime within each
stat API; Windows lstat and fstat expose different ctime semantics. A changing or
unreadable file fails the scan. A comparison mismatch records added/deleted/
modified paths. Renames appear as deletion plus addition. Test exit 0 and `OK`
cannot override a failed integrity verdict: the controller stops with
`source_integrity_failure`, retains checkpoints and never auto-repairs source.

This is cooperative verification. The host baseline and package are independently
verifiable; the host cannot independently inspect post-test guest files after
shutdown. A malicious SYSTEM guest can forge observations or change files after
the observation point. No attestation or adversarial resistance is claimed.
Batch 3 exercised these limits and interruption/recovery. Complete repository CI
has qualification blockers: the replacement is **NOT QUALIFIED**. The old tester
remains intact, MXC remains the default, and Windows Sandbox remains experimental.

## Batch 3 hardening and qualification

The controller now binds reused guest argv to the selected approved policy command
and its source-repository translation. Validation rejects contradictory successful
receipts containing host/guest errors, boolean deadline metadata, and cleanup
records whose argv does not stop precisely the requested owned ID and then list
instances. Deterministic regressions reproduce these acceptance defects.

One real failure fixture exposed Windows clock-provider rounding: a Python hash
worker's observation finished 0.6903 ms after its enclosing .NET process timestamp.
Cross-provider observation/worker bounds allow at most 20 ms of rounding. Native
worker/test ordering and each provider's own start/end ordering remain strict;
the tolerance does not permit a worker to overlap a test. Regression coverage
accepts 1 ms and rejects 50 ms. Original failed receipts remain intact, and a
normal controller resume produced the correct test-failure decision.

Raw process streams permit read sharing for bounded progress inspection. No
deadline or process-containment check was removed. Real cases verified a 30-second
guest deadline and an intentional stop of only the fixture's owned Sandbox;
startup and foreign-instance rescue failure paths used launcher doubles. Hard
host termination and machine shutdown were not deliberately exercised: either
can prevent cleanup/receipt completion, requiring recovery of the recorded owned
ID. Incomplete evidence cannot authorize PASS.

The complete repository CI command used a 3,600-second deadline from the CI policy,
without removing tests or allowing skips at the controller gate. Two existing
desktop-reader assertions fail against unchanged dashboard HTML. The final-code
snapshot discovers 735 tests; focused adapter/integrity/acceptance checks pass
all 60 cases. Exact full-run counts, failures, cleanup records, matrix and explicit
real-versus-mocked methods are in the external Phase4Batch3 evidence directory
identified by `C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Batch3\run-root.txt`.
This qualification does not claim other OS/Python CI matrix cells or malicious
guest attestation, and does not authorize old-tester removal or production cutover.

The complete host run finished all 734 cases from its earlier snapshot: 723 passed,
8 failed, 1 error, 2 skipped; one skip violates the existing CI skip policy. The
735-case real Sandbox attempt timed out at the original 3,600-second deadline.
The controller stopped with `infrastructure_timeout`, retained the approved commit
and confirmed owned cleanup. Guest unittest totals remain unknown without a final
summary; partial progress does not establish mandatory coverage.
All 19 owned receipts retain original stop/list cleanup proof. Two later
independent `wsb list --raw` queries timed out after 30 seconds each, so current
inventory confirmation is BLOCKED. Host CLI termination is not Sandbox cleanup
proof. No further stop or recovery action was attempted.


## Batch 3 remediation

Remediation is **BLOCKED** by host prerequisites, with historical Batch 3 results
retained above. The nine previously failing/error tests are corrected: obsolete
desktop assertions now exercise the existing single-panel UI and explicit log
export, managed-start fixtures obtain real JSON audit authorization, and native
confirmation again identifies the managed GitHub repository. No dashboard redesign
or authorization bypass was introduced. The adjacent spawn-failure test now proves
that its mocked child launch is actually reached.

Targeted validation passes all six desktop-reader and four managed-start/spawn
cases. Relevant integrity, controller, process, retained MXC and desktop regressions
pass all 182 cases without failures, errors or skips. Complete host CI attempt:
no complete unittest totals; the bounded attempt did not complete; exit 124, original 3,600-second deadline retained.

Real symlink creation fails with WinError 1314 because this token lacks the required
privilege; both real-link tests remain skipped, without accepting an unexpected
skip as PASS. A bounded inventory diagnostic finds that the CLI root exits 0, but
WindowsSandboxServer.exe remains active in its owned Job and prevents quiescence.
Partial empty-inventory stdout is not accepted. No security policy or containment
exception was added, and no new Sandbox was launched, stopped or reclaimed.

The original full guest run completed a continuing test prefix before its deadline,
while host and guest full CI overlapped for about 44.5 minutes. Its retained logs
cannot establish per-test guest latency or justify a larger deadline. Current host
per-case timing and a read-only Git measurement are retained separately.

The twelve-part remediation report and exact evidence are in
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Batch3\remediation-b12194e241a2464d85c8f2ce4c28c14b` (`remediation-report.md` and `remediation-report.json`).
MXC remains default, the old tester remains intact, Windows Sandbox stays
experimental and opt-in, and no Batch 4 cleanup or production cutover occurred.

## Approved session coverage

The controller's Windows final stage uses `executionScope: approved-session-v2`.
It calls the existing Session runner once with all commands, starts one owned VM,
executes the commands sequentially in the same disposable repository, and stops
that VM once. `suiteIndex: 1` is the legacy integrity schema's execution slot;
individual results are `session-suite-1` through `session-suite-N`. Older receipts
remain readable through the legacy API but cannot satisfy a new grouped session.

Example policy fragment for a hypothetical feature (choose actual suites and
counts for the repository before obtaining approval):

```json
{
  "test_commands": [
    ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_feature.py"],
    ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_feature_security.py"]
  ],
  "minimum_tests": 7,
  "timeout_seconds": 300,
  "session_coverage": {
    "rules": [
      {
        "id": "feature-and-callers",
        "paths": ["src/feature.py", "src/feature_caller.py", "tests/test_feature.py", "tests/test_feature_security.py"],
        "suite_indices": [1, 2],
        "reason": "Feature, affected caller regression, added tests and authorization boundary"
      }
    ],
    "minimum_tests_per_suite": [4, 3],
    "mandatory_security_suites": [2]
  }
}
```

Rules use case-sensitive `fnmatchcase` patterns on safe repository-relative POSIX
paths. `*` also matches `/`; these are not Git pathspecs. Prefer exact paths or
narrow reviewed patterns. Each rule requires a unique id, paths, one-based suite
indices and a coverage reason. The mapping must explicitly justify changed tests,
affected dependencies, integration/regression coverage and security coverage where
applicable. It does not automatically discover dependency relationships.

Every command in `test_commands` is mandatory. Rules never filter that list;
`mandatory_security_suites` records explicitly required security entries. Each
suite must meet its own positive minimum and the minima must cover the combined
minimum. Missing rules for any checkpoint or net-change path stop execution before
launch. Changing the declaration changes the approved policy/plan identity and
requires the normal approval process. A previously approved full-CI command is
retained; there is no automatic full-CI fallback. Manual full CI remains
`python tests/ci_unittest.py`.

`timeout_seconds` remains the per-command deadline. The overall guest deadline is
N times that value plus 300 seconds; the host launcher gets N times it plus 1500
seconds for setup, evidence and cleanup. A combined command budget above 86400
seconds is rejected before launch, rather than shortened. Existing bounded-process
containment, offline tool hashes, disabled networking and owned-id cleanup remain.

The canonical `final-verification-NNNN-selection.json` records initial/final commits,
all approved checkpoint deltas, net changes, matched paths/rules, mandatory suite
indices and per-suite minima. Its SHA-256 is echoed through the request and guest
result. All suite outputs reference the same receipt and pre/post integrity proof.
Reuse rechecks the current approved checkpoint chain, audit identity, selection,
source/policy identities, every output and owned cleanup. One synthetic launcher
call in tests proves orchestration only; real VM acceptance still requires a safe
bounded inventory and a successful real run. Partial inventory stdout never
satisfies that prerequisite.

For this session-scoped implementation, the strict 30-second inventory attempt
remained unconfirmed: the root CLI exited 0 but its contained
`WindowsSandboxServer.exe` child stayed active through the 28-second sample.
The bounded runner confirmed cleanup of its owned process job; no VM run was
attempted. No broker exemption or Windows security setting was introduced.
See `C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4SessionScoped-0b808c6d4ecb4daa90aa4a542d39f4f4\session-report.md`
for focused results, selection/approval proofs, final Git status and limitations.

## Cold-start candidate: blocked, diagnostic only

`broker.ps1` and `auto_coding.sandbox_broker` are an isolated diagnostic candidate,
not part of automatic final verification. They use registered Microsoft package
activation with bounded strict Job-owned helper/readiness calls (45/30 seconds).
No target-selected command, profile or environment enters activation. Package
identity, protected installation path, Microsoft signature on `AppxSignature.p7x`,
native `CreateValidatedBlockMapReader` validation, every payload's signed block
hashes, supported whole-file hashes, and returned PID/package/session are checked.
The executables are covered by the MSIX signature; they have no individual
Authenticode signature. `-ValidateOnly` performs no activation.

The actual package-only check passed all 835 payload files. Cold activation
returned PID 20500; the following strict inventory created a different broker,
PID 18012, which remained in the CLI's Job through the 30-second sample. Inventory
timed out with owned-job cleanup confirmed. Partial empty output is retained only
as incomplete diagnostics. The cause of the activated service's failure to serve
the CLI has not been established; registered activation alone is insufficient.

No instance was started. The two-task fixture's approved 2-test and 3-test commands
were selected but not executed. Host packaging matched approved final source;
guest pre/post SHA evidence does not exist. The unqualified integration was removed
from the automatic path; existing session-scoped verification and MXC are preserved.
Full repository CI was not run. No more speculative activation/VM attempts follow
this failed readiness proof.

Evidence and twelve-part **BLOCKED** report:
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Broker-8dbd484fec7b447498dd87cbd8b8d365\broker-report.txt`.

API contracts: [package activation](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-iapplicationactivationmanager-activateapplication),
[signed block-map validation](https://learn.microsoft.com/en-us/windows/win32/api/appxpackaging/nf-appxpackaging-iappxfactory-createvalidatedblockmapreader),
and [Job process ownership](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## Sealed management path: real session acceptance

The current Windows controller uses `trusted_sandbox_management.py` with
`windows_management_job.py` for host management only. The general-purpose process
runner and its kill-on-close Jobs are unchanged. The Phase 3 Session runner still
packages/executes all selected commands in one owned VM and validates its existing
evidence protocol. The only runner change selects the validated package CLI path
directly, without depending on a user PATH alias.

Six controller files have explicit reviewed SHA-256 pins: run/common/baseline/guest
scripts, the read-only package validator, and the integrity worker. UTF-8 BOM/CRLF
normalization is explicit. A private host snapshot outside the target is checked
again before each operation, together with the immutable request hash. Only this
snapshot is used; approved source archives remain guest data. Pin updates require
reviewed controller deployment, not selection from session output.

The fixed system PowerShell image is resolved through the Windows API and its
Microsoft signature checked. `broker.ps1 -ValidateOnly` validates the installed
Microsoft package signature, signature-bound block map and every payload hash;
it does not call the experimental AppService activation. Command arguments and
working directories are sealed. Rescue is limited to the marker's owned UUID;
inventories are read-only and malformed/duplicate IDs are rejected.

The host here inherits Job flags `0x2800` (kill-on-close plus explicit breakaway).
Only the sealed root uses that existing permission to leave the ancestor, suspended,
and is attached to a separate tracking Job before resume. No flags on an existing
Job or machine policy change. The new Job has no descendant-breakaway flags.
Normal untrusted Jobs (`0x2000`) cannot use this route; a native regression proves
that denial. Direct command deadlines and complete capture still apply. Broker
survival is authorized only after exact signed-package/image/hash, package-token,
retained-handle/creation-time and owned-Job checks. Other survivors reject completion.
Closing this tracking Job does not terminate the shared broker or foreign VMs.
Cancellation targets owned command handles, then the existing adapter stops only
its marked VM and confirms absence. Any uncertainty keeps verification failed.

Cold management preflight completed in 0.76 seconds with a verified persistent
broker instead of a false 30-second timeout. The first runner exited before VM
launch because it still queried the alias; this was fixed and tested with alias
lookup deliberately forbidden. The same approved fixture resumed without agent
replay and started exactly one instance, `82b8e924-2c54-492e-bf18-d3ec46ce1de9`.
Both suites passed (2 and 3 tests, exits 0); host/guest exits, five protected source
files, SHA pre/post scans, stop, after-inventory and fresh independent inventory
passed. Evidence reuse also passed. This is controlled integration acceptance,
not broad production qualification or cutover. Full repository CI was not run.

Evidence: `C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Management-d8981e7e838649bb94a25a5de8181b55\management-report.txt`.
