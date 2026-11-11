# Isolated controller verification on Windows

Controller tests still use the validated Codex CLI 0.163.0 Windows MXC backend,
with networking disabled and an effective policy checked before execution.
Implementation, independent review, audit authorization, branch/checkpoint
protection, Stop/Resume, process ownership, and the final no-skips gate retain
their existing workflows. There is no host-execution fallback.

Each test command receives a fresh source copy under controller-owned external
`review-scratch-*` storage. The original checkout is explicitly denied inside
MXC. Only the disposable copy can receive generated files and source writes.
TEMP/TMP/TMPDIR, HOME/USERPROFILE, app data, application configuration, and
Python/npm caches point into that scratch directory. Credentials and user Git
configuration are not inherited. Nested Git repositories can initialize and
commit there with an explicit test identity; host hooks and system/global Git
configuration are disabled.
Validated custom Python import roots inside the project are relocated to the
copy, preserving the controller's existing support for additional source layouts.

The snapshot records the exact HEAD, an independently constructed Git tree ID
over the copied working bytes and index file modes, and a SHA-256 file manifest.
This includes uncommitted implementation files and tracked deletions: task
verification runs before the existing review/checkpoint stages. Final
verification uses the approved committed HEAD. Windows CRLF working bytes may
produce a different tree ID from the canonical Git commit tree; both identities
are recorded, without running Git filters. A second capture after CLI inspection
must match before target execution starts. Existing controller checks continue
to bind the original branch, diff, review and final snapshot.
New projects without a first commit bind the unborn branch and exact tree;
their first implementation still follows the existing review/checkpoint flow.

Git metadata/history/configuration are never copied. Linked files, reparse
points, submodules, unresolved merges, assume-unchanged/skip-worktree index flags,
sensitive filenames and Windows filename
aliases fail closed. Ignored files (including local dependency directories) are
excluded. Snapshot limits are 50,000 paths and 128 MiB. Projects needing ignored
fixtures must track them; dependencies must be preinstalled in trusted roots.
Tests depending on the original `.git` metadata need disposable Git fixtures.
Standalone absolute arguments beneath the original checkout are relocated to
the copy. Commands embedding host paths inside code or shell strings must use
relative paths themselves.

## Trusted tools

The selected Python installation and Windows runtime retain read-only access.
Fixed machine installation directories `%ProgramFiles%\Git` and
`%ProgramFiles%\nodejs`, when present, are validated and added read-only. There
is no automatic grant derived from PATH or project requests. PATH is filtered
to directories in approved read-only roots, with Python/Windows and validated
installation binary directories added independently of the interactive PATH.
Top-level executable resolution searches those explicit
directories without consulting the controller's working directory.

An operator can replace the default Git/Node roots with
`AUTOCODING_TEST_TOOLCHAIN_DIRS_JSON`, a JSON array of up to eight external
installation directories. An explicit empty array disables discovery. Roots,
ancestors and bounded directory trees are checked for links/reparse points;
drive roots, personal directories, checkout/log/runtime overlaps and unsafe
spellings are rejected. Sensitive files inside approved installations receive
explicit deny entries. No environment variable from the project or guest can
change this policy. Alternative Python layouts with standard libraries outside
the selected interpreter installation need separately validated installation
roots; no broader read grant is inferred from an import error.

## Evidence and recovery

Controller-owned `controller-test-sandbox-*.txt` and matching `.json` exports
retain combined stdout/stderr, launcher and test exit codes, snapshot identity,
working directory, executable resolution, selected non-secret environment paths,
timeout, process cleanup proof, and workspace removal status. Preflight failures
produce `controller-test-preflight-*.json`. Guest-selected artifact paths are
never opened for export. Test stdout remains untrusted and can itself contain
application data; logs retain the existing controller storage/access rules.

Missing executables and permission errors at launch carry structured OS error
codes. Python dependency/permission/subprocess output patterns are diagnostic
hints, not proven infrastructure causes or permission-change authorization.
Incomplete launcher output, initialization failures, timeout and uncertain
cleanup remain distinct. No untrusted output can authorize a broader policy.

The only automatic correction is one attempt to recreate a missing designated
temporary leaf before test code starts. It never recreates parents or reruns
test code. Missing tools, failed permissions, assertion failures, timeouts and
uncertain cleanup have no sandbox retry. Existing explicit controller recovery
and implementation correction policies still apply.

Normal Windows execution now carries the bounded runner's explicit empty-Job
and closed-pipe cleanup proof. An exit code alone cannot authorize removal.
Scratch is retained when process cleanup is uncertain; existing safety gates
block further execution. Confirmed process termination permits removal using
the existing scratch identity/path guard. Removal failures are reported
separately from process termination and retain diagnostic evidence.

## Validation

`python tests/ci_unittest.py` runs controller regressions, including deterministic
mocked MXC policy, timeout and uncertain-cleanup cases. These mocks do not prove
native MXC behavior.

`python tests/mxc_workspace_probe.py` is a separate real MXC probe. It builds an
external fixture repository and executes six acceptance tests for original/host
path denials, disposable writes, isolated environment/storage, nested Python,
Git initialization/commit, and Node. It requires six passing tests with no
skips and confirmed cleanup, retaining controller-owned evidence externally.
Run it from a host that can launch native MXC; an outer sandbox may lack that
capability. A failure never substitutes unsandboxed target execution.

To validate the preserved dashboard, use a separate regular clone at
`e402409ecb3f501f0c6ed42a4390de255b262b01`, with clean status and external evidence
storage. Leave the preserved dashboard branch untouched. With this controller's
`src` directory on the host Python import path, execute:

```python
from pathlib import Path
import sys
from auto_coding import controller_test_sandbox as sandbox, simple_run as runner

target = Path(r"C:\validation\dashboard")
logs = Path(r"C:\validation\dashboard-evidence")
logs.mkdir(parents=True, exist_ok=True)
assert runner.git(target, ["rev-parse", "HEAD"]) == "e402409ecb3f501f0c6ed42a4390de255b262b01"
assert not runner.git(target, ["status", "--porcelain"])
result = sandbox.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                      "-p", "test_dashboard_acceptance.py", "-v"],
                     target, logs, 2400, git=runner.git, launch=runner.call)
print(result[1])
summary = runner.unittest_evidence(result[1])
assert result[0] == 0 and not result[2] and result.cleanup_confirmed is True
assert summary and summary["successful"] and summary["tests"] == 7 and not summary["skipped"]
```

Inspect the exported JSON for the exact source HEAD and removed workspace, and
confirm the original clone remains clean. The fixture probe alone does not
establish that the historical dashboard failure has been reproduced or fixed.
