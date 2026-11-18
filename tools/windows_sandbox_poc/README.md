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

Batch 1 remains experimental. No protected before/after source manifest, UI03,
dashboard integration, dependency removal, or production cutover is implemented.
