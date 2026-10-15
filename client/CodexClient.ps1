<#
.SYNOPSIS
    Codex 客户端认证切换、额度监控与在线追踪工具 (支持跨网段、VPN、单人单号与下载)
.DESCRIPTION
    1. 跨网段/VPN 友好：自动记忆服务端地址，支持 Tailscale / VPN / 局域网多种网络环境
    2. 严格约束：一个担当同一时间只能取一个认证 json，获取新认证会自动清除旧账号的 DB 占用
    3. 连接 Codex Auth Hub 查看所有可用账号、官方 5小时额度、本周额度以及当前有几个人在用、都是谁
    4. 灵活应用：支持一键应用到本地 ~/.codex/auth.json、仅下载文件、或导入离线文件
    5. 解决 VS Code 插件免重启生效的痛点，提供 1 秒重载快捷指引
    6. 自动保持在线心跳，退出时自动释放
.EXAMPLE
    .\CodexClient.ps1
    .\CodexClient.ps1 -ServerUrl "http://100.100.88.111:8000"
    .\CodexClient.ps1 -ImportFile "D:\Transfer\auth.json"
#>

[CmdletBinding()]
param(
    [string]$ServerUrl,
    [string]$UserName = $env:USERNAME,
    [string]$HostName = $env:COMPUTERNAME,
    [string]$ImportFile
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 计算本地 Codex 路径
$targetDir = ""
if ($env:CODEX_HOME) {
    $targetDir = $env:CODEX_HOME
} elseif ($env:USERPROFILE) {
    $targetDir = Join-Path $env:USERPROFILE '.codex'
} else {
    $targetDir = "C:\Users\$UserName\.codex"
}
$targetAuthPath = Join-Path $targetDir 'auth.json'

if (-not (Test-Path -LiteralPath $targetDir)) {
    [System.IO.Directory]::CreateDirectory($targetDir) | Out-Null
}

# ----------------- 离线导入模式 (针对完全断网的受限 VPN 机) -----------------
if ($ImportFile) {
    Write-Host ">>> 进入离线导入模式..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $ImportFile -PathType Leaf)) {
        Write-Host "指定的凭据文件不存在: $ImportFile" -ForegroundColor Red
        exit 1
    }
    if (Test-Path -LiteralPath $targetAuthPath) {
        Copy-Item -LiteralPath $targetAuthPath -Destination "$targetAuthPath.bak" -Force
        Write-Host "已备份旧凭证至: $targetAuthPath.bak" -ForegroundColor DarkGray
    }
    Copy-Item -LiteralPath $ImportFile -Destination $targetAuthPath -Force
    Write-Host ">>> [成功] 凭据已成功导入至本地: $targetAuthPath" -ForegroundColor Green
    Write-Host "提示: 在 VS Code 中按 Ctrl+Shift+P 输入 Reload Window 即可立即生效！" -ForegroundColor Yellow
    exit 0
}

# ----------------- 读取持久化配置（Server 地址与担当姓名） -----------------
$serverConfigFile = Join-Path $targetDir "hub_server.txt"
$userConfigFile = Join-Path $targetDir "hub_user.txt"

if (-not $ServerUrl) {
    if (Test-Path -LiteralPath $serverConfigFile) {
        $savedServer = (Get-Content -LiteralPath $serverConfigFile -Raw).Trim()
        if ($savedServer) { $ServerUrl = $savedServer }
    }
    if (-not $ServerUrl) {
        $ServerUrl = "http://172.28.26.33:8000"
    }
}

