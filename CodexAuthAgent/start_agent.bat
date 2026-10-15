@echo off
setlocal
cd /d "%~dp0"
if not exist "agent_config.json" copy /y "agent_config.example.json" "agent_config.json" >nul
if exist "CodexAuthAgent.exe" (
    CodexAuthAgent.exe --config "%~dp0agent_config.json"
) else (
    python auth_agent.py --config "%~dp0agent_config.json"
)
pause
