# Auto-Coding — JSON Plan Reference

**Purpose:** This file is for writing new Auto-Coding plans against the **existing backend**. It is not a setup guide or development history. Check the current source before relying on a capability; update this file when the backend changes.

## Existing backend — reuse it

| Capability | Existing implementation |
| --- | --- |
| JSON task loading, A with focused tests → B, corrections, budgets, local Git checkpoints | `src/auto_coding/simple_run.py` |
| One-time, read-only plan audit and approval binding | `src/auto_coding/plan_audit.py` |
| Windows MXC controller-run tests and structured launch errors | `src/auto_coding/controller_test_sandbox.py` |
| Sanitized Python/test environment | `src/auto_coding/python_environment.py` |
| Process limits, partial failure output and cleanup protection | `src/auto_coding/process_runner.py` |
| GitHub workspace management and publication safeguards | `src/auto_coding/github_workspace.py` |
| Existing desktop UI and bridge | `src/auto_coding/desktop_app.py`, `desktop_control.py`, `desktop_reader.py` |
| Usage/event records and final-suite verification | `src/auto_coding/metrics.py`, `event_log.py`, `final_verification.py` |

**Plan rule:** Inspect relevant source and tests first. Extend existing functionality; don't ask Codex A to rebuild the runner, audit, review, Git management, or desktop bridge. A implements code and runs the configured focused tests on the controller-selected branch; the **controller** owns B review, commits, recovery and the final verification suite. New workflows require this mode; saved workflows retain their recorded testing mode. No Git branch creation, resets or commits inside task prompts.

For new workflows, Codex B reviews code and task requirements without executing tests, linting or builds, or demanding optional coverage. It may read tests and A's reported results as context; those remain implementer claims. A code review can approve with `verification.status = "not_run"`. Concrete defects still block approval, and the controller's final suite remains required. Historical controller-tested workflows retain their verification gates.

## Accepted JSON shape

```json
{
  "tasks": [
    {
      "id": "v01",
      "prompt": "A narrowly scoped feature with exact acceptance criteria, existing modules to reuse, exclusions and meaningful test expectations.",
      "test_commands": [["python", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_example.py"]]
    }
  ],
  "final_verification": {
    "test_commands": [["python", "-B", "tests/ci_unittest.py"]],
    "minimum_tests": 1,
    "timeout_seconds": 7200
  }
}
```

This is a **shape example**, not a ready-to-run plan: replace the example test commands and `minimum_tests` with real evidence-backed project values.

- Top level: `tasks` and `final_verification` (required for new approved JSON runs); **1–30 tasks**; JSON file at most **128 KiB** for audit (**512 KiB** loader limit).
- Each task: unique `id`, nonempty `prompt`, and meaningful `test_commands` for Codex A to execute and report. Commands are arrays of arguments, **not shell strings**; max **10 commands per task**, max **32 arguments per command**.
- `final_verification`: `test_commands` (1–10), `minimum_tests` (positive integer), optional `timeout_seconds` (30–86400).
- **Current limitation:** final verification recognizes **Python `unittest` summaries**, requires enough tests and no skips. Arbitrary build/test commands can be used for focused tests, but other languages need compatible, genuinely meaningful final verification before an autonomous run.

## Actual controller-test environment

