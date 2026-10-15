@echo off
chcp 65001 >nul
title Codex Auth Hub 客户端
cd /d "%~dp0"
start "" CodexAuthClient.exe
