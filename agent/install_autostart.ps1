[CmdletBinding()]
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexAuthHubAgent'
if ($Remove) {
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
$powershellPath = Join-Path $PSHOME 'powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`"" -WorkingDirectory $agentRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'Agent autostart installed and started for the current login user.'
