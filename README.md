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
- Confirmed sandbox preflight/test-command launch blockers stop with durable diagnostics; ordinary test failures still return to A. **Nested tool failures may still look like ordinary nonzero test exits**. Never assume the controller detects every permission problem.

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
