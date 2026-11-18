param(
    [ValidateSet('Smoke', 'Baseline145', 'Session')][string]$Mode = 'Smoke',
    [ValidateRange(1, 300)][int]$ResultTimeoutSeconds = 60,
    [ValidateRange(1, 86400)][int]$SuiteTimeoutSeconds = 900,
    [ValidateRange(1, 90000)][int]$OverallTimeoutSeconds = 3600,
    [string]$SourceDirectory = $PSScriptRoot,
    [string]$RequestPath
)
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $SourceDirectory 'common.ps1'))))
$sourceCommit = '84c3bd826aadad24a39c4739c39414bf836373cd'
$toolsRoot = 'C:\Users\HP\Desktop\AutoCodingSandboxRepro'
$runId = [guid]::NewGuid().ToString()
$requestedId = [guid]::NewGuid().ToString()
$runsFolder = if ($Mode -eq 'Smoke') { 'Phase2POC' } else { 'Phase3Runs' }
$root = Join-Path (Join-Path $toolsRoot $runsFolder) $runId
if ($Mode -eq 'Session') {
    $request = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
    $runId = ([guid]$request.runId).ToString()
    $requestedId = ([guid]$request.sandboxId).ToString()
    if ($runId -cne $request.runId -or $requestedId -cne $request.sandboxId -or
        $request.sourceCommit -notmatch '^[0-9a-f]{40}([0-9a-f]{24})?$' -or
        [string]::IsNullOrWhiteSpace($request.sourceBranch) -or $request.policy.timeout_seconds -lt 30 -or
        $request.policy.timeout_seconds -gt 86400 -or $request.suiteIndex -lt 1 -or
        $request.suiteIndex -gt @($request.policy.test_commands).Count -or @($request.command).Count -lt 1) {
        throw 'Invalid controller session request.'
    }
    $root = Split-Path -Parent (Resolve-Path -LiteralPath $RequestPath).Path
    $sourceCommit = $request.sourceCommit
    $SuiteTimeoutSeconds = [int]$request.policy.timeout_seconds
    $OverallTimeoutSeconds = $SuiteTimeoutSeconds + 300
}
$inputDir = Join-Path $root 'input'
$resultsDir = Join-Path $root 'results'
$ownedId = $null
$launchAttempted = $false
$passed = $false
$cleanupVerified = $false
$failure = $null
$checks = @()
$baseline = $null
if ($Mode -eq 'Session') {
    if (Test-Path -LiteralPath $resultsDir) { throw 'Session results directory already exists.' }
    New-Item -ItemType Directory -Path $resultsDir | Out-Null
} else { New-Item -ItemType Directory -Path $inputDir, $resultsDir | Out-Null }
Copy-Item -LiteralPath (Join-Path $SourceDirectory 'guest.ps1') -Destination $inputDir
$wsbExe = (Get-Command wsb -ErrorAction Stop).Source

