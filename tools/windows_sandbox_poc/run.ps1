param([int]$ResultTimeoutSeconds = 60, [string]$SourceDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$runId = [guid]::NewGuid().ToString()
$requestedId = [guid]::NewGuid().ToString()
$root = Join-Path 'C:\Users\HP\Desktop\AutoCodingSandboxRepro\Phase2POC' $runId
$inputDir = Join-Path $root 'input'
$resultsDir = Join-Path $root 'results'
$ownedId = $null
$launchAttempted = $false
$passed = $false
$cleanupVerified = $false
$failure = $null
$checks = @()
New-Item -ItemType Directory -Path $inputDir, $resultsDir | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceDirectory 'guest.ps1') -Destination $inputDir

function Invoke-Wsb([string]$Label, [string[]]$Arguments) {
    [ordered]@{ executable = 'wsb'; arguments = $Arguments } | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath (Join-Path $root "$Label.command.json") -Encoding UTF8
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & wsb @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPreference }
    $raw = ($output | Out-String).Trim()
    $raw | Set-Content -LiteralPath (Join-Path $root "$Label.output.txt") -Encoding UTF8
    $code | Set-Content -LiteralPath (Join-Path $root "$Label.host-exit.txt")
    Write-Host "$Label (host exit $code): $raw"
    if ($code -ne 0) { throw "$Label failed with host exit $code. See $root" }
    return ($raw | ConvertFrom-Json)
}

function Get-Ids($Response) {
    if ($null -eq $Response.PSObject.Properties['WindowsSandboxEnvironments']) {
        throw 'Unrecognized list response; cannot verify Sandbox ownership/cleanup.'
    }
    return @($Response.WindowsSandboxEnvironments)
}

try {
    & wsb --version | Set-Content -LiteralPath (Join-Path $root 'cli-version.txt')
    foreach ($verb in @('start', 'list', 'exec', 'connect', 'stop')) {
        & wsb $verb --help | Set-Content -LiteralPath (Join-Path $root "$verb.help.txt")
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect $verb help." }
    }
    $before = @(Get-Ids (Invoke-Wsb 'before' @('list', '--raw')))
    if (($before | ConvertTo-Json -Depth 10) -match [regex]::Escape($requestedId)) {
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
  </MappedFolders>
</Configuration>
"@
    $xml | Set-Content -LiteralPath (Join-Path $root 'sandbox.wsb') -Encoding UTF8
    $launchAttempted = $true
    $launch = Invoke-Wsb 'start' @('start', '--id', $requestedId, '--config', $xml, '--raw')
    # Parse the actual CLI launch schema rather than inferring an ID from a list.
    if ($launch.Id -cne $requestedId) {
        throw 'Launch response did not return the requested Sandbox ID.'
    }
    $ownedId = $launch.Id
    foreach ($case in @('success', 'failure')) {
        $started = [DateTime]::UtcNow
        $guestCommand = "& ([scriptblock]::Create([IO.File]::ReadAllText('C:\ACSetup\guest.ps1'))) -RunId '$runId' -Case '$case'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($guestCommand))
        $command = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded"
        $execution = Invoke-Wsb "exec-$case" @('exec', '--id', $ownedId, '--run-as', 'System', '--command', $command, '--raw')
        if ($null -eq $execution.PSObject.Properties['ExitCode'] -or $execution.ExitCode -isnot [int]) {
            throw "Missing or invalid CLI guest ExitCode: $case"
        }
        $path = Join-Path $resultsDir "$case.json"
        $deadline = [DateTime]::UtcNow.AddSeconds($ResultTimeoutSeconds)
        while (!(Test-Path -LiteralPath $path) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
        if (!(Test-Path -LiteralPath $path)) { throw "Missing guest result: $path" }
        $result = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
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
    }
    $passed = $true
} catch {
    $failure = $_.Exception.Message
    Write-Warning $failure
} finally {
    try {
        if ($launchAttempted) {
            # Even if launch output could not be parsed, the unique requested ID is ours only.
            $null = Invoke-Wsb 'stop' @('stop', '--id', $requestedId, '--raw')
            $remaining = @(Get-Ids (Invoke-Wsb 'after' @('list', '--raw')))
            if (($remaining | ConvertTo-Json -Depth 10) -match [regex]::Escape($requestedId)) {
                throw "Owned Sandbox remains running: $requestedId"
            }
            $cleanupVerified = $true
        }
    } catch {
        $failure = "$failure Cleanup failed for $requestedId : $($_.Exception.Message)"
        Write-Warning $failure
    }
    $outcome = if ($passed -and $cleanupVerified) { 'PASS' } else { 'FAIL' }
    [ordered]@{ outcome = $outcome; runId = $runId; sandboxId = $ownedId; requestedSandboxId = $requestedId;
        cleanupVerified = $cleanupVerified; checks = $checks; failure = $failure; evidencePath = $root } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'summary.json') -Encoding UTF8
    Write-Host "$outcome : $root"
}
if (!$passed -or !$cleanupVerified) { exit 1 }