if (Test-Path -LiteralPath $userConfigFile) {
    $savedUser = (Get-Content -LiteralPath $userConfigFile -Raw).Trim()
    if ($savedUser) { $UserName = $savedUser }
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   Codex Auth Hub 客户端 (支持跨网段/VPN & 单人单号约束)  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " 凭据目标路径: $targetAuthPath" -ForegroundColor Gray
Write-Host " 规则说明:     一个担当同一时间只能取一个认证 json，获取新账号自动清除旧占用" -ForegroundColor DarkCyan
Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray

# 确认/修改服务端地址（跨网段/VPN机器只需配置一次）
Write-Host "服务端地址: [$ServerUrl]" -ForegroundColor Yellow
Write-Host "若需修改服务端地址，请直接输入(如 http://172.28.26.33:8000)，直接回车保持默认: " -NoNewline -ForegroundColor Gray
$inputServer = Read-Host
if (-not [string]::IsNullOrWhiteSpace($inputServer)) {
    $ServerUrl = $inputServer.Trim().TrimEnd('/')
    Set-Content -LiteralPath $serverConfigFile -Value $ServerUrl -Encoding UTF8
}

# 确认/修改担当姓名
Write-Host "当前担当姓名: [$UserName] (直接回车保持，或输入新姓名并回车修改): " -NoNewline -ForegroundColor Yellow
$inputUser = Read-Host
if (-not [string]::IsNullOrWhiteSpace($inputUser)) {
    $UserName = $inputUser.Trim()
    Set-Content -LiteralPath $userConfigFile -Value $UserName -Encoding UTF8
}

Write-Host "已就绪: 担当 [$UserName] | 服务端: $ServerUrl" -ForegroundColor Green
Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray

function Get-Profiles {
    param([string]$BaseUrl)
    try {
        $url = "$($BaseUrl.TrimEnd('/'))/api/profiles"
        $resp = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 8
        return $resp
    } catch {
        Write-Host "无法连接到服务端 [$BaseUrl] : $_" -ForegroundColor Red
        return $null
    }
}

function Invoke-EnvCheckAndInstall {
    param([string]$BaseUrl)
    Write-Host "`n==========================================================" -ForegroundColor Cyan
    Write-Host "   Codex 客户端离线开发环境体检与一键部署 (无需外网)   " -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan
    
    # 1. 检查 Node.js
    $nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
    $nodeVer = if ($nodeCmd) { (node -v) } else { $null }
    if ($nodeVer) {
        Write-Host " [OK] 1. Node.js 运行环境:     $nodeVer" -ForegroundColor Green
    } else {
        Write-Host " [!!] 1. Node.js 运行环境:     未安装 (缺少 Node.js 运行环境)" -ForegroundColor Red
    }
    
    # 2. 检查 Codex CLI
    $codexCmd = Get-Command codex.cmd -ErrorAction SilentlyContinue
    $codexVer = if ($codexCmd) { (cmd.exe /c codex --version) } else { $null }
    if ($codexVer) {
        Write-Host " [OK] 2. Codex CLI 命令行:     $codexVer" -ForegroundColor Green
    } else {
        Write-Host " [!!] 2. Codex CLI 命令行:     未安装 (缺少 @openai/codex)" -ForegroundColor Red
    }
    
    # 3. 检查 VS Code
    $codeCmd = Get-Command code.cmd -ErrorAction SilentlyContinue
    if (-not $codeCmd) {
        $vsPaths = @(
            "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
            "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd"
        )
        foreach ($vp in $vsPaths) {
            if (Test-Path $vp) { $codeCmd = $vp; break }
        }
    }
    if ($codeCmd) {
        Write-Host " [OK] 3. VS Code 主程序:       已检测到 ($codeCmd)" -ForegroundColor Green
        # 4. 检查 VS Code 插件
        $exts = & $codeCmd --list-extensions
        if ($exts -match "openai.chatgpt") {
            Write-Host " [OK] 4. Codex VS Code 插件:   已安装 (openai.chatgpt)" -ForegroundColor Green
        } else {
            Write-Host " [!!] 4. Codex VS Code 插件:   未安装 (openai.chatgpt)" -ForegroundColor Red
        }
    } else {
        Write-Host " [!!] 3. VS Code 主程序:       未检测到 VS Code" -ForegroundColor Yellow
        Write-Host " [!!] 4. Codex VS Code 插件:   需先安装 VS Code 主程序" -ForegroundColor Yellow
    }
    
    Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "选项: [1] 一键离线安装全部缺失组件 | [b] 返回主菜单: " -NoNewline -ForegroundColor Yellow
    $ans = Read-Host
    if ($ans -ne '1') { return }
    
    $tempDir = Join-Path $env:TEMP "codex_offline_install"
    if (-not (Test-Path $tempDir)) { New-Item -ItemType Directory -Path $tempDir -Force | Out-Null }
    
    # 安装 Node.js
    if (-not $nodeVer) {
        Write-Host "`n>>> [1/3] 从服务端高速下载 Node.js v20 (node-v20-x64.msi)..." -ForegroundColor Cyan
        $msiPath = Join-Path $tempDir "node-v20-x64.msi"
        $url = "$($BaseUrl.TrimEnd('/'))/api/packages/download/node-v20-x64.msi"
        Invoke-WebRequest -Uri $url -OutFile $msiPath
        Write-Host ">>> 正在执行静默安装: msiexec /i node-v20-x64.msi /passive /norestart ..." -ForegroundColor Yellow
        Start-Process "msiexec.exe" -ArgumentList "/i `"$msiPath`" /passive /norestart" -Wait
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        Write-Host ">>> [成功] Node.js 安装完成！" -ForegroundColor Green
    }
    
    # 安装 Codex CLI
    if (-not $codexVer) {
        Write-Host "`n>>> [2/3] 从服务端高速下载 Codex CLI 离线包 (openai-codex.tgz)..." -ForegroundColor Cyan
        $tgzPath = Join-Path $tempDir "openai-codex.tgz"
        $url = "$($BaseUrl.TrimEnd('/'))/api/packages/download/openai-codex.tgz"
        Invoke-WebRequest -Uri $url -OutFile $tgzPath
        Write-Host ">>> 正在执行离线全局安装: npm install -g openai-codex.tgz ..." -ForegroundColor Yellow
        Start-Process "cmd.exe" -ArgumentList "/c npm install -g `"$tgzPath`"" -Wait -NoNewWindow
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        Write-Host ">>> [成功] Codex CLI 全局安装完成！" -ForegroundColor Green
    }
    
    # 安装 VS Code 插件
    if ($codeCmd) {
        $exts = & $codeCmd --list-extensions
        if ($exts -notmatch "openai.chatgpt") {
            Write-Host "`n>>> [3/3] 从服务端高速下载 VS Code 插件 (openai.chatgpt.vsix, 522MB)..." -ForegroundColor Cyan
            $vsixPath = Join-Path $tempDir "openai.chatgpt.vsix"
            $url = "$($BaseUrl.TrimEnd('/'))/api/packages/download/openai.chatgpt.vsix"
            Invoke-WebRequest -Uri $url -OutFile $vsixPath
            Write-Host ">>> 正在安装 VS Code 插件: code --install-extension ..." -ForegroundColor Yellow
            Start-Process "cmd.exe" -ArgumentList "/c `"$codeCmd`" --install-extension `"$vsixPath`" --force" -Wait -NoNewWindow
            Write-Host ">>> [成功] Codex VS Code 插件安装完成！" -ForegroundColor Green
        }
    }
    
    Write-Host "`n>>> 🎉 所有缺失组件已安装完成！" -ForegroundColor Green
    Write-Host "按回车键返回主菜单..." -ForegroundColor Gray
    Read-Host
}

