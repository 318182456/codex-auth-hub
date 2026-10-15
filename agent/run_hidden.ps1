[CmdletBinding()]
param([switch]$StartOnly)
$ErrorActionPreference = 'Stop'
$exePath = Join-Path $PSScriptRoot 'CodexAuthAgent.exe'
$configPath = Join-Path $PSScriptRoot 'agent_config.json'
if (-not (Test-Path -LiteralPath $exePath)) { throw "Agent EXE not found: $exePath" }
if (-not (Test-Path -LiteralPath $configPath)) { throw "Agent config not found: $configPath" }
$existing = @(Get-Process -Name CodexAuthAgent -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and [System.IO.Path]::GetFullPath($_.Path) -eq [System.IO.Path]::GetFullPath($exePath)
})
if ($existing.Count) {
    Write-Host "Agent is already running. PID: $($existing[0].Id)"
    if ($StartOnly) { return }
    # Keep the task attached to the running Agent, so it cannot count a live
    # instance as a successful exit and lose its restart behavior.
    Wait-Process -Id $existing.Id -ErrorAction SilentlyContinue
    exit 0
}
$stderrPath = Join-Path $PSScriptRoot 'agent_startup.stderr.log'
$stdoutPath = Join-Path $PSScriptRoot 'agent_startup.stdout.log'
$process = Start-Process -FilePath $exePath -ArgumentList @('--config', "`"$configPath`"") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardError $stderrPath -RedirectStandardOutput $stdoutPath
if ($process.WaitForExit(3000)) {
    $process.Refresh()
    $detail = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
    } else { '' }
    throw "Agent exited during startup (exit code $($process.ExitCode)).`n$detail`nCheck $stderrPath and agent_config.log."
}
Write-Host "Agent is running in background. PID: $($process.Id). Log: $(Join-Path $PSScriptRoot 'agent_config.log')"
if ($StartOnly) { return }
$process.WaitForExit()
$process.Refresh()
exit $process.ExitCode
