# One-shot plan audit

Run these commands from the trusted backend checkout (or an installed backend).
The target must be a separate, clean regular Git checkout on a feature branch,
with an initial commit and contained Git metadata. Logs must be outside it.
Use a JSON plan with the existing designated `final_verification` policy.

```powershell
$env:PYTHONPATH = "$PWD\src"
python -B -m auto_coding.simple_run audit C:\Plans\tasks.json --repo C:\Projects\target --logs C:\Runs\audit-01 --audit-model gpt-6-sol --timeout 300 --audit-goal "Describe the overall development goal"
Get-Content C:\Runs\audit-01\audit-report.json
# Explicit consent after inspecting the report:
python -B -m auto_coding.simple_run start C:\Plans\tasks.json --repo C:\Projects\target --logs C:\Runs\run-01 --approved-audit C:\Runs\audit-01
# Subsequent safe resume uses the saved approval; no audit call:
python -B -m auto_coding.simple_run start C:\Plans\tasks.json --repo C:\Projects\target --logs C:\Runs\run-01 --resume
```

Choose an available audit model explicitly; it is independent of `--model-a` and
`--model-b`. The audit fixes `model_reasoning_effort="high"`. If using
`--controller-tests`, pass it to both `audit` and `start`; the audit binds that
focused verification mode. CLI compatibility, loaded permission settings and
native temporary storage use the B07/B07b checks
(currently validated against Codex CLI 0.163.0). Target and Git are read-only;
only dedicated external scratch is writable by auditor shell commands. No
command network access is granted. Unsupported linked worktrees fail closed.

Audit is a separate operation, never an implementation launch. Exit zero means
a validated APPROVE report was saved, not that tests passed or code was approved.
REVISE/BLOCK, malformed output and execution failures cannot authorize a start.
Inspect task findings, recommended changes and blockers before giving consent.
The ordinary positional-plan invocation also requires `--approved-audit` for a
new run. Existing pre-audit run states remain recoverable. The desktop has no
new controls; starting new workflows requires the CLI.

The entire plan is supplied once, up to 128 KiB, without truncation. Reports are
bounded to 64 KiB. A durable sibling `.auto-coding-*.plan-audit-*.json` ledger
reserves one invocation per plan hash, initial repository identity/HEAD/branch
and final-verification policy. Repeating identical inputs reuses the completed
report, including a negative verdict, without calling Codex. Different model,
goal or timeout settings do not silently buy another call for those inputs.
After a failed/interrupted reserved invocation there is no automatic retry;
inspect diagnostics and revise the inputs deliberately before a new audit.
Unverified cleanup retains workspace safety protection and scratch for inspection.

Evidence binds the exact plan bytes (including formatting), canonical target
path/file identity, Git metadata location, initial HEAD/branch, final policy,
model/High configuration and report hash. Start rechecks clean initial state;
resume checks saved bindings before Git recovery and relies on existing Git
checkpoint validation for later approved commits. Moving/recreating a checkout
or deleting audit evidence prevents reuse. Evidence hashes detect accidental
edits; they are not signatures against a user who can rewrite controller logs
and the sibling ledger. Keep both until the run has completed.

The prompt limits inspection to relevant files and requests at most 12 files;
this relevance limit is an instruction, not an OS read quota. The process timeout
and existing bounded output collector enforce execution/output limits. The
audit neither executes tests nor guarantees its recommendations are correct.
