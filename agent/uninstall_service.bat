@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" -Action Uninstall
pause
