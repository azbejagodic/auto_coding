# Windows Sandbox Phase 2 proof of concept

Run from the repository root in an interactive host PowerShell session:

```powershell
$pocSource = Join-Path (Get-Location) 'tools\windows_sandbox_poc'
& ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $pocSource 'run.ps1')))) -SourceDirectory $pocSource
```

This command also works with the host's default Restricted script execution policy.
It changes no execution policy or Application Control settings. The guest script
is supplied to PowerShell as an encoded command, without changing guest policies.
Application Control restrictions still apply; a blocked command fails the proof.

Requires the installed `wsb` CLI (tested with 0.8.114.0) and Windows Sandbox.
No dependencies, downloads, networking, repository mappings, or test suites are used.

The host saves CLI help, exact arguments (including XML configuration), raw
responses, host process exit codes, guest JSON, and a summary under:

`C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase2POC\<run-id>`

One disposable Sandbox has a read-only input mapping and a separate writable
results mapping. Networking, vGPU, clipboard, audio/video input, and printer
redirection are disabled. The policy-changing LogonCommand found in the old
manual configuration is deliberately omitted. The saved manual evidence is untouched.

`start --id <fresh-guid> --config <XML-contents> --raw` returns the owned `Id`.
`exec --id <returned-id> --run-as System --command <PowerShell-command> --raw`
runs two harmless operations: arithmetic plus a child `cmd.exe` exit of 0 or 23.
The CLI help says `ExistingLogin` requires an active user session. `System`
worked without `connect`; no connected user session or user input is needed.
The `connect` help is captured for reference, but no connection is launched.

For both operations the observed host CLI process exit was 0. Its JSON
`ExitCode` independently reported the actual guest exit, respectively 0 and 23.
The host checks that against guest JSON, identity fields, run/case identifiers,
timestamp, arithmetic, and success flag. Missing, malformed, stale, contradictory,
or unexpected results fail. The known failure must have `hostAccepted: false`;
the overall proof passes only when both classifications and cleanup succeed.
Mapped-folder evidence is guest reported, not cryptographic attestation.

Cleanup uses only the fresh ID reserved by this launch, including when launch
response parsing fails. It never stops a discovered or pre-existing Sandbox.
An independent `list --raw` must confirm that ID is absent. Stop errors fail
the proof and report the ID; evidence is always preserved. A forced host process
termination or machine shutdown can bypass PowerShell's `finally` block. CLI
calls are synchronous; the result-file wait defaults to 60 seconds, but that
does not impose a deadline on the CLI itself.

Exit 0 means the proof passed; exit 1 means failure. Inspect `summary.json` and
raw CLI output before drawing conclusions. The earliest diagnostic run predates
the explicit CLI-versus-guest exit comparison; use the final run's evidence.
This does not implement the testing backend or Phase 3.
