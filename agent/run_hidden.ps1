$ErrorActionPreference = 'Stop'
$exePath = Join-Path $PSScriptRoot 'CodexAuthAgent.exe'
$configPath = Join-Path $PSScriptRoot 'agent_config.json'
$process = Start-Process -FilePath $exePath -ArgumentList @('--config', "`"$configPath`"") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru
exit $process.ExitCode