function Invoke-Wsb([string]$Label, [string[]]$Arguments, [int]$TimeoutSeconds = 60, [switch]$TextOutput) {
    [ordered]@{ executable = 'wsb'; arguments = $Arguments } | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath (Join-Path $root "$Label.command.json") -Encoding UTF8
    $p = Invoke-BoundedProcess -Executable $wsbExe -Arguments $Arguments -WorkingDirectory $SourceDirectory `
        -LogPrefix (Join-Path $root $Label) -TimeoutSeconds $TimeoutSeconds
    $code = $p.exitCode
    $raw = [IO.File]::ReadAllText($p.stdout).Trim()
    $raw | Set-Content -LiteralPath (Join-Path $root "$Label.output.txt") -Encoding UTF8
    $code | Set-Content -LiteralPath (Join-Path $root "$Label.host-exit.txt")
    if (!$TextOutput) { Write-Host "$Label (host exit $code): $raw" }
    if ($p.timedOut) { throw "$Label TIMEOUT after $TimeoutSeconds seconds. See $root" }
    if ($code -ne 0) { throw "$Label failed with host exit $code. See $root" }
    if ($TextOutput) { return $raw }
    return ($raw | ConvertFrom-Json)
}

function Wait-Result([string]$Name) {
    $path = Join-Path $resultsDir $Name
    $deadline = [DateTime]::UtcNow.AddSeconds($ResultTimeoutSeconds)
    while (!(Test-Path -LiteralPath $path) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
    if (!(Test-Path -LiteralPath $path)) { throw "Missing guest result: $path" }
    return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
}

function Get-Ids($Response) {
    if ($null -eq $Response.PSObject.Properties['WindowsSandboxEnvironments'] -or
        $Response.WindowsSandboxEnvironments -isnot [array]) {
        throw 'Unrecognized list response; cannot verify Sandbox ownership/cleanup.'
    }
    foreach ($instance in $Response.WindowsSandboxEnvironments) {
        $parsedId = [guid]::Empty
        if ($instance.Id -isnot [string] -or ![guid]::TryParse($instance.Id, [ref]$parsedId)) {
            throw 'Malformed Sandbox list entry; cannot verify cleanup.'
        }
        $instance.Id
    }
}

try {
    $null = Invoke-Wsb 'cli-version' @('--version') -TextOutput
    foreach ($verb in @('start', 'list', 'exec', 'connect', 'stop')) {
        $null = Invoke-Wsb "$verb-help" @($verb, '--help') -TextOutput
    }
    $toolMappings = ''
    if ($Mode -ne 'Smoke') {
        foreach ($name in @('common.ps1', 'baseline.ps1')) {
            Copy-Item -LiteralPath (Join-Path $SourceDirectory $name) -Destination $inputDir
        }
        $gitRoot = Join-Path $toolsRoot 'GitHubDesktop-3.6.5-x64-full\lib\net45\resources\app\git'
        $nodeRoot = Join-Path $toolsRoot 'node-v26.13.1-win-x64\node-v26.13.1-win-x64'
        $pythonZip = Join-Path $toolsRoot 'python-3.13.17-embed-amd64.zip'
        $hashes = @(
            @((Join-Path $gitRoot 'cmd\git.exe'), '32BC406CBAFABD33B2F236325F98DCEA988CFA86F0519229176F5DB19D526CE3'),
            @($pythonZip, '9E35319BFFEDAB8B36AD44D8F377CB4F1733BCEC257A0B6D04D94F01486DDB38'),
            @((Join-Path $nodeRoot 'node.exe'), 'F5109DC868EC38703AD8F305E0ACEECE717CB60D4889873F434017AF4E96CCCB')
        )
        $actualHashes = foreach ($item in $hashes) {
            $hash = Get-FileHash -LiteralPath $item[0] -Algorithm SHA256
            if ($hash.Hash -cne $item[1]) { throw "Offline tool hash mismatch: $($item[0])" }
            $hash
        }
        $actualHashes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'tool-hashes.json') -Encoding UTF8
        Copy-Item -LiteralPath $pythonZip -Destination (Join-Path $inputDir 'python.zip')
        if ($Mode -eq 'Baseline145') {
        $archive = Invoke-BoundedProcess -Executable (Get-Command git).Source `
            -Arguments @('archive', '--format=zip', '--output', (Join-Path $inputDir 'baseline.zip'), $sourceCommit) `
            -WorkingDirectory (Split-Path $SourceDirectory | Split-Path) -LogPrefix (Join-Path $root 'source-archive') -TimeoutSeconds 120
        if ($archive.timedOut -or $archive.exitCode -ne 0) { throw 'Baseline git archive failed.' }
        } else {
            if (!(Test-Path -LiteralPath (Join-Path $inputDir 'baseline.zip') -PathType Leaf)) { throw 'Missing approved session archive.' }
            # Only guest-needed fields enter the read-only input mapping.
            [ordered]@{ sourceBranch = $request.sourceBranch; policySha256 = $request.policySha256;
                suiteIndex = $request.suiteIndex; command = @($request.command); environment = $request.environment } | ConvertTo-Json -Depth 6 |
                Set-Content -LiteralPath (Join-Path $inputDir 'session.json') -Encoding UTF8
        }
        $gitXml = [Security.SecurityElement]::Escape($gitRoot)
        $nodeXml = [Security.SecurityElement]::Escape($nodeRoot)
        $toolMappings = "<MappedFolder><HostFolder>$gitXml</HostFolder><SandboxFolder>C:\ACGit</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder><MappedFolder><HostFolder>$nodeXml</HostFolder><SandboxFolder>C:\ACNode</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>"
    }
    $before = @(Get-Ids (Invoke-Wsb 'before' @('list', '--raw')))
    if ($before -contains $requestedId) {
        throw 'Requested ID already exists.'
    }
    $escapedInput = [System.Security.SecurityElement]::Escape($inputDir)
    $escapedResults = [System.Security.SecurityElement]::Escape($resultsDir)
    $xml = @"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Disable</Networking>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder><HostFolder>$escapedInput</HostFolder><SandboxFolder>C:\ACSetup</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$escapedResults</HostFolder><SandboxFolder>C:\ACResults</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
    $toolMappings
  </MappedFolders>
</Configuration>
"@
    $xml | Set-Content -LiteralPath (Join-Path $root 'sandbox.wsb') -Encoding UTF8
    $launchAttempted = $true
    [ordered]@{ runId = $runId; sandboxId = $requestedId } | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $root 'launch-attempt.json.tmp') -Encoding UTF8
    Move-Item -LiteralPath (Join-Path $root 'launch-attempt.json.tmp') -Destination (Join-Path $root 'launch-attempt.json')
    $launch = Invoke-Wsb 'start' @('start', '--id', $requestedId, '--config', $xml, '--raw') -TimeoutSeconds 180
    # Parse the actual CLI launch schema rather than inferring an ID from a list.
    if ($launch.Id -cne $requestedId) {
        throw 'Launch response did not return the requested Sandbox ID.'
    }
    $ownedId = $launch.Id
    if ($Mode -eq 'Smoke') { foreach ($case in @('success', 'failure')) {
        $started = [DateTime]::UtcNow
        $guestCommand = "& ([scriptblock]::Create([IO.File]::ReadAllText('C:\ACSetup\guest.ps1'))) -RunId '$runId' -Case '$case'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($guestCommand))
        $command = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded"
        $execution = Invoke-Wsb "exec-$case" @('exec', '--id', $ownedId, '--run-as', 'System', '--command', $command, '--raw')
        if ($null -eq $execution.PSObject.Properties['ExitCode'] -or $execution.ExitCode -isnot [int]) {
            throw "Missing or invalid CLI guest ExitCode: $case"
        }
        $result = Wait-Result "$case.json"
        foreach ($field in @('runId', 'case', 'computerName', 'account', 'timestampUtc', 'value', 'operationExitCode', 'success', 'error')) {
            if ($null -eq $result.PSObject.Properties[$field]) { throw "Missing field: $field" }
        }
        $timestamp = [DateTimeOffset]::Parse($result.timestampUtc).UtcDateTime
        if ($result.runId -cne $runId -or $result.case -cne $case -or
            [string]::IsNullOrWhiteSpace($result.computerName) -or [string]::IsNullOrWhiteSpace($result.account) -or
            $result.computerName -eq $env:COMPUTERNAME -or $timestamp -lt $started.AddSeconds(-5) -or
            $timestamp -gt [DateTime]::UtcNow.AddSeconds(5) -or $result.value -ne 42 -or $null -ne $result.error -or
            $result.success -isnot [bool] -or $result.operationExitCode -isnot [int]) {
            throw "Invalid, stale, or contradictory guest evidence: $case"
        }
        $expected = if ($case -eq 'success') { 0 } else { 23 }
        if ($result.operationExitCode -ne $expected -or $result.success -ne ($expected -eq 0)) {
            throw "Unexpected guest operation status: $case"
        }
        if ($execution.ExitCode -ne $result.operationExitCode) {
            throw "CLI and guest exit statuses disagree: $case"
        }
        $accepted = ($execution.ExitCode -eq 0 -and $result.operationExitCode -eq 0 -and $result.success)
        if ($accepted -ne ($case -eq 'success')) { throw "Incorrect host classification: $case" }
        $checks += [ordered]@{ case = $case; cliGuestExitCode = $execution.ExitCode;
            operationExitCode = $result.operationExitCode; hostAccepted = $accepted }
        Write-Host "Guest $case validated: operation exit $($result.operationExitCode), success=$($result.success)"
    } } else {
        $started = [DateTime]::UtcNow
        $sessionFlag = if ($Mode -eq 'Session') { ' -Session' } else { '' }
        $guestCommand = "& ([scriptblock]::Create([IO.File]::ReadAllText('C:\ACSetup\baseline.ps1'))) -RunId '$runId' -SandboxId '$ownedId' -SourceCommit '$sourceCommit' -SuiteTimeoutSeconds $SuiteTimeoutSeconds -OverallTimeoutSeconds $OverallTimeoutSeconds$sessionFlag"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($guestCommand))
        $execution = Invoke-Wsb 'exec-baseline' @('exec', '--id', $ownedId, '--run-as', 'System', '--command', "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded", '--raw') -TimeoutSeconds ($OverallTimeoutSeconds + 30)
        $baseline = Wait-Result 'baseline.json'
        if ($execution.ExitCode -isnot [int] -or $baseline.operationExitCode -isnot [int] -or
            $execution.ExitCode -ne $baseline.operationExitCode) { throw 'CLI and guest execution status missing or contradictory.' }
        if ($baseline.runId -cne $runId -or $baseline.sandboxId -cne $ownedId -or $baseline.sourceCommit -cne $sourceCommit -or
            $baseline.disposableCommit -notmatch '^[0-9a-f]{40}$' -or [string]::IsNullOrWhiteSpace($baseline.account) -or
            [string]::IsNullOrWhiteSpace($baseline.computerName) -or $baseline.computerName -eq $env:COMPUTERNAME -or
            [DateTimeOffset]::Parse($baseline.startedUtc).UtcDateTime -lt $started.AddSeconds(-5) -or
            [DateTimeOffset]::Parse($baseline.finishedUtc).UtcDateTime -gt [DateTime]::UtcNow.AddSeconds(5)) { throw 'Invalid baseline identity/timestamps.' }
        if ($baseline.versions.git -cne 'git version 2.53.0.windows.4' -or $baseline.versions.python -cne 'Python 3.13.17' -or
            $baseline.versions.node -cne 'v26.13.1') { throw 'Missing or incorrect guest tool versions.' }
        $files = @('test_desktop_ui.py', 'test_dashboard_acceptance.py', 'test_desktop_bridge.py',
            'test_desktop_control.py', 'test_plan_audit.py', 'test_isolated_workspace.py')
        $counts = @(4, 7, 39, 37, 40, 18)
        if ($Mode -eq 'Session') {
            $files = @("session-suite-$($request.suiteIndex)")
            if ($baseline.sourceBranch -cne $request.sourceBranch -or $baseline.policySha256 -cne $request.policySha256 -or
                $baseline.suiteIndex -ne $request.suiteIndex) { throw 'Guest session identity differs from controller request.' }
        }
        if (@($baseline.suites).Count -ne $files.Count) { throw "Required suites missing. Guest error: $($baseline.error)" }
        $total = 0
        $previousFinish = [DateTimeOffset]::Parse($baseline.startedUtc)
        for ($i = 0; $i -lt $files.Count; $i++) {
            $suite = $baseline.suites[$i]
            $file = $files[$i]
            $stdout = Join-Path $resultsDir "$file.stdout.log"
            $stderr = Join-Path $resultsDir "$file.stderr.log"
            foreach ($required in @($stdout, $stderr, (Join-Path $resultsDir "$file.process.json"), (Join-Path $resultsDir "$file.suite.json"))) {
                if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing suite evidence: $required" }
            }
            $report = Read-UnittestReport ([IO.File]::ReadAllText($stdout) + "`n" + [IO.File]::ReadAllText($stderr)) -LastSummary:($Mode -eq 'Session')
            $p = $suite.process
            $metadata = Get-Content -LiteralPath (Join-Path $resultsDir "$file.process.json") -Raw | ConvertFrom-Json
            $savedSuite = Get-Content -LiteralPath (Join-Path $resultsDir "$file.suite.json") -Raw | ConvertFrom-Json
            $expectedArgs = @('-m', 'unittest', 'discover', '-s', 'tests', '-p', $file, '-v')
            $expectedExe = 'C:\Python313Embed\python.exe'
            if ($Mode -eq 'Session') {
                $expectedExe = $request.command[0]
                $expectedArgs = @($request.command | Select-Object -Skip 1)
            }
            if (($metadata | ConvertTo-Json -Depth 6 -Compress) -cne ($p | ConvertTo-Json -Depth 6 -Compress) -or
                ($savedSuite | ConvertTo-Json -Depth 7 -Compress) -cne ($suite | ConvertTo-Json -Depth 7 -Compress) -or
                $suite.file -cne $file -or $p.executable -cne $expectedExe -or
                ($p.arguments | ConvertTo-Json -Compress) -cne ($expectedArgs | ConvertTo-Json -Compress) -or
                $p.workingDirectory -cne 'C:\AutoCodingTest' -or $p.stdout -cne "C:\ACResults\$file.stdout.log" -or
                $p.stderr -cne "C:\ACResults\$file.stderr.log" -or $p.exitCode -isnot [int] -or $p.exitCode -ne 0 -or
                $p.timedOut -isnot [bool] -or $p.timedOut -or $p.timeoutSeconds -lt 1 -or $p.timeoutSeconds -gt $SuiteTimeoutSeconds -or
                [DateTimeOffset]::Parse($p.startedUtc) -lt $previousFinish -or
                [DateTimeOffset]::Parse($p.finishedUtc) -lt [DateTimeOffset]::Parse($p.startedUtc) -or
                [DateTimeOffset]::Parse($p.finishedUtc) -gt [DateTimeOffset]::Parse($baseline.finishedUtc) -or
                !$report.complete -or !$report.cleanPass -or $suite.status -cne 'PASS') { throw "Invalid or failed suite evidence: $file" }
            foreach ($field in @('tests', 'failures', 'errors', 'skips')) {
                if ($suite.$field -isnot [int] -or $suite.$field -ne $report.$field) { throw "Contradictory $field for $file" }
            }
            if (($Mode -eq 'Baseline145' -and $report.tests -ne $counts[$i]) -or $report.tests -lt 1 -or $report.failures -ne 0 -or $report.errors -ne 0 -or $report.skips -ne 0) {
                throw "Suite count or failure mismatch: $file"
            }
            $previousFinish = [DateTimeOffset]::Parse($p.finishedUtc)
            $total += $report.tests
            $checks += [ordered]@{ file = $file; tests = $report.tests; exitCode = $p.exitCode;
                failures = $report.failures; errors = $report.errors; skips = $report.skips; status = 'PASS' }
        }
        if (($Mode -eq 'Baseline145' -and $total -ne 145) -or $baseline.totalTests -isnot [int] -or $baseline.totalTests -ne $total -or
            $baseline.success -isnot [bool] -or !$baseline.success -or $null -ne $baseline.error -or $execution.ExitCode -ne 0) {
            throw "Baseline failed: $($baseline.error)"
        }
    }
    $passed = $true
} catch {
    $failure = $_.Exception.Message + "`n" + $_.ScriptStackTrace
    Write-Warning $failure
} finally {
    try {
        if ($launchAttempted) {
            # Even if launch output could not be parsed, the unique requested ID is ours only.
            $stopError = $null
            try { $null = Invoke-Wsb 'stop' @('stop', '--id', $requestedId, '--raw') -TimeoutSeconds 120 } catch { $stopError = $_.Exception.Message }
            # Verify absence even when stop itself fails or times out.
            $remaining = @(Get-Ids (Invoke-Wsb 'after' @('list', '--raw') -TimeoutSeconds 30))
            if ($remaining -contains $requestedId) {
                throw "Owned Sandbox remains running: $requestedId"
            }
            $cleanupVerified = $true
            if ($stopError) { throw "Stop reported an error (absence verified): $stopError" }
        }
    } catch {
        $failure = "$failure Cleanup failed for $requestedId : $($_.Exception.Message)"
        Write-Warning $failure
    }
    $outcome = if ($passed -and $cleanupVerified) { 'PASS' } else { 'FAIL' }
    if ($failure) { $outcome = 'FAIL'; $passed = $false }
    [ordered]@{ outcome = $outcome; mode = $Mode; runId = $runId; sandboxId = $ownedId; requestedSandboxId = $requestedId;
        cleanupVerified = $cleanupVerified; checks = $checks; baseline = $baseline; failure = $failure; evidencePath = $root } |
        ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $root 'summary.json.tmp') -Encoding UTF8
    Move-Item -LiteralPath (Join-Path $root 'summary.json.tmp') -Destination (Join-Path $root 'summary.json')
    Write-Host "$outcome : $root"
}
if (!$passed -or !$cleanupVerified) { exit 1 }