- On **Windows MXC**: target checkout **read**, Windows system directory **read**, selected Python runtime **read**, dedicated external scratch **write**; other roots **denied by default**, network **off**.
- Tests use a sanitized environment. They cannot assume the host/VS Code `PATH`, credentials, unrestricted temporary directories, compiler caches or network access.
- A tool being installed does **not** prove it can launch inside MXC. Verify **both the executable and its nested dependencies**, plus permitted output/cache directories, before making a test mandatory.
- Observed in a prior dashboard run: Node absent from test `PATH`; bundled Node denied access to `C:\`; headless Edge crashed. These are **configuration-specific observations**, not universal tool bans.
- Python `unittest` has been exercised under MXC. C/C++, Rust, Go, Java, .NET and other toolchains are **not yet verified** by this document.
- Confirmed sandbox preflight/test-command launch blockers stop with durable diagnostics. Historical controller-managed focused test failures return to A; final-suite failures stop without automatic repair. **Nested tool failures may still look like ordinary nonzero test exits**. Never assume the controller detects every permission problem.

## Final verification before a JavaScript/browser experiment

- The final gate accepts real Python `unittest` evidence: the **last** `Ran N tests in ...s` summary must be successful, each configured command must run at least one test with **zero skips**, and the combined count must meet `minimum_tests`. Exit code 0, `OK` alone, npm/Node success messages, TAP, Jest/Vitest and Playwright reports are not sufficient by themselves. Never print fabricated Python summaries to make another tool appear supported.
- A genuine `unittest` integration test may invoke Node or a browser and assert its actual behavior and subprocess results. Its count represents Python test cases, not every nested JavaScript/browser case. Dependencies, failures and timeouts must propagate; optional skips do not satisfy the final application's gate.
- Final commands run against the approved **read-only** checkout in the existing Windows MXC sandbox, with network disabled and external writable scratch (`TEMP`/`TMP`/`TMPDIR`). Builds or test tools that write into the checkout, download packages/browsers, or need undeclared installation/cache/profile paths cannot assume they will work. Operator-provisioned toolchains can use the existing validated `AUTOCODING_TEST_TOOLCHAIN_DIRS_JSON` read grants; these are not writable roots or a permission bypass.
- The deterministic batch regression runs real Python focused/final tests through the injected-executor path. Host Node/UI tests and mocked sandbox-policy checks do **not** qualify Node or browser automation inside live MXC. The upcoming dashboard's JavaScript/browser final verification remains **unvalidated** until its exact preinstalled toolchain, nested dependencies and scratch paths have been exercised inside that sandbox. No sandbox restrictions were broadened for S3.
- Final verification checks HEAD, branch, plan hash, policy hash, output hashes, process cleanup and a clean worktree before completion or reuse. A failing test, unavailable tool, sandbox error, incomplete evidence or a skip stops the run and preserves approved checkpoints and diagnostics. There is no automatic final-failure repair task or model loop. Explicit resume can retry final verification without replaying completed tasks; a code correction needs an externally prepared, separately audited workflow. Uncertain process cleanup continues to block execution/publication pending inspection.
- The backend CI suite permits a documented Windows symlink skip (and Windows-only cases on other platforms); this exception does **not** relax the final application's zero-skip gate.

## Checklist before creating and auditing a plan

- [ ] Inspect the current backend functions and tests; identify **implemented vs missing** functionality.
- [ ] Break the goal into small ordered tasks, each with acceptance criteria and explicit non-goals.
- [ ] Make tests prove the task's required behavior; no silent skips or static-only substitutes for mandatory browser/UI behavior.
- [ ] Validate required tools **inside the same MXC controller-test mode** before spending A/B calls. If unverified, change the verification strategy or prepare a suitable isolated environment first.
- [ ] Use safe test commands, disposable outputs and the existing controller workflow; don't broaden MXC permissions in a feature task.
- [ ] Choose a real final suite and coverage minimum; check the `unittest` evidence restriction.
- [ ] Keep JSON, audit files and run logs **outside** the target checkout. The target must be a clean feature branch; don't use `main`, `master` or detached HEAD.
- [ ] Audit the **exact JSON bytes and target Git state** before starting. Revised plans require a fresh audit. Ordinary resume cannot swap plans, reset exhausted budgets or bypass safety stops.
- [ ] Keep A/B calls and token limits bounded. A failed run retains its evidence and any approved checkpoints; don't erase it to hide the failure.

**Source of truth:** `simple_run.py` (JSON validation / execution), `controller_test_sandbox.py` (permissions), `final_verification.py` (completion evidence), and their tests. This README is a planning aid, **not** a verified inventory of every installed tool.

The desktop **Stop after task** control sends a cooperative request to the existing
runner, including runs started from PowerShell: open that run's external logs in
the dashboard and select **Stop after task**. **Stop requested** means the request
is persisted; executing A/B calls, corrections, and safe checkpoint handling may
finish first. The runner records `STOPPED` before starting the next task or final
verification. It never kills the runner or its subprocesses. Closing the window
leaves the request effective. Reopening shows the durable request and outcome.

Resume with the original CLI arguments plus `--resume`, or use desktop Resume.
Existing plan/audit, process ownership, and Git checkpoint reconciliation still
apply. After successful reconciliation, an explicitly resumed cooperative stop is
acknowledged in saved state and the timeline; `stop-request-*.json` remains in the
external run directory. Resume never repeats the plan audit. Uncertain execution
or cleanup continues to block resume.

## Experimental Windows Sandbox final verification (Phase 4 Batch 1)

The existing MXC tester remains intact and is the default. An operator may
explicitly select the experimental Phase 3 Windows Sandbox mechanism for the
mandatory end-of-session final gate:

```powershell
$env:AUTOCODING_FINAL_TEST_BACKEND = 'windows-sandbox'
# Run the normal audit/start workflow with its approved plan and final policy.
```

This setting is operator-controlled, cannot be supplied by a plan, and is saved
in state and the run manifest. Resume retains that selection; an explicit attempt
to change it is rejected. Unset it before creating a new default/MXC workflow.
The approval, feature-branch, clean-worktree, checkpoint, process-safety, policy,
coverage, evidence and recovery gates remain required. Focused tests and the old
tester are unchanged; there is no fallback to MXC or the historical baseline
when an experimental final test fails.

The final gate packages the current approved session commit from committed Git
blobs, avoiding working-tree files, credentials, links, submodules, checkout
filters and export rewriting. Each policy command receives a fresh disposable
Sandbox, preserving the existing per-suite process gate and fail-fast/resume
semantics. Original argv and policy are recorded; source-relative absolute paths
and the controller Python executable are relocated into the guest. Standard
Python aliases use the verified offline Python. Required tools/dependencies must
already exist in the source or offline environment; nothing is downloaded.

Original streams, native exit codes, timeouts, source/branch/policy identities,
owned IDs and verified cleanup are retained in `windows-sandbox-<uuid>` folders
inside the normal external session logs. Existing final evidence filenames and
output hashes are preserved. Cached successful Windows Sandbox results also
require their original receipt and logs to validate again.

This is **not a production cutover**. The guest runs as SYSTEM against a writable
disposable copy with network disabled and read-only input/tool mappings. Results
are guest-reported evidence, not attestation. Batch 2 now requires protected
host SHA-256 baselines and complete pre/post inventories (see below).
Source-object capture currently has the existing
16 MiB process-output bound. Fixed offline tool paths are specific to this machine.
See [runner instructions](tools/windows_sandbox_poc/README.md) for deadlines,
evidence and interruption limits. No UI03 or dashboard integration is included.

## Batch 2 source integrity

Windows Sandbox final verification now requires a versioned authoritative
`source-baseline.json` generated from the approved commit's exact blobs. It stays
in the host-controlled receipt directory, outside all guest-writable mappings.
A read-only copy is used by an isolated stdlib hash worker inside the guest.
The source ZIP is independently compared with the host baseline before launch.

The worker inventories SHA-256, byte size and normalized relative path for every
protected file before tests and again afterwards. Any changed, added, deleted or
renamed file, incomplete scan, invalid record, stale identity or missing evidence
prevents PASS. An initial failure prevents test launch. Passing test output cannot
override an integrity failure. Only generated root `.git` metadata and untracked,
CPython-tagged `.pyc` files directly inside `__pycache__` are excluded; tracked files
are always protected. Other outputs must be written outside the source directory.

The host validates the full inventories against its own baseline and retains
`integrity-verification.json`; final state records the verdict. Reuse requires
the baseline, both observations, worker metadata, original suite evidence and the
matching host verdict. Old Batch 1 receipts without integrity cannot qualify.

The authoritative baseline and package bytes are host-verifiable facts. Post-test
guest files are not independently exported/rehashed on the host: their inventory,
timing and process records remain cooperative guest observations. A malicious
guest can forge those reports. SHA-256 comparison does not provide attestation.
MXC stays the default; Windows Sandbox remains explicitly selected and experimental.
Batch 3 qualification found blockers in the complete repository CI policy;
the replacement is **NOT QUALIFIED**. Production cutover remains deferred.

## Batch 3 acceptance evidence

Qualification used the real controller final gate and actual approved fixture
commits, with only external A/B decisions and audit authorization mocked. Real
cases covered multiple mandatory suites, assertion failures, test errors, skips,
insufficient counts, source modification/addition/deletion/rename, missing and
corrupt inventories, a bounded guest timeout, controlled owned-instance termination,
and controller resume without A/B replay. Startup-timeout and foreign-instance
coexistence checks used deterministic launcher doubles, not real hung instances.

The selected production policy was the complete `python tests/ci_unittest.py`
entrypoint with a 3,600-second command deadline, using the available offline
Windows Python 3.13.17. The final-code snapshot discovers 735 tests. Existing
desktop-reader assertions fail against unchanged dashboard HTML; expectations
and mandatory coverage were retained. Focused adapter, integrity and acceptance
regressions pass: 60 tests, zero failures, errors or skips.

Complete host CI on the earlier 734-test snapshot finished with 723 passed,
8 failed, 1 error and 2 skips (one unexpected under CI policy). The real Sandbox
735-test policy attempt reached its unchanged 3,600-second command deadline;
the controller reported `infrastructure_timeout`, preserved the checkpoint and
confirmed owned cleanup. No complete guest unittest totals were produced.
All 19 owned receipts retain stop/list cleanup proof. Two later independent
30-second inventory queries timed out; the current inventory is unconfirmed.

Original receipts, exact full-CI results and the acceptance matrix are retained
under `C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Batch3`; `run-root.txt`
identifies the qualification directory containing `acceptance-report.md` and
`acceptance-matrix.json`. Earlier Phase 3 evidence remains separate. MXC and its
dependencies remain available; no Batch 4 removal or production cutover occurred.


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

## Session-scoped Windows final verification

The experimental Windows backend now submits all approved final test commands to
one disposable Sandbox after every task, independent review and checkpoint has
finished. Task-level implementation/review validation is unchanged. MXC remains
the default, and the existing tester remains available.

Windows sessions require an explicit `session_coverage` declaration inside their
approved `final_verification` policy. See the [runner documentation](tools/windows_sandbox_poc/README.md#approved-session-coverage)
for its schema. The plan audit reviews this declaration; its normalized contents
are included in the existing policy hash and approval/resume identity. Existing
approved policies are never migrated or narrowed automatically. A Windows policy
without a coverage declaration stops before launch and requires a freshly
prepared, audited policy.

The controller inventories the initial manifest commit through the final approved
checkpoint, including every intervening approved checkpoint delta. Thus additions,
deletions, both rename paths and changes subsequently reverted remain visible.
Every path must match an approved rule declaring the relevant tests and affected
dependency, integration, regression or security coverage. This is a reviewed
coverage declaration; the controller does not infer semantic dependencies. Missing
coverage fails closed. All existing mandatory commands remain selected even when
no changed path names them. Full CI remains available with
`python tests/ci_unittest.py`; it is included automatically only when an approved
policy explicitly requires that command.

One run-specific selection artifact binds the session commits, checkpoint changes,
coverage reasons through the policy, command indices, minimum counts and mandatory
security suites. One host source package and protected SHA-256 baseline feed one
VM. A single guest pre-test scan precedes all commands and a single post-test scan
follows them. PASS also requires every command's raw unittest evidence, counts,
exit code, complete selection identity and confirmed owned-VM cleanup. Failures
stop without a coding repair or automatic retry. Resume can reuse only complete
matching evidence; an explicit resume after a confirmed failure may retry final
verification. Cooperative guest observations remain evidence, not malicious-guest
attestation. Production qualification and backend cutover remain separate work.

Real session-wide acceptance remains blocked on this host: the bounded read-only
inventory query times out because `WindowsSandboxServer.exe` remains active in
the owned process job after the CLI exits. Partial empty-inventory output is not
accepted. No VM was launched and no containment/security setting was changed.
The focused validation and nine-part report are retained in
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4SessionScoped-0b808c6d4ecb4daa90aa4a542d39f4f4\session-report.md`.

