@echo off
chcp 65001 >nul
title Codex Auth Hub 服务端
echo ===================================================
echo   Codex Auth Hub 服务端正在启动中...
echo ===================================================
cd /d "%~dp0"
start "CodexAuthServer" CodexAuthServer.exe
