# Shared by host and guest; no dependencies or changes to machine policy.
function Invoke-BoundedProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory,
        [string]$LogPrefix, [ValidateRange(1, 7200)][int]$TimeoutSeconds)
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
    $stdout = [IO.File]::Create("$LogPrefix.stdout.log")
    $stderr = [IO.File]::Create("$LogPrefix.stderr.log")
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

function Read-UnittestReport([string]$Text) {
    $runs = [regex]::Matches($Text, '(?m)^Ran (\d+) tests? in [\d.]+s\r?$')
    $footer = [regex]::Matches($Text, '(?m)^(OK|FAILED)(?: \(([^\r\n]*)\))?\r?$')
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
