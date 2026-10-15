<#
.SYNOPSIS
    将本地 Codex 的 auth.json 直接上传/同步到 Codex Auth Hub 服务端

.EXAMPLE
    .\Upload-CodexAuth.ps1 -ProfileName "Codex-开发节点1" -Description "主账号开发环境"
    .\Upload-CodexAuth.ps1 -ServerUrl "http://127.0.0.1:8000" -ProfileName "Codex-VIP" -AuthFilePath "D:\Transfer\auth.json"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProfileName,

    [string]$ServerUrl = "http://127.0.0.1:8000",
    [string]$Description = "",
    [int]$MaxUsers = 0,
    [string]$AuthFilePath
)

$ErrorActionPreference = 'Stop'

if (-not $AuthFilePath) {
    if ($env:CODEX_HOME) {
        $AuthFilePath = Join-Path $env:CODEX_HOME 'auth.json'
    } elseif ($env:USERPROFILE) {
        $AuthFilePath = Join-Path $env:USERPROFILE '.codex\auth.json'
    } else {
        throw "无法自动定位本地 auth.json，请使用 -AuthFilePath 指定文件路径。"
    }
}

if (-not (Test-Path -LiteralPath $AuthFilePath -PathType Leaf)) {
    throw "未找到 auth.json 文件: $AuthFilePath"
}

Write-Host ">>> 正在读取本地凭证: $AuthFilePath" -ForegroundColor Cyan
$rawBytes = [System.IO.File]::ReadAllBytes($AuthFilePath)
$jsonText = [System.Text.Encoding]::UTF8.GetString($rawBytes).TrimStart([char]0xFEFF)

try {
    $authObj = ConvertFrom-Json -InputObject $jsonText -ErrorAction Stop
} catch {
    throw "本地 auth.json 不是合法的 JSON 文件。"
}

$payload = @{
    name = $ProfileName
    description = $Description
    max_users = $MaxUsers
    auth_json = $authObj
} | ConvertTo-Json -Depth 20 -Compress

$uploadUrl = "$($ServerUrl.TrimEnd('/'))/api/profiles"
Write-Host ">>> 正在上传到服务端: $uploadUrl ..." -ForegroundColor Cyan

try {
    $response = Invoke-RestMethod -Uri $uploadUrl -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
    Write-Host ">>> 上传成功！" -ForegroundColor Green
    Write-Host "    配置ID: $($response.profile_id)" -ForegroundColor Green
    Write-Host "    节点名称: $($response.name)" -ForegroundColor Green
    if ($response.account_email) {
        Write-Host "    关联账号: $($response.account_email)" -ForegroundColor Yellow
    }
} catch {
    Write-Error "上传失败: $_"
}
