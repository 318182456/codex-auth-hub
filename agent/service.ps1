[CmdletBinding()]
param(
    [ValidateSet('Install', 'Start', 'Stop', 'Restart', 'Uninstall', 'Status')]
    [string]$Action = 'Status',
    [string]$AuthFilePath
)
$ErrorActionPreference = 'Stop'
$serviceName = 'CodexAuthHubAgent'
$agentRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$exePath = Join-Path $agentRoot 'CodexAuthAgent.exe'
$configPath = Join-Path $agentRoot 'agent_config.json'
$scExe = Join-Path $env:SystemRoot 'System32\sc.exe'

function Get-OptionalRegistryValue([string]$Path, [string]$Name) {
    try {
        $properties = Get-ItemProperty -LiteralPath $Path -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
    # A missing property is normal on a first install. Inspect the property
    # collection instead of asking the provider for a value that may not exist.
    $property = $properties.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Stop-AgentService {
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if (-not $service) { return }
    if ($service) {
        try {
            if ($service.Status -ne 'Stopped') {
                Stop-Service -Name $serviceName -ErrorAction Stop
                $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
            }
        } finally { $service.Dispose() }
    }
    # Wait until the frozen executable's bootloader also releases the EXE.
    $processes = @(Get-Process -Name CodexAuthAgent -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [System.IO.Path]::GetFullPath($_.Path) -eq $exePath
    })
    foreach ($process in $processes) {
        if (-not $process.HasExited -and -not $process.WaitForExit(15000)) {
            throw 'Agent is still running. Stop the legacy background instance before replacing its EXE.'
        }
    }
}

function Start-AgentService {
    Start-Service -Name $serviceName -ErrorAction Stop
    $service = Get-Service -Name $serviceName -ErrorAction Stop
    try {
        $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(45))
        Start-Sleep -Seconds 2
        $service.Refresh()
        if ($service.Status -ne 'Running') { throw 'Agent service exited. Check agent_config.log and Windows Application event log.' }
    } finally { $service.Dispose() }
}