function Show-ProfileList {
    param($Profiles)
    if (-not $Profiles -or $Profiles.Count -eq 0) {
        Write-Host "服务端目前没有任何认证配置，请先在网页端上传！" -ForegroundColor Yellow
        return
    }

    Write-Host "`n当前可用服务端账号、官方额度及每个账号的使用者清单:" -ForegroundColor Green
    Write-Host ("=" * 82) -ForegroundColor DarkGray

    for ($i = 0; $i -lt $Profiles.Count; $i++) {
        $p = $Profiles[$i]
        $userCount = $p.active_users_count
        $limitStr = if ($p.max_users -gt 0) { "$userCount/$($p.max_users)" } else { "$userCount" }
        
        $onlineUsers = @()
        if ($p.active_users) {
            foreach ($u in $p.active_users) {
                $dName = if ($u.display_name) { $u.display_name } else { $u.client_user }
                $statusTag = if ($u.is_online) { "在线" } else { "在用" }
                $onlineUsers += "$dName ($statusTag)"
            }
        }
        $userListText = if ($onlineUsers.Count -gt 0) { $onlineUsers -join ", " } else { "闲置中 (暂无担当使用)" }

        # 额度信息
        $q = $p.quota
        $planStr = if ($p.plan_type) { "[$($p.plan_type)]" } else { "" }
        $emailStr = if ($p.account_email) { "<$($p.account_email)>" } else { "" }

        $p5Rem = if ($q.five_hour) { $q.five_hour.remaining_percent } else { 0 }
        $p5Reset = if ($q.five_hour.reset_after_text) { $q.five_hour.reset_after_text } else { "-" }
        $p5Color = if ($p5Rem -gt 30) { "Green" } elseif ($p5Rem -gt 10) { "Yellow" } else { "Red" }

        $pwRem = if ($q.weekly) { $q.weekly.remaining_percent } else { 0 }
        $pwReset = if ($q.weekly.reset_after_text) { $q.weekly.reset_after_text } else { "-" }
        $pwColor = if ($pwRem -gt 30) { "Green" } elseif ($pwRem -gt 10) { "Yellow" } else { "Red" }

        Write-Host ("[{0,2}] 账号名称: {1} {2} {3}" -f ($i + 1), $p.name, $planStr, $emailStr) -ForegroundColor White
        
        # 额度行
        Write-Host "     ⚡ 5小时额度: " -NoNewline -ForegroundColor Cyan
        Write-Host ("剩余 {0,3}% (重置倒计时: {1})" -f $p5Rem, $p5Reset) -ForegroundColor $p5Color
        
        Write-Host "     📅 本周额度:  " -NoNewline -ForegroundColor Cyan
        Write-Host ("剩余 {0,3}% (重置倒计时: {1})" -f $pwRem, $pwReset) -ForegroundColor $pwColor

        # 每个人数与谁在用行
        Write-Host "     👥 使用状态:  " -NoNewline -ForegroundColor Gray
        $statusColor = if ($userCount -gt 0) { "Yellow" } else { "DarkGray" }
        Write-Host ("当前有 {0} 人在用 -> {1}" -f $limitStr, $userListText) -ForegroundColor $statusColor
        
        if ($p.description) {
            Write-Host ("     📝 账号备注:  {0}" -f $p.description) -ForegroundColor DarkGray
        }
        Write-Host ("-" * 82) -ForegroundColor DarkGray
    }
}

