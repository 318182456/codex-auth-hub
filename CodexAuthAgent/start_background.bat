@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_hidden.ps1" -StartOnly
pause
