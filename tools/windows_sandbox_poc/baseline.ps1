param([string]$RunId, [string]$SandboxId, [string]$SourceCommit,
    [int]$SuiteTimeoutSeconds, [int]$OverallTimeoutSeconds, [switch]$Session)
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create([IO.File]::ReadAllText('C:\ACSetup\common.ps1')))
$started = [DateTime]::UtcNow
$deadline = $started.AddSeconds($OverallTimeoutSeconds)
$result = [ordered]@{ runId = $RunId; sandboxId = $SandboxId; sourceCommit = $SourceCommit;
    disposableCommit = $null; computerName = $env:COMPUTERNAME;
    account = [Security.Principal.WindowsIdentity]::GetCurrent().Name;
    startedUtc = $started.ToString('o'); finishedUtc = $null; versions = $null;
    suites = @(); totalTests = 0; success = $false; operationExitCode = 1; error = $null }
$workspace = 'C:\AutoCodingTest'
$python = 'C:\Python313Embed\python.exe'
$git = 'C:\ACGit\cmd\git.exe'
$node = 'C:\ACNode\node.exe'
if ($Session) {
    $sessionRequest = Get-Content -LiteralPath 'C:\ACSetup\session.json' -Raw | ConvertFrom-Json
    $result.sourceBranch = $sessionRequest.sourceBranch
    $result.policySha256 = $sessionRequest.policySha256
    $result.suiteIndex = $sessionRequest.suiteIndex
    $sessionWide = $sessionRequest.executionScope -ceq 'approved-session-v2'
    $sessionSpecs = @(Get-SessionSuites $sessionRequest)
    if ($sessionWide) {
        $result.executionScope = $sessionRequest.executionScope
        $result.sourceStartCommit = $sessionRequest.sourceStartCommit
        $result.selectionSha256 = $sessionRequest.selectionSha256
    }
    $result.sourceIntegrity = [ordered]@{ schemaVersion = 1; baselineSha256 = $sessionRequest.sourceIntegrity.baselineSha256;
        pre = $null; post = $null }
}
$env:PATH = "C:\ACGit\cmd;C:\ACNode;C:\Python313Embed;$env:PATH"
$env:PYTHONPATH = 'C:\AutoCodingTest\src'
$env:PYTHONIOENCODING = 'utf-8'
if ($Session) {
    $allowed = @('CI', 'NO_COLOR', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUTF8', 'PYTHONIOENCODING',
        'PYTHONHASHSEED', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM', 'PYTHONPATH', 'AUTOCODING_CONFIG_DIR')
    foreach ($setting in $sessionRequest.environment.PSObject.Properties) {
        if ($setting.Name -notin $allowed -or $setting.Value -isnot [string]) { throw 'Unsupported guest environment setting.' }
        [Environment]::SetEnvironmentVariable($setting.Name, $setting.Value, 'Process')
    }
    $null = [IO.Directory]::CreateDirectory('C:\AutoCodingConfig')
}
# SYSTEM inherits TEMP with different casing from the on-disk Windows directory.
# Use a disposable guest-owned directory whose spelling survives Path.resolve().
$null = [IO.Directory]::CreateDirectory('C:\AutoCodingTemp')
$env:TEMP = 'C:\AutoCodingTemp'
$env:TMP = 'C:\AutoCodingTemp'
$result.environment = [ordered]@{ temp = $env:TEMP; tmp = $env:TMP; pythonPath = $env:PYTHONPATH; path = $env:PATH }
function Run-Setup([string]$Name, [string]$Exe, [string[]]$Argv) {
    $remaining = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remaining -le 0) { throw 'Overall guest deadline expired during setup.' }
    $p = Invoke-BoundedProcess -Executable $Exe -Arguments $Argv -WorkingDirectory $workspace `
        -LogPrefix "C:\ACResults\setup-$Name" -TimeoutSeconds ([Math]::Min(120, $remaining))
    if ($p.timedOut -or $p.exitCode -ne 0) { throw "Setup $Name failed: exit=$($p.exitCode), timeout=$($p.timedOut)" }
    return [IO.File]::ReadAllText($p.stdout).Trim()
}
function Verify-Source([string]$Stage) {
    $remaining = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remaining -le 0) { throw 'Overall guest deadline expired before source integrity verification.' }
    $p = Invoke-BoundedProcess -Executable $python -Arguments @('-I', '-B', 'C:\ACSetup\source_integrity.py',
        '--root', $workspace, '--baseline', 'C:\ACSetup\source-baseline.json', '--sha256',
        $sessionRequest.sourceIntegrity.baselineSha256, '--stage', $Stage, '--output', "C:\ACResults\integrity-$Stage.json") `
        -WorkingDirectory $workspace -LogPrefix "C:\ACResults\integrity-$Stage-hash" -TimeoutSeconds ([Math]::Min(120, $remaining))
    $observation = Get-Content -LiteralPath "C:\ACResults\integrity-$Stage.json" -Raw | ConvertFrom-Json
    $result.sourceIntegrity[$Stage] = [ordered]@{ complete = $observation.complete; matchesBaseline = $observation.matchesBaseline }
    if ($p.timedOut -or $p.exitCode -ne 0 -or $observation.complete -ne $true -or $observation.matchesBaseline -ne $true) {
        throw "Source integrity $Stage verification failed."
    }
}
try {
    Expand-Archive -LiteralPath 'C:\ACSetup\baseline.zip' -DestinationPath $workspace
    Expand-Archive -LiteralPath 'C:\ACSetup\python.zip' -DestinationPath 'C:\Python313Embed'
    Rename-Item -LiteralPath 'C:\Python313Embed\python313._pth' -NewName 'python313._pth.bak'
    $versions = [ordered]@{ git = (Run-Setup 'git-version' $git @('--version'));
        python = (Run-Setup 'python-version' $python @('--version'));
        node = (Run-Setup 'node-version' $node @('--version')) }
    $result.versions = $versions
    if ($versions.git -cne 'git version 2.53.0.windows.4' -or $versions.python -cne 'Python 3.13.17' -or
        $versions.node -cne 'v26.13.1') { throw 'Offline tool version mismatch.' }
    $null = Run-Setup 'init' $git @('init')
    $null = Run-Setup 'name' $git @('config', '--local', 'user.name', 'Auto-Coding Sandbox')
    $null = Run-Setup 'email' $git @('config', '--local', 'user.email', 'sandbox@example.invalid')
    $null = Run-Setup 'add' $git @('add', '.')
    $null = Run-Setup 'commit' $git @('commit', '-m', 'Disposable baseline for Sandbox tests')
    $result.disposableCommit = Run-Setup 'head' $git @('rev-parse', 'HEAD')
    $status = Run-Setup 'status' $git @('status', '--porcelain=v1')
    if ($status) { throw 'Disposable repository is not initially clean.' }
    if ($Session) { Verify-Source 'pre' }
    $files = @('test_desktop_ui.py', 'test_dashboard_acceptance.py', 'test_desktop_bridge.py',
        'test_desktop_control.py', 'test_plan_audit.py', 'test_isolated_workspace.py')
    if ($Session) { $files = @($sessionSpecs | ForEach-Object { $_.file }) }
    $suiteNumber = 0
    foreach ($file in $files) {
        $suiteNumber++
        $remaining = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
        if ($remaining -le 0) { throw 'Overall guest deadline expired before suite launch.' }
        $exe = $python
        $argv = @('-m', 'unittest', 'discover', '-s', 'tests', '-p', $file, '-v')
        if ($Session) {
            $exe = $sessionSpecs[$suiteNumber - 1].command[0]
            $argv = @($sessionSpecs[$suiteNumber - 1].command | Select-Object -Skip 1)
        }
        $p = Invoke-BoundedProcess -Executable $exe -Arguments $argv `
            -WorkingDirectory $workspace -LogPrefix "C:\ACResults\$file" `
            -TimeoutSeconds ([Math]::Min($SuiteTimeoutSeconds, $remaining))
        $text = [IO.File]::ReadAllText($p.stdout) + "`n" + [IO.File]::ReadAllText($p.stderr)
        $report = Read-UnittestReport $text -LastSummary:$Session
        $state = if ($p.timedOut) { 'TIMEOUT' } elseif ($p.exitCode -eq 0 -and $report.cleanPass) { 'PASS' } else { 'FAIL' }
        $suite = [ordered]@{ file = $file; process = $p; tests = $report.tests; failures = $report.failures;
            errors = $report.errors; skips = $report.skips; status = $state }
        $result.suites += $suite
        $result.totalTests += $report.tests
        $suite | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "C:\ACResults\$file.suite.json" -Encoding UTF8
        if ($p.timedOut) { throw "Suite timed out: $file; aborting before further suites." }
    }
    $expected = @(4, 7, 39, 37, 40, 18)
    for ($i = 0; $i -lt $files.Count; $i++) {
        if ($result.suites[$i].status -ne 'PASS' -or $result.suites[$i].tests -lt 1 -or
            ($Session -and $sessionWide -and $result.suites[$i].tests -lt $sessionRequest.minimumTestsPerSuite[$i]) -or
            (!$Session -and $result.suites[$i].tests -ne $expected[$i])) {
            throw "Suite did not match clean baseline: $($result.suites[$i].file)"
        }
    }
    $result.success = $true
    $result.operationExitCode = 0
} catch {
    $result.error = $_.Exception.ToString() + "`n" + $_.ScriptStackTrace
} finally {
    if ($Session -and $result.sourceIntegrity.pre.matchesBaseline -eq $true) {
        try { Verify-Source 'post' } catch {
            $result.success = $false
            $result.operationExitCode = 1
            $result.error = "$($result.error) Source integrity post verification failed: $($_.Exception.Message)"
        }
    }
    $result.finishedUtc = [DateTime]::UtcNow.ToString('o')
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'C:\ACResults\baseline.json.tmp' -Encoding UTF8
    Move-Item -LiteralPath 'C:\ACResults\baseline.json.tmp' -Destination 'C:\ACResults\baseline.json'
}
exit $result.operationExitCode