$profiles = Get-Profiles -BaseUrl $ServerUrl
if (-not $profiles) {
    Write-Host "`n[跨网段/VPN 连通提示]" -ForegroundColor Yellow
    Write-Host "如果您的机器在 VPN 或其他子网，且无法连入 $ServerUrl：" -ForegroundColor Cyan
    Write-Host "1. 请确认服务端已开启，并在服务端运行 open_firewall_port.bat 开放防火墙端口 8000" -ForegroundColor Gray
    Write-Host "2. 尝试使用 Tailscale 专线地址: http://100.100.88.111:8000" -ForegroundColor Gray
    Write-Host "3. 尝试使用 VPN 网卡地址:       http://172.19.1.1:8000" -ForegroundColor Gray
    Write-Host "4. 亦可在浏览器打开网页直接点击「下载 auth.json」拷贝到 ~/.codex/ 目录" -ForegroundColor Gray
    Write-Host "`n按任意键退出..."
    pause
    exit 1
}

Show-ProfileList -Profiles $profiles

Write-Host "`n操作选项: [1-$($profiles.Count)] 切换账号 | [d] 仅下载json另存为 | [e] 离线环境体检/一键安装 | [r] 刷新 | [q] 退出: " -NoNewline -ForegroundColor Cyan
$choice = Read-Host

while ($choice -eq 'r' -or $choice -eq 'e') {
    if ($choice -eq 'e') {
        Invoke-EnvCheckAndInstall -BaseUrl $ServerUrl
    }
    $profiles = Get-Profiles -BaseUrl $ServerUrl
    Show-ProfileList -Profiles $profiles
    Write-Host "`n操作选项: [1-$($profiles.Count)] 切换账号 | [d] 仅下载json另存为 | [e] 离线环境体检/一键安装 | [r] 刷新 | [q] 退出: " -NoNewline -ForegroundColor Cyan
    $choice = Read-Host
}

if ($choice -eq 'q' -or [string]::IsNullOrWhiteSpace($choice)) {
    Write-Host "已退出。" -ForegroundColor Gray
    exit
}

# ----------------- 仅下载另存为逻辑 -----------------
if ($choice -eq 'd') {
    Write-Host "请输入要下载的账号序号 [1-$($profiles.Count)]: " -NoNewline -ForegroundColor Yellow
    $dChoice = Read-Host
    $dIndex = [int]$dChoice - 1
    if ($dIndex -ge 0 -and $dIndex -lt $profiles.Count) {
        $p = $profiles[$dIndex]
        Write-Host "请输入保存路径 [默认: .\auth-$($p.name).json]: " -NoNewline -ForegroundColor Yellow
        $savePath = Read-Host
        if ([string]::IsNullOrWhiteSpace($savePath)) {
            $savePath = ".\auth-$($p.name).json"
        }
        $dUrl = "$($ServerUrl.TrimEnd('/'))/api/profiles/$($p.id)/download?user=$([System.Uri]::EscapeDataString($UserName))"
        Invoke-WebRequest -Uri $dUrl -OutFile $savePath
        Write-Host ">>> [成功] 文件已保存至: $savePath" -ForegroundColor Green
        Write-Host ">>> 已在服务端为您登记并清除旧账号占用！" -ForegroundColor Green
    }
    exit 0
}

