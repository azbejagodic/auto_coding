# Shared by host and guest; no dependencies or changes to machine policy.
function Get-SessionSuites {
    param($Request)
    $whole = $Request.executionScope -ceq 'approved-session-v2'
    if ($Request.executionScope -and !$whole) { throw 'Unsupported session execution scope.' }
    if ($whole) {
        $commands = @($Request.commands)
        $minimum = $Request.minimumTestsPerSuite
        if ($null -eq $minimum) { $minimum = $Request.policy.session_coverage.minimum_tests_per_suite }
        $minimum = @($minimum)
        if ($commands.Count -lt 1 -or $commands.Count -gt 10 -or $minimum.Count -ne $commands.Count) {
            throw 'Incomplete session command/count inventory.'
        }
        for ($i = 0; $i -lt $commands.Count; $i++) {
            $command = $commands[$i]
            if ($command -is [string] -or @($command).Count -lt 1 -or @($command).Count -gt 32 -or
                $minimum[$i] -isnot [int] -or $minimum[$i] -lt 1 -or $minimum[$i] -gt 1000000) {
                throw 'Malformed session suite.'
            }
            foreach ($argument in $command) {
                if ($argument -isnot [string] -or !$argument -or $argument.Length -gt 4096) { throw 'Malformed session argv.' }
            }
            [pscustomobject]@{ file = "session-suite-$($i + 1)"; command = @($command); minimumTests = $minimum[$i] }
        }
    } else {
        if ($Request.suiteIndex -lt 1 -or @($Request.command).Count -lt 1) { throw 'Missing legacy session command.' }
        [pscustomobject]@{ file = "session-suite-$($Request.suiteIndex)"; command = @($Request.command); minimumTests = 1 }
    }
}

function Invoke-BoundedProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory,
        [string]$LogPrefix, [ValidateRange(1, 100000)][int]$TimeoutSeconds)
    # Windows CommandLineToArgvW quoting, including embedded quotes/trailing slashes.
    $quoted = foreach ($argument in $Arguments) {
        '"' + [regex]::Replace([regex]::Replace($argument, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }
    $start = [DateTime]::UtcNow
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = $quoted -join ' '
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    # Preserve raw logs while allowing the host to inspect long-running progress.
    $stdout = [IO.File]::Open("$LogPrefix.stdout.log", [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $stderr = [IO.File]::Open("$LogPrefix.stderr.log", [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $timedOut = $false
    $code = $null
    try {
        if (!$process.Start()) { throw "Could not start $Executable" }
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        if (!$process.WaitForExit($TimeoutSeconds * 1000)) {
            $timedOut = $true
            # Kill only this process. Guest descendants disappear at Sandbox shutdown.
            $process.Kill()
            if (!$process.WaitForExit(5000)) { throw "Timed-out process $($process.Id) did not exit." }
        }
        $code = $process.ExitCode
        if (![Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($outTask, $errTask), 5000)) {
            $timedOut = $true
        }
    } finally {
        $stdout.Dispose()
        $stderr.Dispose()
        $process.Dispose()
    }
    $record = [ordered]@{ executable = $Executable; arguments = $Arguments;
        command = ('"' + $Executable + '" ' + ($quoted -join ' ')); workingDirectory = $WorkingDirectory;
        startedUtc = $start.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o');
        exitCode = $code; timedOut = $timedOut; timeoutSeconds = $TimeoutSeconds;
        stdout = "$LogPrefix.stdout.log"; stderr = "$LogPrefix.stderr.log" }
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$LogPrefix.process.json" -Encoding UTF8
    return [pscustomobject]$record
}

function Read-UnittestReport([string]$Text, [switch]$LastSummary) {
    $runs = [regex]::Matches($Text, '(?m)^Ran (\d+) tests? in [\d.]+s\r?$')
    $footer = [regex]::Matches($Text, '(?m)^(OK|FAILED)(?: \(([^\r\n]*)\))?\r?$')
    if ($LastSummary -and $runs.Count -gt 0) {
        $last = $runs[$runs.Count - 1]
        $runs = @($last)
        $tail = $Text.Substring($last.Index + $last.Length).TrimStart([char[]]"`r`n")
        $footer = [regex]::Matches($tail, '\A(OK|FAILED)(?: \(([^\r\n]*)\))?\r?(?:\n|$)')
    }
    $count = $null; $failures = $null; $errors = $null; $skips = $null
    $complete = ($runs.Count -eq 1 -and $footer.Count -eq 1)
    $ok = $false
    if ($complete) {
        $count = [int]$runs[0].Groups[1].Value
        $failures = 0; $errors = 0; $skips = 0
        $details = $footer[0].Groups[2].Value
        foreach ($entry in @(@('failures', 'failures'), @('errors', 'errors'), @('skipped', 'skips'))) {
            $match = [regex]::Match($details, "(?:^|, )$($entry[0])=(\d+)(?:,|$)")
            if ($match.Success) { Set-Variable -Name $entry[1] -Value ([int]$match.Groups[1].Value) }
        }
        # Any annotation (including expected/unexpected successes) rejects a clean pass.
        $ok = ($footer[0].Groups[1].Value -eq 'OK' -and $details -eq '')
    }
    [pscustomobject]@{ tests = $count; failures = $failures; errors = $errors; skips = $skips; complete = $complete; cleanPass = $ok }
}
