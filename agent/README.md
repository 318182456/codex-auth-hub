# Codex Auth Hub Agent Windows 服务

开机后自动运行，不需要登录 Windows 用户。服务名 `CodexAuthHubAgent`，显示名称 `Codex Auth Hub Agent`，默认使用 LocalSystem，配置为自动启动，失败后按 10 / 30 / 60 秒重启。服务端继续显示 IP、当前账号、在线状态，并自动匹配或手动关联账号。

## 安装

1. 解压到固定的本地目录，修改 `agent_config.json` 中的 `server_url`。首次安装没有配置时会从示例自动创建。
2. 右键 `install_service.bat`，选择「以管理员身份运行」。必须使用新版服务版 `CodexAuthAgent.exe`，旧 EXE 不支持 Windows 服务协议。
3. 安装成功后立即启动，之后电脑重启、尚未登录用户时也会运行。

从旧后台版本迁移时，若 EXE 被占用：先只复制新版 `service.ps1` 到原 Agent 目录，在管理员 PowerShell 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Stop`，再覆盖服务版 EXE 并运行安装。Stop 同时清理同目录旧登录启动项与旧后台进程。

安装器自动记录执行安装时用户的 `CODEX_HOME/auth.json` 或用户目录 `.codex/auth.json`，并作为服务启动参数保存。若提权使用的是另一个管理员账号，或希望指定其他登录用户的文件，请在管理员 PowerShell 中运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Install -AuthFilePath "C:\Users\实际登录用户\.codex\auth.json"
```

认证文件暂时不存在时，服务仍会启动并等待本机重新登录，不会读取 LocalSystem 的认证目录。安装后不要移动 Agent 目录。认证文件应位于本地磁盘，服务不会依赖用户登录后才建立的映射盘。

安装会清理同目录旧版本的 Run 登录启动项、启动文件夹快捷方式、计划任务及旧后台进程；服务启动成功后删除旧的 `install_autostart`、`run_hidden`、`start_agent`、`start_background`、`stop_agent`、`diagnose_autostart` 脚本。不会删除认证、连接配置和同步状态。

## 管理与更新

查看状态无需管理员，其他管理操作需管理员权限：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Stop
# 停止完成后替换 EXE，保留 agent_config.json 和 agent_config.state.json
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Start
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Restart
powershell -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Uninstall
```

也可运行 `services.msc`，查找 `Codex Auth Hub Agent` 管理服务。卸载可右键 `uninstall_service.bat` 以管理员身份运行，认证与配置保留。

服务使用安装时的绝对配置路径，同步状态和账号绑定不因切换为服务而改变。网络未就绪时继续重试心跳及上传。运行日志为同目录 `agent_config.log`；服务初始化错误也可查看 Windows「事件查看器 → Windows 日志 → 应用程序」。如服务运行但面板提示等待认证，检查安装命令显示的认证路径是否为实际登录用户目录。

Agent 只监测认证文件变化。账号失效后的网页登录仍需人工完成；重新登录产生新认证后自动上传。

## 构建与检查

```powershell
python -m pip install -r requirements-build.txt
python -m unittest test_auth_agent test_windows_service -v
python -m PyInstaller --noconfirm --distpath . CodexAuthAgent.spec
```

开发时可直接运行 Python，但服务安装使用打包后的 EXE。
