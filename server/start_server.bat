@echo off
setlocal
cd /d "%~dp0"
set DB_HOST=172.28.26.174
set DB_PORT=5432
set DB_USER=postgres
set DB_PASSWORD=postgres
set DB_NAME=codex_auth_hub

powershell -NoProfile -ExecutionPolicy Bypass -Command "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Host '========================================================' -ForegroundColor Cyan; Write-Host '   Codex Auth Hub 服务端正在启动 (FastAPI + PostgreSQL)' -ForegroundColor Cyan; Write-Host '========================================================' -ForegroundColor Cyan"

:: 1. 检查 Python 环境
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Host '[错误] 未检测到 Python 环境！请先安装 Python 并勾选 Add to PATH。' -ForegroundColor Red"
    pause
    exit /b 1
)

:: 2. 检查并自动安装缺失的依赖库
python -c "import psycopg, fastapi, uvicorn, httpx" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Host '[提示] 检测到当前 Python 缺少运行依赖，正在自动安装 (pip install -r requirements.txt)...' -ForegroundColor Yellow"
    pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; Write-Host '[错误] 依赖安装失败，请检查 pip 或网络连接！' -ForegroundColor Red"
        pause
        exit /b 1
    )
)

:: 3. 启动服务端
python run_server.py
pause