## Broker cold-start investigation

Automatic cold-start readiness remains **BLOCKED**. An isolated package-activation
candidate validates the Microsoft MSIX signature, signed block map and all 835
installed payload files before using `IApplicationActivationManager`. On this host
it starts an authenticated `AppService` process, but the next CLI inventory starts
a different persistent broker inside its own Job. The CLI exits 0 with partial
empty-inventory output; the owned Job remains active until the strict 30-second
timeout. Package activation therefore does not establish a usable broker lifetime.

The candidate is retained as `sandbox_broker.py` and `broker.ps1` for reproduction;
it is **not connected to automatic final verification**. The original automatic
runner, strict process containment, ownership checks, session-scoped policy and
MXC default remain intact. No broker-name exemption, breakaway, machine-policy
change, VM launch or foreign-instance cleanup occurred. The real two-task fixture
completed all approvals/checkpoints but correctly failed final verification before
VM launch. No real guest test or guest SHA verification is claimed.

The twelve-part report, exact lifecycle trace, package-validation results, focused
tests and final Git status are in
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Broker-8dbd484fec7b447498dd87cbd8b8d365\broker-report.txt`.

## Trusted Sandbox management boundary

The opt-in Windows backend now uses a sealed management boundary for the existing
Phase 3 runner. General `bounded_run`, untrusted Windows Jobs and MXC are unchanged.
Host script hashes are loaded with the controller before coding begins; only those
reviewed bytes are snapshotted outside the target and used for host/guest setup.
Session-generated scripts cannot replace them. Legitimate launcher changes require
an explicit review/update of the pinned controller hashes and a controller restart.

The fixed Microsoft PowerShell host and installed Sandbox package are validated
before management. A read-only `broker.ps1 -ValidateOnly` checks the MSIX signature,
signed block map and all payload files; the failed AppService initializer remains
isolated. The permitted commands are the exact snapshotted Session runner, an
inventory query, and marker-authorized cleanup of the unique owned instance.
Guest test argv never becomes host shell code.

Each sealed management root is created suspended and assigned to its own tracking
Job before execution. If an ancestor Job exists, its existing policy must permit
explicit separation; strict untrusted Jobs do not, and are rejected. The tracking
Job forbids descendant breakaway. It intentionally does not kill shared Microsoft
brokers on close. Completion requires root exit, complete stdout/stderr and no
survivor except a broker verified by Job membership, retained process handle,
creation time, exact package identity, protected image path and signed-payload hash.
Unknown survivors fail closed. Timeouts cancel owned transient commands, preserve
broker lifetime and require ID-specific VM cleanup; they cannot produce PASS.

Real controlled acceptance passed: two approved task checkpoints, exactly one VM,
two suites (2 + 3 tests), successful protected SHA-256 pre/post checks, owned shutdown,
fresh independent empty inventory and reusable controller evidence. A pre-VM alias
lookup defect was corrected before the single VM start; final verification resumed
without replaying A/B. No manual broker warmup, AppService activation, guest command,
full CI, security-policy change or production cutover occurred. MXC remains default.

The twelve-part report and original/corrected evidence are retained in
`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase4Management-d8981e7e838649bb94a25a5de8181b55\management-report.txt`.
