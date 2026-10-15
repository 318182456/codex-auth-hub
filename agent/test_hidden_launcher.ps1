# Launch control tests use fake processes only; no Agent or startup task is run.
$ErrorActionPreference = 'Stop'
$global:launcherTest_launches = 0
$global:launcherTest_earlyExit = $false
$global:launcherTest_existing = @()
$global:launcherTest_missing = $false
$global:launcherTest_fakeProcess = [pscustomobject]@{ Id = 1234; ExitCode = 1 }
$global:launcherTest_fakeProcess | Add-Member ScriptMethod WaitForExit { param($timeout) return $global:launcherTest_earlyExit }
$global:launcherTest_fakeProcess | Add-Member ScriptMethod Refresh { }
function Test-Path { return -not $global:launcherTest_missing }
function Get-Process { return $global:launcherTest_existing }
function Start-Process { $global:launcherTest_launches++; return $global:launcherTest_fakeProcess }
function Get-Content { return 'fixture startup failure' }
$launcher = Join-Path $PSScriptRoot 'run_hidden.ps1'

& $launcher -StartOnly
if ($global:launcherTest_launches -ne 1) { throw 'Expected one background launch.' }

$global:launcherTest_earlyExit = $true
$failed = $false
try { & $launcher -StartOnly }
catch {
    $failed = $_.Exception.Message.Contains('fixture startup failure') -and $_.Exception.Message.Contains('exit code 1')
}
if (-not $failed) { throw 'An early exit must expose startup diagnostics.' }

$global:launcherTest_existing = @([pscustomobject]@{Id = 5678; Path = (Join-Path $PSScriptRoot 'CodexAuthAgent.exe')})
$before = $global:launcherTest_launches
& $launcher -StartOnly
if ($global:launcherTest_launches -ne $before) { throw 'An existing Agent must not be launched again.' }

$global:launcherTest_missing = $true
$failed = $false
try { & $launcher -StartOnly }
catch { $failed = $_.Exception.Message.Contains('Agent EXE not found') }
if (-not $failed) { throw 'A missing EXE must be reported before attempting launch.' }
Write-Host 'Launcher tests passed: background start, early exit, duplicate instance, missing EXE.'
