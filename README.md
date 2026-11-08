# Auto-Coding

## 1. What Auto-Coding does

Auto-Coding runs an approved JSON plan against a separate Git checkout:

1. A one-shot Codex audit checks the exact plan and target.
2. A person inspects the report and explicitly authorizes the start.
3. Codex A implements one task at a time.
4. The controller runs the task's focused test commands.
5. Independent Codex B reviews the staged change and saved test evidence. Rejections return findings to A.
6. B approval creates a local Git checkpoint. Final verification follows all approved tasks.

Durable state, events, outputs and diagnostics are saved outside the target. A/B call and observed-token budgets
persist across safe resumes. Stops preserve approved checkpoints and unfinished work.

## 2. How to use it

### Required tools and checkout

Use Windows PowerShell with Python, Git and Codex CLI 0.163.0. Controller testing requires Windows MXC. The target
must be a clean regular Git checkout on a named feature branch, with an initial commit and contained .git metadata.
Linked worktrees are unsupported. Keep plan and run logs outside the target.

From the backend checkout:

~~~powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
$plan = "C:\Plans\dashboard-plan.json"
$repo = "C:\Projects\dashboard"
~~~

A new JSON plan has 1-30 ordered tasks. Each task needs an id, prompt and at least one focused test command as an
argv array. The plan also needs a final_verification policy with test_commands and minimum_tests. See the [plan
audit guide](docs/plan-audit.md).

### Audit, inspect and authorize

Audit is separate from A/B call budgets and does not implement the plan.

~~~powershell
$auditLogs = "C:\AutoCoding-Runs\dashboard-audit"
python -B -m auto_coding.simple_run audit $plan --repo $repo --logs $auditLogs --audit-model gpt-6-sol
Get-Content (Join-Path $auditLogs "audit-report.json")
~~~

Inspect an APPROVE report before authorizing start:

~~~powershell
$runLogs = "C:\AutoCoding-Runs\dashboard-run-01"
python -B -m auto_coding.simple_run start $plan --repo $repo --logs $runLogs --approved-audit $auditLogs --max-agent-calls 40 --max-observed-tokens 500000
~~~

The A/B invocation limit is shared. Observed-token budgeting counts recorded A/B input plus output usage; the audit
call is separate. Missing trustworthy usage stops further calls under a token budget.

### Resume and logs

After checking the stop reason and resolving it where safe, resume with the same plan bytes, target checkout and
run-log directory:

~~~powershell
python -B -m auto_coding.simple_run start $plan --repo $repo --logs $runLogs --resume
~~~

Saved settings, audit evidence, plan and checkout are checked on resume. Changed plans, checkouts or incompatible
budget overrides are rejected. Resume cannot bypass exhausted call/token budgets, process-safety gates or a latched
environment stop. The explicit --retry-review-once option can authorize one extra B review attempt, subject to other
budgets and safety checks.

Useful external run-log files include state.json, run-manifest.json, events.jsonl, Codex logs, test output and
test-environment-*.json diagnostics. The terminal stop message names its diagnostic. Keep logs private because test
output can contain project data. Do not store credentials in the checkout.

### Optional desktop interface

The viewer needs optional pywebview and, on Windows, Microsoft Edge WebView2 Runtime. Install only if the desktop
UI is wanted:

~~~powershell
python -m pip install "pywebview[winforms]"
python -B -m auto_coding.desktop_app
~~~

Codex CLI and Git must also be available to the Python process.

## 3. Test sandbox permissions

Controller tests use Codex Windows MXC with windows.sandbox=mxc and a read-only profile. Source configuration
denies the filesystem root, allows reads of the Windows system directory, selected Python runtime directory and
target checkout, and allows writes only to owned scratch under external logs. TEMP, TMP and TMPDIR point to
scratch. Network access is configured off.

The checkout, including Git metadata, is readable but not writable to tests. Unrelated host paths are denied. Test
subprocesses inherit filtered ordinary runtime variables such as PATH and temporary paths; Codex, Git, cloud and
authentication variables are excluded. Limited non-secret settings can be provided through
AUTOCODING_TEST_ENV_JSON. It cannot replace PATH or PYTHONPATH. Environment filtering is hygiene, not a sandbox.

These permissions are source-confirmed. The B11b native MXC validation recorded source reads, scratch writes,
denied source/.git writes and denied reads of an unrelated fixture for CLI 0.163.0. Network denial is configured;
this README does not claim every network path or runtime was tested. PATH does not grant tool access: the current
profile does not provision arbitrary compilers or package-manager runtime trees. Do not keep secrets in the
readable target checkout.

## 4. Toolchain compatibility

Evidence is for execution inside controller MXC, not host or VS Code availability.

| Toolchain or configuration | Evidence |
| --- | --- |
| Python headless unittest | Verified inside controller MXC |
| Python tkinter.Tcl() in one Dashboard V1 runtime | Known failing in a documented configuration |
| Node.js | Not verified |
| Headless browsers | Not verified |
| C/C++ | Not verified |
| Rust | Not verified |
| Go | Not verified |
| Java | Not verified |
| .NET | Not verified |
| Other declared toolchains | Not verified |

Historically, tkinter.Tcl() reported "Can't find a usable init.tcl" in one Dashboard V1 MXC configuration. This is
not a claim about all Python/Tk installations. No blanket Node.js or browser compatibility claim is established.

Task test_commands accept project-specific argv, but the sandbox does not install dependencies. Final verification
has a format limitation: every command must exit successfully, and its saved output must contain a successful
Python unittest summary, no skips, and at least minimum_tests tests. CTest, Cargo, Go, Gradle, .NET or browser
output does not satisfy that gate as-is.

## 5. JSON plan checklist

Before audit:

- Inspect existing project functionality and reuse suitable helpers.
- Keep tasks narrow, ordered and explicit about acceptance criteria.
- Declare real focused test commands as argv arrays, not shell strings.
- Verify required tools inside the actual controller sandbox.
- Do not use optional skips to meet mandatory acceptance criteria.
- Designate final verification that meets the current unittest evidence gate.
- Keep plan and run logs outside the target checkout.
- Audit the exact final JSON, inspect the report, then explicitly approve.

## 6. Failures and recovery

A completed test command that returns nonzero with confirmed cleanup is a code/test failure. Its evidence goes to A
within saved attempt limits. Timeouts and incomplete results follow their dedicated stop paths. B reviews
independently only after focused task checks pass.

A structured process-launch failure or verified sandbox preflight limitation is a confirmed environment blocker.
The controller stops at that command, saves a bounded test-environment-*.json diagnostic, and does not launch
another test command, A or B, or approve the task. Ordinary resume refuses a saved environment-stop latch; inspect
it before separately authorized recovery.

An unclassified execution problem stops without guessing it is a permission failure. Diagnostics preserve known
facts and unknowns. Uncertain process cleanup retains its safety gate. Stops preserve approved commits and existing
source changes; they never approve incomplete work.

Captured process-failure excerpts are limited to 16 KiB and marked when truncated or incomplete. Hard controller
termination can lose buffered output. A nested runtime or dependency failure after launch may look like a normal
nonzero test result; the controller cannot identify every tool failure.
