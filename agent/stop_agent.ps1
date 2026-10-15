[CmdletBinding()]
param([switch]$KeepTaskDisabled)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexAuthHubAgent'
$agentRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$exePath = Join-Path $agentRoot 'CodexAuthAgent.exe'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$restoreTask = $false
if ($task) {
    $matchesDirectory = @($task.Actions | Where-Object {
        $_.WorkingDirectory -and
        [System.IO.Path]::GetFullPath($_.WorkingDirectory).TrimEnd('\') -eq $agentRoot
    }).Count -gt 0
    if (-not $matchesDirectory) {
        throw 'The scheduled Agent belongs to another directory. Run stop_agent.bat from the installed Agent directory.'
    }
    try {
        $restoreTask = $task.State -ne 'Disabled'
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    } catch {
        $restoreTask = $false
        Write-Warning 'Cannot control the scheduled task. Trying to stop the current user Agent directly.'
    }
}
try {
    if ($task) {
        try { Stop-ScheduledTask -TaskName $taskName }
        catch { Write-Warning 'Cannot stop the scheduled task; checking the Agent process directly.' }
    }
    # PyInstaller may have both a bootloader and a child process with the same path.
    $processes = @(Get-Process -Name CodexAuthAgent -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [System.IO.Path]::GetFullPath($_.Path) -eq $exePath
    })
    foreach ($process in $processes) {
        if (-not $process.HasExited) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction Stop }
            catch {
                if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) { throw }
            }
        }
    }
    foreach ($process in $processes) {
        if (-not $process.HasExited -and -not $process.WaitForExit(15000)) {
            throw 'Agent did not exit. Close it in Task Manager or rerun as the user who started it.'
        }
    }
    # An exclusive open also detects an elevated process we cannot inspect or stop.
    if (Test-Path -LiteralPath $exePath) {
        $file = [System.IO.File]::Open($exePath, 'Open', 'ReadWrite', 'None')
        $file.Dispose()
    }
    Write-Host 'Agent stopped. You can now replace CodexAuthAgent.exe. Config and sync state are preserved.'
} finally {
    if ($task -and $restoreTask -and -not $KeepTaskDisabled) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
    }
}
