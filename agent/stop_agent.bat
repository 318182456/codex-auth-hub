@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_agent.ps1"
if errorlevel 1 (
    echo Stop failed. Run this file as the Windows user who started the Agent.
)
pause