function Remove-LegacyStartup {
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $command = Get-OptionalRegistryValue -Path $runKey -Name $serviceName
    if ($command -and $command.IndexOf($agentRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        Remove-ItemProperty -LiteralPath $runKey -Name $serviceName -ErrorAction Stop
    }
    $startup = [Environment]::GetFolderPath('Startup')
    if ($startup) {
        $link = Join-Path $startup "$serviceName.lnk"
        if (Test-Path -LiteralPath $link) {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($link)
            if ($shortcut.WorkingDirectory.TrimEnd('\') -eq $agentRoot) { Remove-Item -LiteralPath $link }
        }
    }
    $task = Get-ScheduledTask -TaskName $serviceName -ErrorAction SilentlyContinue
    if ($task -and @($task.Actions | Where-Object { $_.WorkingDirectory -and $_.WorkingDirectory.TrimEnd('\') -eq $agentRoot }).Count) {
        Disable-ScheduledTask -TaskName $serviceName -ErrorAction Stop | Out-Null
        Stop-ScheduledTask -TaskName $serviceName -ErrorAction Stop
        Unregister-ScheduledTask -TaskName $serviceName -Confirm:$false -ErrorAction Stop
    }
    # Only retire standalone processes from this installation, after stopping
    # its legacy task so it cannot respawn during migration.
    $legacyProcesses = @(Get-Process -Name CodexAuthAgent -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [System.IO.Path]::GetFullPath($_.Path) -eq $exePath
    })
    $legacyProcesses | ForEach-Object {
        if (-not $_.HasExited) {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($process in $legacyProcesses) {
        if (-not $process.HasExited -and -not $process.WaitForExit(15000)) { throw 'Cannot stop the legacy Agent. Migration was not completed.' }
    }
}

if ($Action -eq 'Status') {
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($service) {
        $service | Select-Object Name, Status, StartType | Format-List
        $imagePath = Get-ItemPropertyValue -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName" -Name ImagePath
        Write-Host "Service command: $imagePath"
    } else { Write-Host 'Agent service is not installed.' }
    Write-Host "Log: $(Join-Path $agentRoot 'agent_config.log')"
    return
}
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Windows service management requires Administrator. Run PowerShell or install_service.bat as Administrator.'
}

switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $exePath)) { throw "EXE not found: $exePath" }
        if (-not (Test-Path -LiteralPath $configPath)) {
            Copy-Item -LiteralPath (Join-Path $agentRoot 'agent_config.example.json') -Destination $configPath
        }
        $oldImagePath = Get-OptionalRegistryValue -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName" -Name ImagePath
        if ($oldImagePath -and -not $oldImagePath.StartsWith("`"$exePath`"", [StringComparison]::OrdinalIgnoreCase)) {
            throw 'This service is installed in another directory. Manage or uninstall it from its original installation first.'
        }
        if (-not $AuthFilePath -and $oldImagePath -match '--auth-file\s+"([^"]+)"') { $AuthFilePath = $Matches[1] }
        if (-not $AuthFilePath) {
            $AuthFilePath = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'auth.json' } else { Join-Path $env:USERPROFILE '.codex\auth.json' }
        }
        $AuthFilePath = [System.IO.Path]::GetFullPath($AuthFilePath)
        if ($AuthFilePath.StartsWith('\\')) { throw 'Use a local auth.json path; the service runs as LocalSystem.' }
        & $exePath --config $configPath --service --auth-file $AuthFilePath --check
        if ($LASTEXITCODE -ne 0) { throw 'Service-capable Agent EXE and valid config are required. Stop the old Agent and replace its EXE with the service version first.' }
        Stop-AgentService
        Remove-LegacyStartup
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service) {
            $service.Dispose()
            & $scExe delete $serviceName
            if ($LASTEXITCODE -ne 0) { throw 'Cannot remove the previous service registration.' }
            $deadline = [DateTime]::UtcNow.AddSeconds(15)
            do {
                $pending = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
                if (-not $pending) { break }
                $pending.Dispose()
                if ([DateTime]::UtcNow -ge $deadline) { throw 'Service deletion is pending. Close Services/Task Manager and retry installation.' }
                Start-Sleep -Milliseconds 250
            } while ($true)
        }
        $binaryPath = '"{0}" --service --config "{1}" --auth-file "{2}"' -f $exePath, $configPath, $AuthFilePath
        $created = New-Service -Name $serviceName -DisplayName 'Codex Auth Hub Agent' -BinaryPathName $binaryPath -StartupType Automatic -Description 'Codex login credential sync and connectivity monitoring; runs before Windows login.' -ErrorAction Stop
        $created.Dispose()
        & $scExe failure $serviceName reset= 86400 actions= restart/10000/restart/30000/restart/60000
        if ($LASTEXITCODE -ne 0) { throw 'Failed to configure service recovery.' }
        & $scExe failureflag $serviceName 1
        if ($LASTEXITCODE -ne 0) { throw 'Failed to enable service failure recovery.' }
        Start-AgentService
        # Remove only the retired launchers explicitly replaced by this service.
        foreach ($obsolete in @('install_autostart.ps1', 'run_hidden.ps1', 'start_agent.bat',
            'start_background.bat', 'stop_agent.bat', 'stop_agent.ps1',
            'diagnose_autostart.ps1', 'diagnose_autostart.bat')) {
            $obsoletePath = Join-Path $agentRoot $obsolete
            if (Test-Path -LiteralPath $obsoletePath -PathType Leaf) { Remove-Item -LiteralPath $obsoletePath -ErrorAction Stop }
        }
        Write-Host 'Agent service installed and running. Startup: Automatic. Windows login is not required.'
        Write-Host "Auth file: $AuthFilePath"
        Write-Host 'If you elevated using a different administrator account, reinstall with -AuthFilePath pointing to the actual login user auth.json.'
    }
    'Start' { Start-AgentService; Write-Host 'Agent service is running.' }
    'Stop' {
        Stop-AgentService
        Remove-LegacyStartup
        Write-Host 'Agent service and legacy background Agent stopped. The EXE can now be replaced.'
    }
    'Restart' { Stop-AgentService; Start-AgentService; Write-Host 'Agent service restarted.' }
    'Uninstall' {
        Stop-AgentService
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service) {
            $service.Dispose()
            & $scExe delete $serviceName
            if ($LASTEXITCODE -ne 0) { throw 'Service removal failed.' }
        }
        Write-Host 'Agent service removed. Config, credentials and sync state are preserved.'
    }
}
