# Exercise installer branching without touching tasks, shortcuts or processes.
$ErrorActionPreference = 'Stop'
$source = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'install_autostart.ps1'))
$source = $source.Replace("[Environment]::GetFolderPath('Startup')", "'D:\fake-startup'")
$source = $source.Replace('$agentRoot = $PSScriptRoot', '$agentRoot = ''D:\fake-agent''')
$source = $source.Replace('& $exePath --config $configPath --check', '$global:LASTEXITCODE = 0')
$source = $source.Replace('& $launcherPath -StartOnly', 'Confirm-TestAgent')
$installer = [scriptblock]::Create($source)
$global:installerTestTaskCalls = 0
$global:installerTestSaved = $false
$global:installerTestStarted = $false
$global:installerTestFailStart = $false
$global:installerTestMessages = @()
$global:installerTestShortcut = [pscustomobject]@{
    TargetPath=''; Arguments=''; WorkingDirectory=''; WindowStyle=0; Description=''
}
$global:installerTestShortcut | Add-Member ScriptMethod Save { $global:installerTestSaved = $true }
$global:installerTestShell = [pscustomobject]@{}
$global:installerTestShell | Add-Member ScriptMethod CreateShortcut { param($path) return $global:installerTestShortcut }
function Test-Path { return $true }
function Remove-Item { }
function New-Item { }
function New-Object { return $global:installerTestShell }
function New-ScheduledTaskAction { return @{} }
function New-ScheduledTaskTrigger { return @{} }
function New-ScheduledTaskPrincipal { return @{} }
function New-ScheduledTaskSettingsSet { return @{} }
function Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    $global:installerTestTaskCalls++
    Write-Error 'Access denied (fixture)'
}
function Write-Host { $global:installerTestMessages += ($args -join ' ') }
function Confirm-TestAgent {
    if ($global:installerTestFailStart) { throw 'fixture startup failed' }
    $global:installerTestStarted = $true
}

& $installer
if ($global:installerTestTaskCalls -ne 0) { throw 'Default installation must never register a task.' }
if (-not $global:installerTestSaved -or -not $global:installerTestStarted) { throw 'Default installation must save shortcut and start Agent.' }

$global:installerTestSaved = $false
$global:installerTestStarted = $false
$global:installerTestMessages = @()
& $installer -UseScheduledTask
if ($global:installerTestTaskCalls -ne 1 -or -not $global:installerTestSaved -or -not $global:installerTestStarted) {
    throw 'Access denied must fall back to Startup folder and start Agent.'
}
if ($global:installerTestMessages -match 'installed via scheduled task') { throw 'A failed task must not be reported as installed.' }

$global:installerTestMessages = @()
$global:installerTestFailStart = $true
$failed = $false
try { & $installer } catch { $failed = $_.Exception.Message -eq 'fixture startup failed' }
if (-not $failed -or ($global:installerTestMessages -match 'process confirmed')) { throw 'Startup failure must not report success.' }
[Console]::WriteLine('Installer tests passed: no task by default, permission-denied fallback, no false startup success.')
