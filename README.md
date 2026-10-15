# Codex Auth Hub (认证中枢分发 & 额度追踪系统)

专为团队多人共享/管理 OpenAI Codex 认证设计的一体化服务端与客户端系统。
默认服务端连接地址：**`http://172.28.26.33:8000`**

## 登录服务器 Agent：重新登录后自动上传认证

新版管理面板提供「登录服务器 Agent」清单，显示主机/IP、连接状态、本机当前账号、关联账号、同步状态、版本和最近心跳。Agent 启动即上报，之后每 30 秒心跳一次，超过 90 秒未上报判定离线，页面每 5 秒更新；离线记录会保留，服务端重启不会丢失。即使本地认证缺失或上传失败，心跳仍独立运行。使用此功能需同时更新服务端和 Agent；服务端启动会自动创建及升级心跳表，无需手动执行 SQL。

在实际执行账号登录的 Windows 机器上运行新增的 `agent/CodexAuthAgent.exe`，也可以双击根目录 `启动Agent.bat`。Agent 无需数据库连接，直接调用中枢 API。

1. 将整个 `agent` 目录复制到登录机器，将 `agent_config.example.json` 复制为 `agent_config.json`。
2. 本地只需修改 `server_url` 为中枢地址，**无需配置 `watches`、账号 ID 或认证文件路径**。旧版配置中残留的 `watches` 会被忽略。
3. 运行 `启动Agent.bat`（部署包解压后运行 `start_agent.bat`）。Agent 自动读取本机当前登录账号并报告至服务端：优先用 `account_id` 唯一匹配已有账号，无法取得账号 ID 的历史记录可用邮箱辅助匹配。未匹配或存在重复账号时不上传，在服务端对应主机/IP 行点击「关联账号」，选择已有账号后保存即可。也可选择「自动匹配当前账号」恢复自动关联；设置在下一次心跳后生效，无需重启 Agent。
4. 配置确认后，可在 PowerShell 运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\agent\install_autostart.ps1`，注册当前 Windows 用户登录后自动启动，并立即运行。不要同时再手动启动第二份 Agent。取消自动启动使用相同命令追加 `-Remove`（只取消后续启动；停止当前任务可用 `Stop-ScheduledTask -TaskName CodexAuthHubAgent`）。

本地连接配置示例：

后台 Agent 更新：先将新版 `stop_agent.bat` 和 `stop_agent.ps1` 放进当前运行的 Agent 目录，双击 `stop_agent.bat`，提示停止成功后再覆盖 `CodexAuthAgent.exe` 及启动脚本。保留 `agent_config.json` 和 `agent_config.state.json`。更新后运行 `install_autostart.ps1` 恢复后台运行，或双击 `start_agent.bat` 前台运行。停止脚本只结束当前目录的 Agent，会临时禁用计划任务避免更新期间重启，停止完成后保留原来的登录自动启动设置。如提示权限不足，使用启动 Agent 的 Windows 用户运行；原进程以管理员身份运行时，也需以管理员身份停止。

```json
{
  "server_url": "http://172.28.26.33:8000",
  "poll_seconds": 3
}
```

Agent 优先读取运行用户的 `CODEX_HOME/auth.json`，否则读取用户目录 `.codex/auth.json`。心跳仅报告账号 ID、邮箱等识别信息，不携带认证令牌；服务端记录请求的连接 IP，账号绑定按 Agent 实例独立保存，避免相同出口 IP 的机器互相覆盖。当前账号改变时，会先停止向旧绑定上传，等待服务端重新识别。自动关联模式随当前登录账号切换；手动关联模式持续使用所选目标，选择时应确认对应账号。

关联成功后，Agent 在有效文件连续两次检查稳定后上传；同账号重新登录通常约 3–6 秒，首次关联或切换账号需要先等待下一次心跳（约 30 秒）。退出登录、文件临时缺失或 JSON 尚未写完时继续等待，不删除中枢账号。失败按 5 秒起步、最长 5 分钟间隔重试，新凭据会解除旧凭据的重试等待。同步状态保存在配置旁的 `agent_config.state.json`，仅含哈希和账号 ID，不保存令牌。请保留该文件，避免重启后再次上传本地旧凭据。后台日志为同目录 `agent_config.log`，自动轮转，Windows 下同一配置仅允许一个实例运行。

Agent 监测本机登录或 token 更新后产生的认证文件，不代替用户完成网页登录，也不会把中枢刷新的 token 写回本机。自动启动任务使用当前登录用户运行，需要该用户保持 Windows 会话。本地连接参数修改后需重启 Agent，服务端账号关联修改后自动生效。生产环境可配置 HTTPS 中枢地址；如代理要求 Bearer 认证，可通过 `CODEX_AUTH_HUB_TOKEN` 环境变量传入中枢令牌。

源码运行与打包（Agent 仅依赖 Python 标准库）：

```powershell
python agent/auth_agent.py --config agent/agent_config.json --check
python agent/auth_agent.py --config agent/agent_config.json
cd agent
python -m unittest test_auth_agent -v
python -m PyInstaller --noconfirm --distpath . CodexAuthAgent.spec
```

---

## 🌟 核心业务规则与功能

### 1. 独立用户系统 (按 User 区分人员 & 单人单号独占约束)
* **用户认证**：客户端和 Web 面板支持用户登录/注册（默认管理员账号：`admin` / `admin123`，也可以直接注册新用户）。
* **独占约束**：按 User 分开独立追踪，一个用户同一时间只能占用一个 Codex 账号。
* **自动释放**：当用户在客户端切换并应用新账号时，系统会自动在数据库 `user_sessions` 中**清除该用户此前占用的旧账号**，旧账号人数立即释放，新账号使用人数加 1。

### 2. 账号卡片管理 (支持直接替换 auth.json & 修复删除逻辑)
* **[✎ 编辑]**：点击卡片名称旁的编辑按钮修改账号名称，保存后客户端重新获取列表时显示新名称。名称需为 1 至 100 个字符且不能与其他账号重复。
* **[🔄 刷新额度]**：随时向官方接口查询该账号最新 5小时与周剩余额度。
* **[📤 替换 auth.json]**：无需删除账号，在卡片上直接点击即可选择新的 `auth.json` 替换该节点的认证凭据，并立即同步最新额度。
* **[🗑️ 删除]**：已修复外键关联约束，删除节点时自动清理关联会话和日志，操作秒级完成。

### 3. 客户端直接下载并替换本地认证 (~/.codex/auth.json)
* 客户端（`CodexAuthClient.exe`）内置一键替换功能：
  1. 选中任意可用账号节点。
  2. 点击 **“🚀 下载并替换本地认证 (~/.codex/auth.json)”**。
  3. 客户端自动从服务端拉取凭据，自动对原有 `auth.json` 备份为 `.bak`，并写入本地 `%USERPROFILE%\.codex\auth.json`。
  4. 自动维持心跳，断开或切换时自动释放名额。

### 4. 实时额度监控（按账号实际返回的周期显示）
* 账号卡片显示认证有效、认证失效、暂时无法验证、未验证及最近验证时间。认证失效时提示重新登录并替换 JSON；网络错误或查询限流显示暂时无法验证。上传不同凭据后重置为未验证，等待实际查询结果。
* 自动对接 OpenAI 官方后台使用接口（`wham/usage`）。
* 服务端启动后立即检查启用账号的 access token，并每 60 秒独立检查一次 JWT `exp`；剩余不超过 5 分钟时提前刷新（需有效 `refresh_token`），新凭据自动写回数据库，不依赖管理页面访问。刷新失败时隔 5 分钟重试；上传新凭据可解除旧凭据的重试等待。停用账号和仅使用 API Key 的账号不参与定时刷新。
* 定时任务还会查询超过 90 秒未更新的额度，验证 token 实际可用性，不仅依赖 JWT `exp`。查询遇到 401 时尝试刷新 token；无法解析到期时间的 token 也通过此机制检测。账号列表访问仍会触发后台额度查询。
* 手动或后台查询额度遇到 401 时，自动使用 `refresh_token` 换取新凭据，写回数据库后重试一次；同一配置的并发刷新通过数据库行锁串行处理。缺少或失效的 `refresh_token` 需要重新登录并上传最新 `auth.json`。已下载的本地凭据需再次下载替换。
* 按接口的 `limit_window_seconds` 区分 **5小时额度（18000秒）** 与 **周额度（604800秒）**，不把 `primary_window` / `secondary_window` 固定当作特定周期。
* 只返回周额度的账号显示实际周剩余百分比及重置倒计时；客户端在所有账号均未返回5小时窗口时隐藏该列。
* 缺失窗口或使用量不会当作剩余100%；重置倒计时按返回的重置时间计算，不假定在周末重置。
* **套餐识别**：自动展示 `TEAM`、`PLUS`、`PRO` 等级别与绑定邮箱。

---

## 🚀 打包可执行程序 (EXE) 清单

无需安装 Python 或任何环境，直接运行根目录下生成的免安装可执行程序：

| 程序文件 | 适用对象 | 说明 |
| :--- | :--- | :--- |
| **`CodexAuthServer.exe`** | 服务端机（运行于 `172.28.26.33` 或任意局域网机器） | 启动中枢服务 (FastAPI + PG)，动态检测本机所有 IP，提供 Web 管理面板与 API |
| **`CodexAuthClient.exe`** | 客户端机（开发人员电脑） | 桌面 GUI 客户端，默认连接 `http://172.28.26.33:8000`，支持用户登录与本地认证一键替换 |

