[CmdletBinding()]
param([switch]$Remove, [switch]$UseStartupFolder, [switch]$UseScheduledTask)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexAuthHubAgent'
$startupFolder = [Environment]::GetFolderPath('Startup')
if (-not $startupFolder) { throw 'Cannot locate the current user Startup folder.' }
$shortcutPath = Join-Path $startupFolder 'CodexAuthHubAgent.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
    }
    & (Join-Path $PSScriptRoot 'stop_agent.ps1') -KeepTaskDisabled
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Agent autostart removed.'
    exit
}
$agentRoot = $PSScriptRoot
$configPath = Join-Path $agentRoot 'agent_config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $agentRoot 'agent_config.example.json') -Destination $configPath
}
$exePath = Join-Path $agentRoot 'CodexAuthAgent.exe'
if (-not (Test-Path -LiteralPath $exePath)) {
    throw 'Build or copy agent/CodexAuthAgent.exe before installing autostart.'
}
& $exePath --config $configPath --check
if ($LASTEXITCODE -ne 0) { throw 'Invalid agent configuration.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$launcherPath = Join-Path $agentRoot 'run_hidden.ps1'
if (-not (Test-Path -LiteralPath $launcherPath)) { throw "Launcher not found: $launcherPath. Copy run_hidden.ps1 into the Agent directory." }
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$launcherArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`""
$installedTask = $false
if ($UseScheduledTask -and -not $UseStartupFolder) {
  try {
    $action = New-ScheduledTaskAction -Execute $powershellPath -Argument $launcherArguments -WorkingDirectory $agentRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    if (-not $?) { throw 'Scheduled task registration did not succeed.' }
    $installedTask = $true
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
  } catch {
    $installedTask = $false
        Write-Warning "Scheduled task installation failed: $($_.Exception.Message)"
  }
}
if (-not $installedTask) {
    # The current user's Startup folder needs no administrator privileges.
    New-Item -ItemType Directory -Path $startupFolder -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $powershellPath
    $shortcut.Arguments = $launcherArguments
    $shortcut.WorkingDirectory = $agentRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Codex Auth Hub Agent'
    $shortcut.Save()
}
# Launch synchronously through the helper so early failures are visible here,
# instead of silently disappearing inside a second hidden PowerShell window.
& $launcherPath -StartOnly
if ($installedTask) {
    Write-Host 'Login autostart installed via scheduled task; Agent process confirmed.'
} else {
    Write-Host 'Login autostart installed via current-user Startup folder; Agent process confirmed.'
    Write-Host 'Startup folder mode does not restart Agent automatically after a crash.'
}
