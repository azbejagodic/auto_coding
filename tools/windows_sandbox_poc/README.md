# Windows Sandbox: Smoke and Baseline145

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
process termination cannot guarantee normal cleanup. This implements only Phase 3:
no source-integrity feature, UI03, dashboard integration or backend replacement.
