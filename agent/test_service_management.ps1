# Mock the SCM and filesystem: no service, task, registry or process is changed.
$ErrorActionPreference = 'Stop'
$source = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'service.ps1'))
$source = $source.Replace('$agentRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd(''\'')', '$agentRoot = ''D:\fake-agent''')
$source = $source.Replace('$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())', '$principal = $global:serviceTestPrincipal')
$source = $source.Replace('& $exePath --config $configPath --service --auth-file $AuthFilePath --check', '$global:LASTEXITCODE = 0')
$source = $source.Replace('& $scExe ', 'Invoke-TestSc ')
$source = $source.Replace("[Environment]::GetFolderPath('Startup')", "'D:\fake-startup'")
$manager = [scriptblock]::Create($source)
$global:serviceTestInstalled = $false
$global:serviceTestAdmin = $true
$global:serviceTestBinary = ''
$global:serviceTestRemoved = @()
$global:serviceTestRecovery = @()
$global:serviceTestMissingRunValue = $false
$global:serviceTestMissingRunKey = $false
$global:serviceTestPrincipal = [pscustomobject]@{}
$global:serviceTestPrincipal | Add-Member ScriptMethod IsInRole { param($role) return $global:serviceTestAdmin }
$global:serviceTestService = [pscustomobject]@{Status = 'Stopped'}
$global:serviceTestService | Add-Member ScriptMethod WaitForStatus { param($status, $timeout) }
$global:serviceTestService | Add-Member ScriptMethod Dispose { }
$global:serviceTestService | Add-Member ScriptMethod Refresh { }
$global:serviceTestShell = [pscustomobject]@{}
$global:serviceTestShell | Add-Member ScriptMethod CreateShortcut { param($path) return [pscustomobject]@{WorkingDirectory = 'D:\fake-agent'} }
function Test-Path { return $true }
function Get-Service { if ($global:serviceTestInstalled) { return $global:serviceTestService } }
function Get-Process { return @() }
function Get-ScheduledTask { return $null }
function Get-ItemPropertyValue {
    param($LiteralPath, $Name, $ErrorAction)
    if ($LiteralPath.StartsWith('HKLM:')) { return $global:serviceTestBinary }
    return 'D:\fake-agent\run_hidden.ps1'
}
function Get-ItemProperty {
    param($LiteralPath, $ErrorAction)
    if ($LiteralPath.StartsWith('HKLM:')) {
        if (-not $global:serviceTestBinary) { throw [System.Management.Automation.ItemNotFoundException]::new('Service key missing') }
        return [pscustomobject]@{ImagePath = $global:serviceTestBinary}
    }
    if ($global:serviceTestMissingRunKey) { throw [System.Management.Automation.ItemNotFoundException]::new('Run key missing') }
    if ($global:serviceTestMissingRunValue) { return [pscustomobject]@{OtherApp = 'preserved'} }
    return [pscustomobject]@{CodexAuthHubAgent = 'D:\fake-agent\run_hidden.ps1'}
}
function Remove-ItemProperty { }
function Remove-Item { param($LiteralPath, $ErrorAction) $global:serviceTestRemoved += $LiteralPath }
function New-Object { return $global:serviceTestShell }
function New-Service {
    param($Name, $DisplayName, $BinaryPathName, $StartupType, $Description, $ErrorAction)
    if ($StartupType -ne 'Automatic') { throw 'Service must start automatically before login.' }
    $global:serviceTestInstalled = $true
    $global:serviceTestBinary = $BinaryPathName
    return $global:serviceTestService
}
function Invoke-TestSc {
    $global:LASTEXITCODE = 0
    $global:serviceTestRecovery += ($args -join ' ')
    if ($args[0] -eq 'delete') { $global:serviceTestInstalled = $false; $global:serviceTestBinary = '' }
}
function Start-Service { $global:serviceTestService.Status = 'Running' }
function Stop-Service { $global:serviceTestService.Status = 'Stopped' }
function Start-Sleep { }
function Write-Host { }

& $manager -Action Install -AuthFilePath 'C:\Users\login\.codex\auth.json'
if (-not $global:serviceTestInstalled -or $global:serviceTestService.Status -ne 'Running') { throw 'Installation must create and start service.' }
if (-not $global:serviceTestBinary.Contains('--service --config "D:\fake-agent\agent_config.json" --auth-file "C:\Users\login\.codex\auth.json"')) {
    throw 'Service command must quote absolute config/auth paths and use native service mode.'
}
if (-not ($global:serviceTestRecovery -match 'failureflag.*1')) { throw 'Service failure recovery must be enabled.' }
if (-not ($global:serviceTestRemoved -match 'run_hidden.ps1')) { throw 'Retired launchers must be removed after migration.' }
if ($global:serviceTestRemoved -match 'agent_config|auth.json') { throw 'User config, state and credentials must be preserved.' }

& $manager -Action Install
if (-not $global:serviceTestBinary.Contains('C:\Users\login\.codex\auth.json')) { throw 'Reinstall must preserve the existing auth path.' }
& $manager -Action Stop
if ($global:serviceTestService.Status -ne 'Stopped') { throw 'Stop must wait for stopped state.' }
& $manager -Action Start
& $manager -Action Uninstall
if ($global:serviceTestInstalled) { throw 'Uninstall must remove the service registration.' }
$global:serviceTestMissingRunValue = $true
& $manager -Action Install -AuthFilePath 'C:\Users\login\.codex\auth.json'
if (-not $global:serviceTestInstalled) { throw 'A missing old startup value must not block installation.' }
& $manager -Action Uninstall
$global:serviceTestMissingRunKey = $true
& $manager -Action Install -AuthFilePath 'C:\Users\login\.codex\auth.json'
if (-not $global:serviceTestInstalled) { throw 'A missing old startup key must not block installation.' }
& $manager -Action Uninstall
$global:serviceTestAdmin = $false
$failed = $false
try { & $manager -Action Install } catch { $failed = $_.Exception.Message.Contains('requires Administrator') }
if (-not $failed) { throw 'Non-administrator installation must fail clearly.' }
[Console]::WriteLine('Service manager tests passed, including missing legacy registry key/value on first install.')