---

## 💡 VS Code 插件免重启快速生效指南

在客户端切换并替换本地认证后，**无需关闭重启 VS Code**：
1. 切换至您的 VS Code 窗口。
2. 按快捷键：<kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>P</kbd>。
3. 输入并选择：`Reload Window` (开发人员: 重新加载窗口) 并回车。
4. 窗口瞬间刷新，插件会立即使用本地最新的 `auth.json` 凭据！

---

## 📂 项目结构

```text
codex-auth-hub/
├── CodexAuthServer.exe         # 服务端独立打包 EXE
├── CodexAuthClient.exe         # 客户端独立打包 EXE
├── server/
│   ├── main.py                 # FastAPI 后端路由 (用户认证/替换凭据/删除/会话互斥)
│   ├── database.py             # PostgreSQL 数据库建表与配置读取 (支持 db_config.json)
│   ├── quota_service.py        # OpenAI 官方 5小时与周额度同步服务
│   ├── run_server.py           # 服务端启动入口 (动态网络IP检测)
│   ├── start_server.bat        # 批处理启动脚本
│   ├── open_firewall_port.bat  # 一键放通防火墙 8000 端口脚本
│   └── static/                 # 现代暗黑 Web 管理面板
│       ├── index.html
│       ├── style.css
│       └── app.js
├── client/
│   ├── client_gui.py           # 桌面图形客户端源码 (Tkinter)
│   ├── CodexClient.ps1         # 原生 PowerShell 客户端脚本
│   ├── start_gui_client.bat    # 启动 GUI 客户端批处理
│   └── start_powershell_client.bat
└── README.md
```