$selectedIndex = [int]$choice - 1
if ($selectedIndex -lt 0 -or $selectedIndex -ge $profiles.Count) {
    Write-Host "无效的选项序号！" -ForegroundColor Red
    exit
}

$selectedProfile = $profiles[$selectedIndex]
Write-Host "`n>>> 正在为担当 [$UserName] 申请账号: [$($selectedProfile.name)] ..." -ForegroundColor Cyan
Write-Host ">>> 系统将自动清除您在数据库中此前使用的旧账号记录..." -ForegroundColor Yellow

# 申请并下载（服务端会自动清该担当名下的任何旧记录）
$sessionToken = [System.Guid]::NewGuid().ToString()
$applyUrl = "$($ServerUrl.TrimEnd('/'))/api/sessions/apply"
$body = @{
    profile_id = $selectedProfile.id
    client_user = $UserName
    client_hostname = $HostName
    session_token = $sessionToken
} | ConvertTo-Json

try {
    $applyResp = Invoke-RestMethod -Uri $applyUrl -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
} catch {
    Write-Host "申请使用失败: $_" -ForegroundColor Red
    exit
}

# 写入本地 auth.json
try {
    if (Test-Path -LiteralPath $targetAuthPath) {
        $backupPath = "$targetAuthPath.bak"
        Copy-Item -LiteralPath $targetAuthPath -Destination $backupPath -Force
        Write-Host ">>> 已备份旧凭据至: $backupPath" -ForegroundColor DarkGray
    }

    $jsonContent = $applyResp.auth_json | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($targetAuthPath, $jsonContent, [System.Text.Encoding]::UTF8)

    Write-Host "`n==========================================================" -ForegroundColor Green
    Write-Host ">>> [成功] 凭据已写入到本地: $targetAuthPath" -ForegroundColor Green
    Write-Host ">>> 担当 [$UserName] 当前独占账号: [$($selectedProfile.name)]" -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Green
} catch {
    Write-Host "写入本地文件失败: $_" -ForegroundColor Red
    exit
}

# 解决 VS Code 插件免重启生效提示
Write-Host "`n💡【VS Code 插件免重启快速生效指南】" -ForegroundColor Yellow
Write-Host "无需关闭并重新启动整个 VS Code！只需采用以下快捷方式：" -ForegroundColor Cyan
Write-Host "  👉 方法 1 (推荐 1秒生效): 在 VS Code 中按「Ctrl + Shift + P」，输入「Reload Window」(重载窗口) 并回车，插件即刻重新读取最新认证！" -ForegroundColor White
Write-Host "  👉 方法 2 (快捷键): 在 VS Code 中按「Ctrl + R」亦可快速重载当前窗口。" -ForegroundColor White
Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "正在保持在线心跳... 其他团队成员已能在看板看到你正在使用 [$($selectedProfile.name)]。" -ForegroundColor Green
Write-Host "按 Ctrl+C 可断开并释放此账号占用。" -ForegroundColor Gray
Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray

$hbUrl = "$($ServerUrl.TrimEnd('/'))/api/sessions/heartbeat"
$hbBody = @{ session_token = $sessionToken } | ConvertTo-Json

try {
    while ($true) {
        Start-Sleep -Seconds 30
        try {
            $hbResp = Invoke-RestMethod -Uri $hbUrl -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($hbBody)) -TimeoutSec 5
            $timeStr = (Get-Date).ToString("HH:mm:ss")
            Write-Host "[$timeStr] 心跳上报成功 (维持在线状态中...)" -ForegroundColor DarkGray
        } catch {
            Write-Host "心跳上报异常: $_" -ForegroundColor DarkRed
        }
    }
} finally {
    Write-Host "`n正在释放服务端使用占位..." -ForegroundColor Yellow
    try {
        $releaseUrl = "$($ServerUrl.TrimEnd('/'))/api/sessions/release"
        $relBody = @{ session_token = $sessionToken } | ConvertTo-Json
        Invoke-RestMethod -Uri $releaseUrl -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($relBody)) -TimeoutSec 5 | Out-Null
        Write-Host "已成功释放，再见！" -ForegroundColor Green
    } catch {
        Write-Host "释放会话完成。" -ForegroundColor DarkGray
    }
}
