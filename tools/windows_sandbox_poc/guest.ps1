param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [Parameter(Mandatory = $true)][ValidateSet('success', 'failure')][string]$Case
)
$ErrorActionPreference = 'Stop'
$code = 1
$value = $null
$problem = $null
try {
    # A real child process supplies the operation's exit status.
    $requested = if ($Case -eq 'failure') { 23 } else { 0 }
    $value = 6 * 7
    & "$env:SystemRoot\System32\cmd.exe" /d /c "exit $requested"
    $code = $LASTEXITCODE
    if ($value -ne 42) { throw 'Arithmetic check failed.' }
} catch {
    $problem = $_.Exception.Message
    $code = 1
}
$result = [ordered]@{
    runId = $RunId
    case = $Case
    computerName = $env:COMPUTERNAME
    account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    timestampUtc = [DateTime]::UtcNow.ToString('o')
    value = $value
    operationExitCode = $code
    success = ($code -eq 0)
    error = $problem
}
$target = "C:\ACResults\$Case.json"
$result | ConvertTo-Json | Set-Content -LiteralPath "$target.tmp" -Encoding UTF8
Move-Item -LiteralPath "$target.tmp" -Destination $target
exit $code
